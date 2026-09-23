"""
OPTIC BOT pipeline - Outlook polling, S3 upload, LLM extraction, OE lookup.

One file, six sections:
    A. config guard      - require() raises a clear error instead of a crash
    B. Outlook / Graph    - MSAL auth + reading the shared mailbox
    C. S3 + OE lookup     - upload attachments, ask the RAG agent for an OE code
    D. LLM extraction     - Bedrock/gateway converse call + JSON parsing
    E. the pipeline       - ties everything together into Order rows
    F. SAP CSV export     - approved order(s) -> CSV -> S3_OUTPUT_BUCKET

RULE FOR THIS FILE: no boto3/msal client may be built at MODULE IMPORT time.
Every client is built inside a function, after require() has already checked
the credentials it needs. That is what lets `import optic_bot.services`
succeed on a machine with an empty .env (see PLAN.md §11).
"""

import base64
import csv
import io
import json
import logging
import re
import time

import requests
from django.conf import settings
from django.utils import timezone

from .models import AuditLog, Order

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# =============================================================================
# Section A - config guard
# =============================================================================

class ConfigError(Exception):
    """Raised when a required .env value is missing.
    Views catch this and return a clean 400 instead of a 500 stack trace."""
    pass


def require(*setting_names):
    """Raise ConfigError naming every missing setting, e.g.
    'Missing MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID in .env'."""
    missing = [name for name in setting_names if not getattr(settings, name, None)]
    if missing:
        raise ConfigError(f"Missing {', '.join(missing)} in .env")


def log_audit(order, action, actor="system", field_name="", old_value="", new_value="", note=""):
    """Small shared helper - every status change and every field edit goes
    through here so the audit trail (GET /api/orders/<id>/audit/) is complete."""
    AuditLog.objects.create(
        order=order,
        action=action,
        actor=actor or "system",
        field_name=field_name,
        old_value=str(old_value) if old_value is not None else "",
        new_value=str(new_value) if new_value is not None else "",
        note=note,
    )


# =============================================================================
# Section B - Outlook / Graph
# =============================================================================

# Cached in-process so we don't re-authenticate on every poll; MSAL tokens are
# normally valid ~60-90 minutes.
_token_cache = {"access_token": None, "expires_at": 0}


def get_graph_token():
    """Client-credentials login (same shape as the reference test.py script)."""
    require("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID")

    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    import msal  # imported here, not at module level - see file docstring

    app = msal.ConfidentialClientApplication(
        settings.MS_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{settings.MS_TENANT_ID}",
        client_credential=settings.MS_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

    if "access_token" not in result:
        raise ConfigError(
            "Outlook authentication failed: "
            + result.get("error_description", str(result))
        )

    _token_cache["access_token"] = result["access_token"]
    _token_cache["expires_at"] = now + result.get("expires_in", 3600)
    return _token_cache["access_token"]


def build_headers(token):
    """THE PLAIN-TEXT RULE LIVES HERE.

    The Prefer header makes Graph return body.content as PLAIN TEXT with
    body.contentType == "text". Do NOT fetch HTML and strip tags afterwards -
    Outlook HTML is full of <style> blocks, conditional comments and tracking
    pixels, and a regex stripper leaves CSS junk in the middle of the order
    text. Let Graph do the conversion.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Prefer": 'outlook.body-content-type="text"',
    }


def get_folder_id(headers):
    """Find the 'OPTIC BOT' child folder under Inbox (case-insensitive)."""
    require("MAILBOX_USER_EMAIL")

    url = f"{GRAPH_BASE}/users/{settings.MAILBOX_USER_EMAIL}/mailFolders/inbox/childFolders"
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    target = settings.MAIL_TARGET_FOLDER.strip().lower()
    for folder in response.json().get("value", []):
        if folder.get("displayName", "").strip().lower() == target:
            return folder["id"]

    raise ConfigError(f"Folder '{settings.MAIL_TARGET_FOLDER}' not found under Inbox")


def fetch_new_emails(headers, folder_id):
    """Unread mail in the target folder, newest first."""
    require("MAILBOX_USER_EMAIL")

    url = (
        f"{GRAPH_BASE}/users/{settings.MAILBOX_USER_EMAIL}"
        f"/mailFolders/{folder_id}/messages"
        f"?$filter=isRead eq false"
        f"&$top={settings.MAIL_BATCH_SIZE}"
        f"&$orderby=receivedDateTime desc"
        f"&$select=id,subject,from,receivedDateTime,body,hasAttachments"
    )
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json().get("value", [])


def clean_body(mail):
    """Plain-text body, trimmed, ready to store and to send as LLM context.

    Safety net only: if body.contentType somehow comes back as html (a proxy
    dropped the Prefer header), fall back to a plain regex tag-stripper - do
    NOT add BeautifulSoup just for this rare case.
    """
    import html as html_module

    body = mail.get("body", {}) or {}
    text = body.get("content", "") or ""

    if body.get("contentType", "").lower() == "html":
        text = re.sub(r"<[^>]+>", " ", html_module.unescape(text))

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[: settings.MAX_BODY_CHARS]


def list_attachments(headers, message_id):
    """Metadata only - returns the attachments WORTH processing.

    Two steps on purpose (this call, then download_attachment per file): do
    NOT pull file content for every attachment in one response. A mail with
    four 4 MB files would blow past Graph's response size limit.
    """
    require("MAILBOX_USER_EMAIL")

    url = f"{GRAPH_BASE}/users/{settings.MAILBOX_USER_EMAIL}/messages/{message_id}/attachments"
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    min_bytes = settings.MIN_ATTACHMENT_KB * 1024
    max_bytes = settings.MAX_ATTACHMENT_MB * 1024 * 1024

    kept = []
    for att in response.json().get("value", []):
        name = att.get("name", "")
        size = att.get("size", 0)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            logger.info("Skipping attachment '%s': not a file attachment", name)
            continue
        if settings.SKIP_INLINE_ATTACHMENTS and att.get("isInline"):
            logger.info("Skipping attachment '%s': inline (signature image etc.)", name)
            continue
        if ext not in settings.ALLOWED_EXTENSIONS:
            logger.info("Skipping attachment '%s': extension .%s not allowed", name, ext)
            continue
        if size < min_bytes:
            logger.info("Skipping attachment '%s': %d bytes, below MIN_ATTACHMENT_KB", name, size)
            continue
        if size > max_bytes:
            logger.info("Skipping attachment '%s': %d bytes, above MAX_ATTACHMENT_MB", name, size)
            continue

        kept.append(att)

    return kept


def download_attachment(headers, message_id, attachment_id):
    """Raw file bytes via the $value endpoint (no base64 decode, no JSON)."""
    require("MAILBOX_USER_EMAIL")

    url = (
        f"{GRAPH_BASE}/users/{settings.MAILBOX_USER_EMAIL}"
        f"/messages/{message_id}/attachments/{attachment_id}/$value"
    )
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    return response.content


def mark_email_read(headers, message_id):
    if not settings.MARK_MAIL_AS_READ:
        return
    require("MAILBOX_USER_EMAIL")

    url = f"{GRAPH_BASE}/users/{settings.MAILBOX_USER_EMAIL}/messages/{message_id}"
    patch_headers = dict(headers)
    patch_headers["Content-Type"] = "application/json"
    response = requests.patch(url, headers=patch_headers, json={"isRead": True}, timeout=30)
    response.raise_for_status()


# =============================================================================
# Section C - S3 + OE lookup
# =============================================================================

def s3_client():
    """Just the AWS creds - NOT bucket-specific, because this client is
    reused for both S3_INPUT_BUCKET (attachments) and S3_OUTPUT_BUCKET
    (SAP CSV exports, Section F)."""
    require("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    import boto3  # imported here, not at module level - see file docstring

    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def upload_to_s3(file_bytes, key, bucket=None):
    """Uploads to S3_INPUT_BUCKET by default (attachments). Pass
    bucket=settings.S3_OUTPUT_BUCKET for the CSV export path (Section F)."""
    bucket = bucket or settings.S3_INPUT_BUCKET
    if not bucket:
        raise ConfigError("Missing S3_INPUT_BUCKET or S3_OUTPUT_BUCKET in .env")

    client = s3_client()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=file_bytes,
        ServerSideEncryption="aws:kms",
    )
    return key


def presigned_url(key, seconds=3600, bucket=None):
    """Used by the order-detail API for both the source document
    (S3_INPUT_BUCKET, the default) and the exported CSV (S3_OUTPUT_BUCKET).
    Returns None (never raises) if S3 isn't configured or the file isn't
    there - the API must still respond cleanly."""
    bucket = bucket or settings.S3_INPUT_BUCKET
    if not key or not bucket:
        return None
    try:
        client = s3_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=seconds,
        )
    except Exception:
        logger.exception("presigned_url failed for key=%s", key)
        return None


def lookup_oe_code(fields):
    """Ask the user's pgvector RAG agent for the OE code. See PLAN.md
    Appendix A for the table this backend does NOT touch directly, and A.5
    for the exact request/response contract used below.

    Returns (oe_code, oe_confidence, candidates_list, matched_bool).
    Never raises - an OE lookup failure must not fail the whole order; it
    just leaves the order without an OE code, which sends it to review.
    """
    if not settings.OE_RAG_URL:
        return (None, 0.0, [], False)

    def value_of(field_name):
        field = fields.get(field_name) or {}
        return field.get("value")

    try:
        payload = {
            "customer_name": value_of("customer_name"),
            "customer_account": value_of("customer_account"),
            "city": None,
            "country": value_of("ship_to_country"),
            "ship_to_address": value_of("ship_to_address"),
            "top_k": settings.OE_TOP_K,
        }
        response = requests.post(
            settings.OE_RAG_URL,
            headers={"Content-Type": "application/json", "x-api-key": settings.OE_RAG_API_KEY},
            json=payload,
            timeout=settings.OE_RAG_TIMEOUT,
        )
        response.raise_for_status()
        candidates = response.json().get("candidates", [])

        if candidates and candidates[0].get("score", 0.0) >= settings.OE_MATCH_THRESHOLD:
            top = candidates[0]
            return (top.get("oe_code"), top.get("score", 0.0), candidates, True)

        return (None, 0.0, candidates, False)

    except Exception:
        logger.exception("OE RAG lookup failed")
        return (None, 0.0, [], False)


# =============================================================================
# Section D - LLM extraction
# =============================================================================
#
# No local file parsing here. No PyPDF2, python-docx, openpyxl or pandas.
# Bedrock's converse API reads PDF/Word/Excel/CSV natively as a "document"
# content block; images go in an "image" block. One code path for every
# format instead of three brittle ones.

DOC_FORMATS = {
    "pdf": "pdf", "docx": "docx", "doc": "doc",
    "xlsx": "xlsx", "xls": "xls", "csv": "csv",
    "txt": "txt", "md": "md", "html": "html",
}
IMAGE_FORMATS = {
    "png": "png", "jpg": "jpeg", "jpeg": "jpeg",
    "gif": "gif", "webp": "webp",
}


def safe_doc_name(file_name):
    """GOTCHA - this one costs an hour if skipped.

    Bedrock rejects a document 'name' containing periods, underscores or
    consecutive spaces with a ValidationException. Strip the extension, then
    keep only letters, digits, spaces, hyphens, parentheses and square
    brackets; collapse runs of whitespace.
    "PO_4471.v2.xlsx" -> "PO 4471 v2"
    The name is only a label shown to the model, so rewriting it is safe.
    """
    name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    name = re.sub(r"[_.]+", " ", name)
    name = re.sub(r"[^A-Za-z0-9 \-()\[\]]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "document"


def build_content_block(file_bytes, file_name, provider=None):
    """Images use an 'image' block, everything else a 'document' block.

    boto3 (provider="bedrock") takes raw bytes in source.bytes. The HTTP
    gateway (provider="gateway") carries JSON, which cannot hold raw bytes,
    so that path base64-encodes it. This is the ONLY difference between the
    two providers.
    """
    provider = provider or settings.LLM_PROVIDER
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    source_bytes = base64.b64encode(file_bytes).decode() if provider == "gateway" else file_bytes

    if ext in IMAGE_FORMATS:
        return {"image": {"format": IMAGE_FORMATS[ext], "source": {"bytes": source_bytes}}}

    if ext in DOC_FORMATS:
        return {
            "document": {
                "format": DOC_FORMATS[ext],
                "name": safe_doc_name(file_name),
                "source": {"bytes": source_bytes},
            }
        }

    raise ConfigError(f"Unsupported file extension for the LLM: .{ext}")


def load_prompt(name):
    """Read prompts/{name}.txt - keeping the prompt in a file (not a Python
    string) is what lets it be tuned without a code deploy."""
    path = settings.PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def call_llm(prompt_text, file_bytes, file_name):
    """One function, an if/else on settings.LLM_PROVIDER. Same converse
    payload shape either way - the document block goes BEFORE the text
    block, so the model reads the file, then the instruction."""
    provider = settings.LLM_PROVIDER
    content_block = build_content_block(file_bytes, file_name, provider=provider)

    if provider == "bedrock":
        require("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        import boto3  # imported here, not at module level - see file docstring

        client = boto3.client(
            "bedrock-runtime",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
        response = client.converse(
            modelId=settings.BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [content_block, {"text": prompt_text}]}],
            inferenceConfig={"maxTokens": settings.LLM_MAX_TOKENS, "temperature": 0},
        )
        return response["output"]["message"]["content"][0]["text"]

    if provider == "gateway":
        require("LLM_GATEWAY_URL", "LLM_GATEWAY_API_KEY")
        url = settings.LLM_GATEWAY_URL.format(model=settings.BEDROCK_MODEL_ID)
        payload = {
            "messages": [{"role": "user", "content": [content_block, {"text": prompt_text}]}],
            "inferenceConfig": {"maxTokens": settings.LLM_MAX_TOKENS, "temperature": 0},
        }
        response = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-api-key": settings.LLM_GATEWAY_API_KEY},
            json=payload,
            timeout=settings.LLM_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return data["output"]["message"]["content"][0]["text"]

    raise ConfigError(f"Unknown LLM_PROVIDER '{provider}' - use 'bedrock' or 'gateway'")


def parse_llm_json(text):
    """Strip ```json fences if present, then json.loads. Raises a clear
    ValueError (not a bare JSONDecodeError) if the model didn't obey the
    output contract in the prompt."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM did not return valid JSON ({e}). Raw text: {text[:500]!r}")


# =============================================================================
# Section E - the pipeline
# =============================================================================

def score(fields):
    """Lowest confidence across every extracted field. 0.0 if there are no
    fields at all - an empty extraction should never look auto-approvable."""
    confidences = [
        f.get("confidence", 0.0) for f in (fields or {}).values() if isinstance(f, dict)
    ]
    return min(confidences) if confidences else 0.0


def _sender_of(mail):
    return ((mail.get("from") or {}).get("emailAddress") or {}).get("address", "")


def _extract_and_score(order, file_bytes):
    """Shared by process_attachment and reprocess_order: calls the LLM,
    parses the result, looks up the OE code, and sets order.status. Does
    NOT save() - the caller decides when to save so it can wrap this in its
    own try/except and still persist a FAILED row on error."""
    prompt = load_prompt("order_extraction")
    if settings.INCLUDE_BODY_AS_CONTEXT and order.body_text:
        prompt += "\n\nEMAIL BODY (context only):\n" + order.body_text

    raw_text = call_llm(prompt, file_bytes, order.attachment_name)
    result = parse_llm_json(raw_text)

    if result.get("is_order") is False:
        order.status = "NOT_AN_ORDER"
        return

    fields = result.get("fields", {}) or {}
    order.extracted_data = fields
    order.min_confidence = score(fields)

    oe_code, oe_confidence, oe_candidates, oe_matched = lookup_oe_code(fields)
    order.oe_code = oe_code or ""
    order.oe_confidence = oe_confidence
    order.oe_candidates = oe_candidates
    order.oe_matched = oe_matched

    auto_eligible = (
        order.min_confidence >= settings.CONFIDENCE_THRESHOLD
        and (oe_matched or not settings.REQUIRE_OE_MATCH)
    )
    order.status = "AUTO_APPROVED" if auto_eligible else "NEEDS_REVIEW"


def process_attachment(mail, att, headers):
    """One attachment -> one Order row. On failure the row is still saved
    with status=FAILED + error_message, so nothing is silently lost."""
    message_id = mail["id"]
    attachment_id = att["id"]
    attachment_name = att.get("name", "")
    file_type = attachment_name.rsplit(".", 1)[-1].lower() if "." in attachment_name else ""

    if Order.objects.filter(message_id=message_id, attachment_id=attachment_id).exists():
        return {"status": "skipped", "attachment_name": attachment_name}

    order = Order.objects.create(
        message_id=message_id,
        sender=_sender_of(mail),
        subject=mail.get("subject", ""),
        received_at=mail.get("receivedDateTime") or None,
        body_text=clean_body(mail) if settings.INCLUDE_BODY_AS_CONTEXT else "",
        attachment_id=attachment_id,
        attachment_name=attachment_name,
        file_type=file_type,
        file_size=att.get("size", 0),
        status="NEW",
    )
    log_audit(order, "EMAIL_RECEIVED", note=f"from {order.sender}, subject: {order.subject}")

    try:
        file_bytes = download_attachment(headers, message_id, attachment_id)

        if settings.S3_INPUT_BUCKET:
            # attachment_id in the key stops two same-named files in one
            # email from overwriting each other in S3
            key = f"{message_id}/{attachment_id}_{attachment_name}"
            upload_to_s3(file_bytes, key)
            order.s3_key = key
            order.save(update_fields=["s3_key"])

        order.status = "PROCESSING"
        order.save(update_fields=["status"])

        _extract_and_score(order, file_bytes)
        order.save()

        if order.status == "NOT_AN_ORDER":
            log_audit(order, "EXTRACTED", note="document is not a purchase order")
            return {"status": "not_an_order", "order_id": order.id}

        log_audit(
            order, "EXTRACTED",
            note=f"min_confidence={order.min_confidence:.2f} oe_matched={order.oe_matched}",
        )
        if order.status == "AUTO_APPROVED":
            log_audit(order, "AUTO_APPROVED", note="all fields met threshold, OE matched")

        return {"status": order.status, "order_id": order.id}

    except Exception as e:
        logger.exception("process_attachment failed for order_id=%s", order.id)
        order.status = "FAILED"
        order.error_message = str(e)
        order.save(update_fields=["status", "error_message"])
        log_audit(order, "FAILED", note=str(e))
        return {"status": "failed", "order_id": order.id, "error": str(e)}


def process_email(mail, headers):
    """One email -> N Order rows, one per usable attachment."""
    message_id = mail["id"]
    summary = {
        "attachments_found": 0, "orders_created": 0,
        "skipped": 0, "not_orders": 0, "failed": 0, "errors": [],
    }

    try:
        attachments = list_attachments(headers, message_id)
    except Exception as e:
        logger.exception("Could not list attachments for message_id=%s", message_id)
        summary["errors"].append(str(e))
        return summary

    if not attachments:
        # record ONE row so this surfaces in the review queue instead of the
        # email just disappearing (order-in-body is phase 2, see PLAN.md §13)
        already_logged = Order.objects.filter(message_id=message_id, attachment_id="").exists()
        if not already_logged:
            order = Order.objects.create(
                message_id=message_id,
                sender=_sender_of(mail),
                subject=mail.get("subject", ""),
                received_at=mail.get("receivedDateTime") or None,
                body_text=clean_body(mail) if settings.INCLUDE_BODY_AS_CONTEXT else "",
                attachment_id="",
                attachment_name="",
                status="FAILED",
                error_message="no supported attachment found",
            )
            log_audit(order, "FAILED", note="no supported attachment found")
            summary["failed"] += 1
            summary["errors"].append("no supported attachment found")
        try:
            mark_email_read(headers, message_id)
        except Exception:
            logger.exception("Could not mark message_id=%s as read", message_id)
        return summary

    summary["attachments_found"] = len(attachments)
    all_terminal = True

    for att in attachments:
        try:
            result = process_attachment(mail, att, headers)
        except Exception as e:
            # process_attachment already catches its own errors and saves a
            # FAILED row; this except only catches something even earlier
            # (e.g. Order.objects.create itself failing)
            logger.exception("Unexpected error processing attachment '%s'", att.get("name", ""))
            summary["errors"].append(str(e))
            all_terminal = False
            continue

        status = result.get("status")
        if status == "skipped":
            summary["skipped"] += 1
        elif status == "not_an_order":
            summary["not_orders"] += 1
        elif status == "failed":
            summary["failed"] += 1
            summary["errors"].append(result.get("error", ""))
        else:
            summary["orders_created"] += 1

    if all_terminal:
        try:
            mark_email_read(headers, message_id)
        except Exception:
            logger.exception("Could not mark message_id=%s as read", message_id)

    return summary


def poll_mailbox():
    """Called by the scheduler AND by POST /api/poll/. Returns a summary dict
    counted PER ATTACHMENT, not per email.

    Raises ConfigError if Outlook settings are missing - the view turns that
    into a clean 400, and the scheduler just logs it and waits for the next
    tick. Everything else (a bad email, a bad attachment) is caught and
    recorded in summary['errors'] instead of raising, so one bad message
    never stops the rest of the batch.
    """
    token = get_graph_token()          # raises ConfigError if MS_* missing
    headers = build_headers(token)
    folder_id = get_folder_id(headers)  # raises ConfigError if folder not found
    emails = fetch_new_emails(headers, folder_id)

    summary = {
        "emails_checked": len(emails), "attachments_found": 0,
        "orders_created": 0, "skipped": 0, "not_orders": 0,
        "failed": 0, "errors": [],
    }

    for mail in emails:
        try:
            email_summary = process_email(mail, headers)
        except Exception as e:
            logger.exception("process_email raised unexpectedly for message_id=%s", mail.get("id"))
            summary["errors"].append(str(e))
            continue

        for key in ("attachments_found", "orders_created", "skipped", "not_orders", "failed"):
            summary[key] += email_summary.get(key, 0)
        summary["errors"].extend(email_summary.get("errors", []))

    return summary


def reprocess_order(order):
    """Re-run extraction on an existing order, e.g. after a prompt change.
    Requires the attachment to already be in S3 - this does not re-fetch
    from Outlook, so it only works for orders processed while S3 was
    configured."""
    if not order.s3_key:
        raise ConfigError("Cannot reprocess: no s3_key stored for this order")

    client = s3_client()
    obj = client.get_object(Bucket=settings.S3_INPUT_BUCKET, Key=order.s3_key)
    file_bytes = obj["Body"].read()

    order.status = "PROCESSING"
    order.save(update_fields=["status"])

    _extract_and_score(order, file_bytes)
    order.save()

    log_audit(order, "EXTRACTED", note=f"reprocessed, now {order.status}")
    return order


# =============================================================================
# Section F - SAP CSV export
# =============================================================================
#
# PLACEHOLDER LAYOUT. SAP_CSV_COLUMNS and build_sap_rows() below use
# reasonable guessed column names, NOT a real SAP import spec - nobody has
# supplied one yet. Before this feeds a real SAP import, replace both with
# the actual column names, column order, and date/decimal format your SAP
# import expects. Everything else here (the CSV writing, the S3 upload, the
# audit log) stays the same regardless of what the columns are called.
#
# One row per LINE ITEM, not per order - a common SAP flat-file shape where
# header fields (PO number, customer, ship-to) repeat on every item line.
# An order with no line items still gets one row, with the item columns
# blank, so it isn't silently dropped from the export.

SAP_CSV_COLUMNS = [
    "SoldToParty",          # order.oe_code - the vector-matched OE code
    "CustomerPO",
    "CustomerName",
    "OrderDate",
    "RequestedDeliveryDate",
    "Currency",
    "Incoterms",
    "ShipToAddress",
    "ShipToCountry",
    "TotalAmount",
    "MaterialCode",
    "Description",
    "Quantity",
    "UoM",
    "UnitPrice",
    "SourceOrderId",        # this Order's id - traceability back to OPTIC BOT
    "SourceAttachment",
]


def build_sap_rows(order):
    """One Order -> a list of plain dicts, one per line item, keyed by
    SAP_CSV_COLUMNS. oe_code (never present in extracted_data - see
    lookup_oe_code) is read straight off the order."""
    fields = order.extracted_data or {}

    def val(name, default=""):
        value = (fields.get(name) or {}).get("value", default)
        return value if value not in (None, "") else default

    header = {
        "SoldToParty": order.oe_code,
        "CustomerPO": val("customer_po_number"),
        "CustomerName": val("customer_name"),
        "OrderDate": val("order_date"),
        "RequestedDeliveryDate": val("requested_delivery_date"),
        "Currency": val("currency"),
        "Incoterms": val("incoterms"),
        "ShipToAddress": val("ship_to_address"),
        "ShipToCountry": val("ship_to_country"),
        "TotalAmount": val("total_amount"),
        "SourceOrderId": order.id,
        "SourceAttachment": order.attachment_name,
    }

    line_items = (fields.get("line_items") or {}).get("value") or []
    if not line_items:
        return [dict(header, MaterialCode="", Description="", Quantity="", UoM="", UnitPrice="")]

    rows = []
    for item in line_items:
        row = dict(header)
        row["MaterialCode"] = item.get("material_code", "")
        row["Description"] = item.get("description", "")
        row["Quantity"] = item.get("quantity", "")
        row["UoM"] = item.get("uom", "")
        row["UnitPrice"] = item.get("unit_price", "")
        rows.append(row)
    return rows


def export_order_to_csv(order):
    """Builds the SAP CSV for one order and uploads it to S3_OUTPUT_BUCKET.
    Only APPROVED/AUTO_APPROVED orders should reach here - the view checks
    status before calling this. Can be called more than once (e.g. after a
    field correction); each call overwrites the same S3 key and updates
    exported_at, so the CSV always reflects the order's current fields.
    Returns the S3 key."""
    require("S3_OUTPUT_BUCKET")

    rows = build_sap_rows(order)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SAP_CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    # utf-8-sig: adds a BOM so Excel/SAP open the file as UTF-8 instead of
    # misreading special characters as a different encoding.
    csv_bytes = buffer.getvalue().encode("utf-8-sig")

    po_number = (order.extracted_data.get("customer_po_number") or {}).get("value") or "no-po"
    safe_po = re.sub(r"[^A-Za-z0-9_-]+", "_", str(po_number))
    date_prefix = timezone.now().strftime("%Y-%m-%d")
    key = f"exports/{date_prefix}/order-{order.id}-{safe_po}.csv"

    upload_to_s3(csv_bytes, key, bucket=settings.S3_OUTPUT_BUCKET)

    order.exported_at = timezone.now()
    order.export_s3_key = key
    order.save(update_fields=["exported_at", "export_s3_key"])

    log_audit(order, "EXPORTED", note=f"uploaded to s3://{settings.S3_OUTPUT_BUCKET}/{key}")
    return key
