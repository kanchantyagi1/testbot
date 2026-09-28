"""
OPTIC BOT APIs - START READING HERE.

There is no frontend in this repo. Every endpoint below is a plain function
(no serializer classes, no viewsets) that a human-review frontend, or your
browser's DRF browsable API, calls directly. JWT auth is handled by that
frontend, not here - see settings.py REST_FRAMEWORK for why.

Endpoints (see PLAN.md §7 for the full table):
    GET   /api/health/                       - dependency status, never 500s
    GET   /api/config/                       - current threshold + status counts
    GET   /api/orders/                       - list, with filters
    GET   /api/orders/<id>/                  - full detail
    PATCH /api/orders/<id>/fields/           - human edits a field
    POST  /api/orders/<id>/approve/          - human approves
    POST  /api/orders/<id>/reject/           - human rejects
    GET   /api/orders/<id>/audit/            - audit trail
    POST  /api/orders/<id>/reprocess/        - re-run extraction
    POST  /api/orders/<id>/export/           - build SAP CSV, upload to S3_OUTPUT_BUCKET
    POST  /api/poll/                         - trigger one mailbox poll now
"""

import logging
from functools import wraps

from django.conf import settings
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Order
from .services import (
    ConfigError,
    aws_credentials_status,
    collect_low_confidence_paths,
    export_order_to_csv,
    log_audit,
    oe_master_status,
    poll_mailbox,
    presigned_url,
    reprocess_order,
    score,
    set_by_path,
)

logger = logging.getLogger(__name__)


# =============================================================================
# small shared helpers
# =============================================================================

def handle_errors(view_func):
    """Every view is wrapped with this instead of repeating the same
    try/except in each function. ConfigError (a named missing .env value)
    becomes a 400; Http404 (from get_object_or_404) is left alone so DRF's
    own handler returns a clean 404; anything else becomes a 500 - never an
    HTML traceback."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except ConfigError as e:
            return Response({"error": str(e)}, status=400)
        except Http404:
            raise
        except Exception as e:
            logger.exception("Unhandled error in %s", view_func.__name__)
            return Response({"error": str(e)}, status=500)

    return wrapper


def get_threshold(request):
    """.env default, overridable per-request with ?threshold=0.9"""
    raw = request.query_params.get("threshold") if request is not None else None
    if raw is None:
        return settings.CONFIDENCE_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        return settings.CONFIDENCE_THRESHOLD


def order_to_dict(order, include_fields=True, threshold=None):
    """One order -> plain dict, ready for Response(). include_fields=False
    is used by the list API so a page of 20 orders doesn't ship every
    field's full extracted_data."""
    data = {
        "id": order.id,
        "message_id": order.message_id,
        "sender": order.sender,
        "subject": order.subject,
        "received_at": order.received_at,
        "attachment_id": order.attachment_id,
        "attachment_name": order.attachment_name,
        "file_type": order.file_type,
        "file_size": order.file_size,
        "status": order.status,
        "min_confidence": order.min_confidence,
        "oe_code": order.oe_code,
        "oe_confidence": order.oe_confidence,
        "oe_matched": order.oe_matched,
        "oe_match_reason": order.oe_match_reason,
        "oe_uom": order.oe_uom,
        "reviewed_by": order.reviewed_by,
        "reviewed_at": order.reviewed_at,
        "review_comment": order.review_comment,
        "error_message": order.error_message,
        "exported_at": order.exported_at,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }

    if include_fields:
        threshold = settings.CONFIDENCE_THRESHOLD if threshold is None else threshold
        fields = order.extracted_data or {}
        data["fields"] = fields
        data["oe_candidates"] = order.oe_candidates
        data["body_text"] = order.body_text
        # dotted paths into the nested extraction, e.g.
        # "order.account_number", "line_items.0.right_eye.sphere" - the
        # same paths PATCH /fields/ accepts
        data["low_confidence_fields"] = list(
            collect_low_confidence_paths(fields, threshold)
        )
        data["extraction_notes"] = fields.get("extraction_notes", "")

    return data


def _apply_field_edits(order, field_updates, actor):
    """Writes new values into order.extracted_data, marks each changed
    field confidence=1.0 and edited=True, recomputes min_confidence, and
    logs one AuditLog row per changed field. Does not save() the order -
    callers save once after also handling oe_code / status.

    Keys are DOTTED PATHS into the nested extraction, exactly as returned
    in low_confidence_fields, e.g.:
        {"order.account_number": "6279505",
         "line_items.0.right_eye.sphere": "-1.25"}
    """
    import copy

    data = copy.deepcopy(order.extracted_data or {})
    for path, new_value in field_updates.items():
        try:
            old_value = set_by_path(data, path, new_value)
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise ValueError(f"Cannot edit field '{path}': {e}")
        log_audit(
            order, "FIELD_EDITED", actor=actor,
            field_name=path, old_value=old_value, new_value=new_value,
        )
    order.extracted_data = data
    order.min_confidence = score(data)


def _apply_oe_edit(order, oe_code, actor):
    """A reviewer picking a different OE code from the candidate list (or
    typing one in) is the most common edit, so it gets first-class
    handling here, same as any other field."""
    if not oe_code:
        return
    old_oe = order.oe_code
    order.oe_code = oe_code
    order.oe_matched = True
    order.oe_confidence = 1.0
    log_audit(
        order, "FIELD_EDITED", actor=actor,
        field_name="oe_code", old_value=old_oe, new_value=oe_code,
    )


# =============================================================================
# 1. health
# =============================================================================

@api_view(["GET"])
@handle_errors
def health(request):
    def dep_status(names):
        missing = [n for n in names if not getattr(settings, n, None)]
        return f"not_configured: {', '.join(missing)}" if missing else "ok"

    try:
        Order.objects.exists()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    if settings.LLM_PROVIDER == "gateway":
        llm_status = dep_status(["LLM_GATEWAY_URL", "LLM_GATEWAY_API_KEY"])
    else:
        llm_status = "ok" if settings.BEDROCK_MODEL_ID else "not_configured: BEDROCK_MODEL_ID"

    return Response({
        "database": db_status,
        # A REAL check, not a presence check - static keys are optional
        # (services.aws_credential_kwargs()); this covers a CLI profile or
        # an EC2 instance role too, not just AWS_ACCESS_KEY_ID in .env.
        "aws_credentials": aws_credentials_status(),
        "outlook": dep_status(["MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID", "MAILBOX_USER_EMAIL"]),
        "s3": dep_status(["S3_INPUT_BUCKET"]),
        "llm": llm_status,
        "oe_master": oe_master_status(),
        "sqs": (dep_status(["SQS_QUEUE_URL"]) if settings.USE_SQS else "disabled (inline mode)"),
        "confidence_threshold": settings.CONFIDENCE_THRESHOLD,
        "scheduler": "enabled" if settings.RUN_SCHEDULER else "disabled",
    })


# =============================================================================
# 2. config
# =============================================================================

@api_view(["GET"])
@handle_errors
def config(request):
    from django.db.models import Count

    counts = {row["status"]: row["count"] for row in Order.objects.values("status").annotate(count=Count("id"))}
    for status_key, _label in Order.STATUS_CHOICES:
        counts.setdefault(status_key, 0)

    return Response({
        "confidence_threshold": settings.CONFIDENCE_THRESHOLD,
        "mailbox": settings.MAILBOX_USER_EMAIL,
        "mail_target_folder": settings.MAIL_TARGET_FOLDER,
        "mail_orders_folder": settings.MAIL_ORDERS_FOLDER,
        "mail_poll_minutes": settings.MAIL_POLL_MINUTES,
        "require_oe_match": settings.REQUIRE_OE_MATCH,
        "status_counts": counts,
    })


# =============================================================================
# 3. list orders
# =============================================================================

@api_view(["GET"])
@handle_errors
def list_orders(request):
    qs = Order.objects.all()

    status_param = request.query_params.get("status")
    if status_param:
        qs = qs.filter(status=status_param.upper())
    else:
        # default view hides junk/error rows unless explicitly asked for
        qs = qs.exclude(status__in=["NOT_AN_ORDER", "FAILED"])

    if request.query_params.get("below_threshold", "").lower() == "true":
        qs = qs.filter(min_confidence__lt=get_threshold(request))

    file_type = request.query_params.get("file_type")
    if file_type:
        qs = qs.filter(file_type=file_type.lower())

    message_id = request.query_params.get("message_id")
    if message_id:
        qs = qs.filter(message_id=message_id)

    search = request.query_params.get("search")
    if search:
        qs = qs.filter(
            Q(subject__icontains=search)
            | Q(sender__icontains=search)
            | Q(attachment_name__icontains=search)
            | Q(oe_code__icontains=search)
        )

    total = qs.count()

    try:
        page = max(int(request.query_params.get("page", 1)), 1)
    except ValueError:
        page = 1
    try:
        page_size = min(max(int(request.query_params.get("page_size", 20)), 1), 100)
    except ValueError:
        page_size = 20

    start = (page - 1) * page_size
    rows = qs[start:start + page_size]

    return Response({
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": [order_to_dict(o, include_fields=False) for o in rows],
    })


# =============================================================================
# 4. order detail
# =============================================================================

@api_view(["GET"])
@handle_errors
def get_order(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    data = order_to_dict(order, include_fields=True, threshold=get_threshold(request))
    data["source_url"] = presigned_url(order.s3_key)
    data["export_url"] = (
        presigned_url(order.export_s3_key, bucket=settings.S3_OUTPUT_BUCKET)
        if order.export_s3_key else None
    )
    data["sibling_attachments"] = list(
        Order.objects.filter(message_id=order.message_id)
        .exclude(id=order.id)
        .values("id", "attachment_name", "file_type", "status")
    )

    return Response(data)


# =============================================================================
# 5. edit fields
# =============================================================================

@api_view(["PATCH"])
@handle_errors
def edit_fields(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    fields = request.data.get("fields")
    reviewed_by = request.data.get("reviewed_by", "")
    oe_code = request.data.get("oe_code")

    if not fields and not oe_code:
        return Response({"error": "fields or oe_code is required"}, status=400)

    actor = reviewed_by or "unknown"
    if fields:
        _apply_field_edits(order, fields, actor)
    if oe_code:
        _apply_oe_edit(order, oe_code, actor)

    order.save()
    return Response(order_to_dict(order, include_fields=True))


# =============================================================================
# 6. approve
# =============================================================================

@api_view(["POST"])
@handle_errors
def approve_order(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    if order.status in ("APPROVED", "REJECTED"):
        return Response({"error": f"Order already {order.status}"}, status=400)

    reviewed_by = request.data.get("reviewed_by")
    if not reviewed_by:
        return Response({"error": "reviewed_by is required"}, status=400)

    fields = request.data.get("fields")
    if fields:
        _apply_field_edits(order, fields, reviewed_by)

    oe_code = request.data.get("oe_code")
    if oe_code:
        _apply_oe_edit(order, oe_code, reviewed_by)

    order.status = "APPROVED"
    order.reviewed_by = reviewed_by
    order.reviewed_at = timezone.now()
    order.review_comment = request.data.get("comment", "")
    order.save()

    log_audit(order, "APPROVED", actor=reviewed_by, note=order.review_comment)
    return Response(order_to_dict(order, include_fields=True))


# =============================================================================
# 7. reject
# =============================================================================

@api_view(["POST"])
@handle_errors
def reject_order(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    if order.status in ("APPROVED", "REJECTED"):
        return Response({"error": f"Order already {order.status}"}, status=400)

    reviewed_by = request.data.get("reviewed_by")
    reason = request.data.get("reason")
    if not reviewed_by:
        return Response({"error": "reviewed_by is required"}, status=400)
    if not reason:
        return Response({"error": "reason is required"}, status=400)

    order.status = "REJECTED"
    order.reviewed_by = reviewed_by
    order.reviewed_at = timezone.now()
    order.review_comment = reason
    order.save()

    log_audit(order, "REJECTED", actor=reviewed_by, note=reason)
    return Response(order_to_dict(order, include_fields=True))


# =============================================================================
# 8. audit trail
# =============================================================================

@api_view(["GET"])
@handle_errors
def order_audit(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    logs = order.audit_logs.all()  # AuditLog.Meta.ordering = oldest first

    return Response([
        {
            "id": log.id,
            "action": log.action,
            "actor": log.actor,
            "field_name": log.field_name,
            "old_value": log.old_value,
            "new_value": log.new_value,
            "note": log.note,
            "created_at": log.created_at,
        }
        for log in logs
    ])


# =============================================================================
# 9. reprocess
# =============================================================================

@api_view(["POST"])
@handle_errors
def reprocess_order_view(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    order = reprocess_order(order)
    return Response(order_to_dict(order, include_fields=True))


# =============================================================================
# 10. export to SAP CSV
# =============================================================================
# PLACEHOLDER LAYOUT - see the comment at the top of services.py Section F.
# Only APPROVED/AUTO_APPROVED orders can be exported; can be called again
# (e.g. after a late field correction) and just overwrites the same S3 key.

@api_view(["POST"])
@handle_errors
def export_order_view(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    if order.status not in ("APPROVED", "AUTO_APPROVED"):
        return Response(
            {"error": f"Order must be APPROVED or AUTO_APPROVED to export (current status: {order.status})"},
            status=400,
        )

    export_order_to_csv(order)
    return Response(order_to_dict(order, include_fields=True))


# =============================================================================
# 11. trigger a mailbox poll right now
# =============================================================================

@api_view(["POST"])
@handle_errors
def trigger_poll(request):
    summary = poll_mailbox()
    return Response(summary)
