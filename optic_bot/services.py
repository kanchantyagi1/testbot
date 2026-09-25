"""
OPTIC BOT pipeline - Outlook polling, S3 upload, LLM extraction, OE lookup.

One file, seven sections:
    A. config guard      - require() raises a clear error instead of a crash
    B. Outlook / Graph    - MSAL auth + reading the shared mailbox
    C. S3 + OE lookup     - upload attachments, ask the RAG agent for an OE code
    D. LLM extraction     - Bedrock/gateway converse call + JSON parsing
    E. the pipeline       - ties everything together into Order rows
    F. SAP CSV export     - approved order(s) -> CSV -> S3_OUTPUT_BUCKET
    G. SQS queue          - ingest -> execution queue -> worker -> DLQ

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
from django.db import connection
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


def aws_credential_kwargs():
    """Static keys are OPTIONAL, not required. If AWS_ACCESS_KEY_ID/
    AWS_SECRET_ACCESS_KEY are set in .env, use them explicitly (useful for
    local dev with no other credential source). If blank, return {} so
    boto3 falls back to its own default chain - environment variables,
    ~/.aws/credentials, or (in production, per PLAN.md's security section)
    the EC2 instance's IAM role. That is what lets this same code run
    correctly both on a laptop with an AWS CLI profile and on EC2 with no
    static keys anywhere.

    Deliberately does NOT call require() - an empty dict is a valid,
    working boto3 configuration whenever another credential source
    exists. If truly nothing is configured anywhere, boto3 itself raises
    NoCredentialsError at call time, which the caller's normal exception
    handling already turns into a FAILED order / clean error response.
    """
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        return {
            "aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
            "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
        }
    return {}


def aws_credentials_status():
    """A REAL check, not a presence check. Since static keys are optional
    (aws_credential_kwargs() above), checking whether AWS_ACCESS_KEY_ID is
    set no longer tells you whether AWS calls will work - the environment's
    default credential chain (CLI profile, instance role) may cover it
    instead. This makes one free STS call to find out for certain. Used by
    GET /api/health/.
    """
    try:
        import boto3
        client = boto3.client("sts", region_name=settings.AWS_REGION, **aws_credential_kwargs())
        identity = client.get_caller_identity()
        return f"ok (account {identity.get('Account')})"
    except Exception as e:
        return f"no AWS credentials available: {e}"


def oe_master_status():
    """A REAL check for GET /api/health/: is the pgvector extension
    installed and does oe_master actually have rows? Cheap (two tiny
    queries on the same connection Django already has open) - this is
    what tells you the difference between "not configured" and "I built
    the table but forgot to load it"."""
    if not settings.OE_MATCHING_ENABLED:
        return "disabled (OE_MATCHING_ENABLED=False)"
    if "postgresql" not in connection.settings_dict.get("ENGINE", ""):
        return "not_configured: DB_ENGINE must be postgres for pgvector"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            if not cursor.fetchone():
                return "pgvector extension not installed on this database"
            cursor.execute("SELECT count(*) FROM oe_master WHERE is_active")
            count = cursor.fetchone()[0]
        return f"ok ({count} active row(s))" if count else "oe_master table is empty"
    except Exception as e:
        return f"oe_master not queryable: {e}"


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

    # NO SIZE FILTERING. An order attachment may be 2 KB or 50 MB and is
    # processed either way - size is never a reason to drop a file. The
    # only things excluded here are non-files, inline signature images,
    # and extensions the pipeline cannot read at all.
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

        logger.info("Keeping attachment '%s' (%d bytes)", name, size)
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
    import boto3  # imported here, not at module level - see file docstring

    return boto3.client("s3", region_name=settings.AWS_REGION, **aws_credential_kwargs())


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


def get_query_embedding(text):
    """Embeds `text` with Bedrock (OE_EMBEDDING_MODEL_ID, default Titan v2
    1024-dim) for the pgvector query below. MUST be the same model and
    dimension oe_master.embedding was populated with, or every cosine
    score is meaningless - see PLAN.md Appendix A.3.

    Uses invoke_model, not converse - embeddings are a different Bedrock
    API shape from the chat models the rest of this file calls.
    """
    import boto3  # imported here, not at module level - see file docstring

    client = boto3.client(
        "bedrock-runtime", region_name=settings.AWS_REGION, **aws_credential_kwargs()
    )
    body = json.dumps({
        "inputText": text,
        "dimensions": settings.OE_EMBEDDING_DIMENSION,
        "normalize": True,
    })
    response = client.invoke_model(modelId=settings.OE_EMBEDDING_MODEL_ID, body=body)
    payload = json.loads(response["body"].read())
    return payload["embedding"]


def build_product_label(brand_name, variant_name, lens_type=None):
    """The one place that turns master-data columns into a readable
    label, e.g. "1-DAY DEFINE WITH LACREON ACCENT" - used both for the
    candidate's display name and (by whoever populates oe_master) as the
    basis for embedding_text. Keep this in sync with however the table
    was actually embedded - if the two diverge, matches degrade silently
    with no error. See PLAN.md Appendix A.3.
    """
    parts = [p for p in (brand_name, variant_name, lens_type) if p]
    return " ".join(parts)


def query_oe_master(embedding, top_k):
    """Direct cosine-similarity search against oe_master - the exact
    table PLAN.md Appendix A defines, matching the REAL master-data
    columns (Product Type, OE Code, UOM, Fam Code, Base Curve, Brand,
    Brand Name, Variant Name, Type) - see reference-images/
    masterdata_embeddings.jpeg. Runs on the SAME Postgres connection
    Django already has open (DATABASES['default']). No separate service,
    no network hop - just SQL, via django.db.connection.

    Requires DB_ENGINE=postgres with the pgvector extension and the
    oe_master table created there. Raises on any DB error (missing
    table/extension, wrong dimension, etc.) - the caller (lookup_oe_code)
    turns that into a clean reason string rather than letting it crash
    the order.
    """
    # pgvector's wire format for a literal vector: "[0.1,0.2,...]" cast to
    # ::vector in SQL. Avoids adding the pgvector-python package just for
    # this one query.
    vector_literal = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    sql = """
        SELECT product_type, oe_code, uom, fam_code, base_curve, brand,
               brand_name, variant_name, type,
               1 - (embedding <=> %s::vector) AS score
        FROM oe_master
        WHERE is_active
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [vector_literal, vector_literal, top_k])
        columns = [c[0] for c in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    return [
        {
            "oe_code": row["oe_code"],
            "name": build_product_label(row.get("brand_name"), row.get("variant_name"), row.get("type")),
            "uom": row.get("uom"),
            "product_type": row.get("product_type"),
            "fam_code": row.get("fam_code"),
            # NUMERIC(4,2) comes back from psycopg2 as Decimal, which
            # json.dumps() rejects outright - found live, when
            # rerank_oe_candidates() tried to serialize a real candidate
            # list for the first time. float() here, not str(), so
            # base_curve is still usable for numeric comparison downstream.
            "base_curve": float(row["base_curve"]) if row.get("base_curve") is not None else None,
            "brand": row.get("brand"),
            "score": round(float(row["score"]), 4),
            "match_type": "pgvector-cosine",
        }
        for row in rows
    ]


def lookup_oe_code(fields):
    """Direct pgvector match against oe_master (query_oe_master() above),
    then ALWAYS ask an LLM to explain the decision - not just when the
    vector score is ambiguous. Every order that reaches here gets a
    plain-English reason for its OE code (or lack of one), because that
    reason is what an auditor reads later, not a bare similarity number.
    See PLAN.md Appendix A for the oe_master schema and §16 for this
    reasoning step.

    Set OE_RERANK_ENABLED=False to skip the LLM call entirely and fall
    back to a bare OE_MATCH_THRESHOLD check on the vector score - faster
    and free, but with no reason recorded. Set OE_MATCHING_ENABLED=False
    to skip pgvector entirely (e.g. before the table exists).

    Returns (oe_code, oe_confidence, candidates_list, matched_bool, reason).
    reason is always a non-empty string. Never raises - a lookup failure
    must not fail the whole order; it just leaves the order without an OE
    code, which sends it to review.
    """
    if not settings.OE_MATCHING_ENABLED:
        return (None, 0.0, [], False, "OE_MATCHING_ENABLED is False")

    # The PRODUCT description is what actually needs fuzzy matching -
    # practices write "Acuvus oasys astig", "Oasys Dailies", "Daily 1 Day
    # Oasys 90pk" for the same SAP product. See PLAN.md §17.5. Embed the
    # first line item's description - one order, one embedding query;
    # per-line-item OE codes are a documented Phase 2 item.
    products = [
        ((item or {}).get("product_description") or {}).get("value")
        for item in ((fields or {}).get("line_items") or [])
    ]
    products = [p for p in products if p]

    if not products:
        return (None, 0.0, [], False, "no product description was extracted to match on")

    try:
        embedding = get_query_embedding(products[0])
        candidates = query_oe_master(embedding, settings.OE_TOP_K)

        if not candidates:
            return (None, 0.0, [], False, "oe_master returned no candidates")

        if not settings.OE_RERANK_ENABLED:
            top = candidates[0]
            if top.get("score", 0.0) >= settings.OE_MATCH_THRESHOLD:
                return (
                    top.get("oe_code"), top.get("score", 0.0), candidates, True,
                    f"vector score {top.get('score', 0.0):.2f} >= OE_MATCH_THRESHOLD "
                    f"{settings.OE_MATCH_THRESHOLD} (LLM reasoning disabled)",
                )
            return (
                None, 0.0, candidates, False,
                f"top vector score {top.get('score', 0.0):.2f} below OE_MATCH_THRESHOLD "
                f"{settings.OE_MATCH_THRESHOLD} (LLM reasoning disabled)",
            )

        # ALWAYS reason over the candidates - every order gets an
        # explanation, not just the ambiguous ones. The vector score is
        # still passed to the LLM as context (see slim_candidates in
        # rerank_oe_candidates), so a clean 0.97 top score should still
        # come back as a confident LLM match - it's just explained now.
        oe_code, confidence, reason = rerank_oe_candidates(fields, candidates)

        if oe_code and confidence >= settings.OE_RERANK_THRESHOLD:
            return (oe_code, confidence, candidates, True, reason)

        return (None, 0.0, candidates, False, reason)

    except Exception as e:
        logger.exception("OE pgvector lookup failed")
        return (None, 0.0, [], False, f"OE pgvector lookup failed: {e}")


def rerank_oe_candidates(fields, candidates):
    """Ask the LLM to pick the best OE PRODUCT match among oe_master's
    top-k candidates, using order context the vector search doesn't weigh
    directly (base curve match, lens type consistency, trial vs full
    pack), and to explain why. Called from lookup_oe_code() for every
    lookup that has at least one candidate (see PLAN.md §16) - not only
    ambiguous ones - so every order carries a reason, which is what
    auditing needs.

    Uses call_llm(prompt_text) with NO file - a text-only reasoning call,
    not a document extraction (Section D below). Never raises: any failure
    here (LLM not configured, bad JSON, network error) just means "no
    rerank match", and lookup_oe_code() falls back to human review exactly
    as if this function didn't exist - the reason string still explains why.

    Returns (oe_code_or_None, confidence, reason_string). reason is always
    a non-empty string, even on failure or "no match".
    """
    try:
        header = (fields or {}).get("order") or {}

        def value_of(name):
            return (header.get(name) or {}).get("value")

        first_item = ((fields or {}).get("line_items") or [{}])[0] or {}
        right_eye = first_item.get("right_eye") or {}

        order_context = {
            "product_descriptions": [
                ((item or {}).get("product_description") or {}).get("value")
                for item in ((fields or {}).get("line_items") or [])
            ],
            # pack_size is a REQUIRED exact match against the candidate's
            # uom (e.g. "30P"/"1P"/"10P"), not a fuzzy signal - see
            # prompts/oe_rerank.txt and PLAN.md Appendix A.1
            "pack_size": (first_item.get("pack_size") or {}).get("value"),
            "base_curve": (right_eye.get("base_curve") or {}).get("value"),
            "trial_only": value_of("trial_only"),
        }
        # only pass the fields a reviewer/LLM actually needs to compare -
        # not the whole candidate dict verbatim, in case oe_master gains
        # extra columns later
        slim_candidates = [
            {
                "oe_code": c.get("oe_code"),
                "name": c.get("name"),
                "uom": c.get("uom"),
                "base_curve": c.get("base_curve"),
                "product_type": c.get("product_type"),
                "score": c.get("score"),
            }
            for c in candidates
        ]

        prompt = load_prompt("oe_rerank").format(
            order_context=json.dumps(order_context, indent=2),
            candidates=json.dumps(slim_candidates, indent=2),
        )
        raw_text = call_llm(prompt)  # no file_bytes - text-only reasoning call
        result = parse_llm_json(raw_text)

        if not result.get("match"):
            return (None, 0.0, result.get("reason") or "LLM found no confident match")

        candidate_codes = {c.get("oe_code") for c in candidates}
        oe_code = result.get("oe_code")
        if oe_code not in candidate_codes:
            # the model invented a code that wasn't offered - never trust it
            return (None, 0.0, f"LLM returned oe_code '{oe_code}' not in candidate list, ignored")

        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        return (oe_code, confidence, result.get("reason") or "LLM matched but gave no reason text")

    except Exception as e:
        logger.exception("OE rerank failed")
        return (None, 0.0, f"rerank failed: {e}")


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

    # This is NOT a filter - the attachment was already downloaded and
    # stored in S3. It only converts "provider rejected a huge payload"
    # into a message a human can act on. The order is saved as FAILED with
    # this text, so a 50 MB scan shows up in the review queue asking for
    # manual entry instead of vanishing or throwing a cryptic AWS error.
    limit_bytes = settings.LLM_MAX_DOCUMENT_MB * 1024 * 1024
    if len(file_bytes) > limit_bytes:
        raise ValueError(
            f"Attachment '{file_name}' is {len(file_bytes) / 1024 / 1024:.1f} MB, "
            f"above the {settings.LLM_MAX_DOCUMENT_MB} MB the LLM accepts in one call. "
            f"The file is saved in S3 - it needs manual entry, or split into smaller files."
        )

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


def _check_not_truncated(stop_reason):
    """Found live: an 8-line-item order's response was cut off mid-string
    at the old 4096-token default, and parse_llm_json() reported a
    confusing 'Unterminated string' JSONDecodeError instead of the real
    cause. The extraction schema is deliberately verbose (3 eye objects x
    7 fields x {value, confidence} per line item), so a large multi-item
    order can need a lot of output tokens. Catch the truncation here,
    right where the actual signal (stopReason) is available, and raise an
    error that names the fix instead of a downstream JSON parse mystery.
    """
    if stop_reason == "max_tokens":
        raise ValueError(
            f"LLM response was truncated at LLM_MAX_TOKENS={settings.LLM_MAX_TOKENS} "
            f"before finishing its JSON - raise LLM_MAX_TOKENS in .env (this order likely "
            f"has many line items or a long body). Not a JSON formatting problem."
        )


def call_llm(prompt_text, file_bytes=None, file_name=None):
    """One function, an if/else on settings.LLM_PROVIDER. Same converse
    payload shape either way - the document block (when there is a file)
    goes BEFORE the text block, so the model reads the file, then the
    instruction.

    file_bytes=None (the default) sends a TEXT-ONLY message - no document
    block at all. Used by the OE re-ranking prompt in Section C, which
    reasons over candidate text, not a file.
    """
    provider = settings.LLM_PROVIDER
    content = []
    if file_bytes is not None:
        content.append(build_content_block(file_bytes, file_name, provider=provider))
    content.append({"text": prompt_text})

    if provider == "bedrock":
        import boto3  # imported here, not at module level - see file docstring

        client = boto3.client(
            "bedrock-runtime", region_name=settings.AWS_REGION, **aws_credential_kwargs()
        )
        response = client.converse(
            modelId=settings.BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": settings.LLM_MAX_TOKENS, "temperature": 0},
        )
        _check_not_truncated(response.get("stopReason"))
        return response["output"]["message"]["content"][0]["text"]

    if provider == "gateway":
        require("LLM_GATEWAY_URL", "LLM_GATEWAY_API_KEY")
        url = settings.LLM_GATEWAY_URL.format(model=settings.BEDROCK_MODEL_ID)
        payload = {
            "messages": [{"role": "user", "content": content}],
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
        _check_not_truncated(data.get("stopReason"))
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


def classify_email(mail, attachment_names):
    """ORDER or COMMUNICATION? One cheap text-only LLM call on the
    subject/body/attachment names, made BEFORE any attachment is
    downloaded or extracted - so delivery chasers, invoice queries,
    complaints and out-of-office replies never reach the expensive
    extraction step. See prompts/email_classification.txt.

    FAILS OPEN on purpose: if the classifier is disabled, unavailable, or
    returns junk, the email is treated as an ORDER. Paying to extract a
    communication email is cheap and gets caught downstream; discarding a
    real order is a lost order.

    Returns (is_order_bool, confidence, reason).
    """
    if not settings.EMAIL_CLASSIFICATION_ENABLED:
        return (True, 0.0, "email classification disabled")

    try:
        prompt = load_prompt("email_classification").format(
            sender=_sender_of(mail) or "(unknown)",
            subject=mail.get("subject") or "(no subject)",
            attachment_names=", ".join(attachment_names) if attachment_names else "(none)",
            body=clean_body(mail) or "(empty)",
        )
        result = parse_llm_json(call_llm(prompt))  # text-only, no document

        classification = str(result.get("classification", "")).strip().upper()
        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = result.get("reason") or "no reason given"

        # only skip the email when the classifier is CONFIDENT it is not an
        # order - an unsure "COMMUNICATION" still goes through extraction
        if (
            classification == "COMMUNICATION"
            and confidence >= settings.EMAIL_CLASSIFICATION_THRESHOLD
        ):
            return (False, confidence, reason)

        return (True, confidence, reason)

    except Exception as e:
        logger.exception("Email classification failed")
        return (True, 0.0, f"classification failed, processed as order: {e}")


# =============================================================================
# Section E - the pipeline
# =============================================================================

def collect_confidences(node, skip_null_fields=True):
    """Walk the nested extraction result and yield every leaf confidence.

    The extraction JSON nests: order.account_number, line_items[].right_eye
    .sphere, and so on - so this recurses rather than looking one level
    deep. A leaf is any dict with a "confidence" key.

    skip_null_fields=True ignores leaves whose value is null. Those are
    fields the order simply didn't mention (a spherical lens has no
    cylinder, a stock order has no patient), and they always carry
    confidence 0.0 - counting them would drag min_confidence to 0.0 on
    every single order and make the threshold meaningless.
    """
    if isinstance(node, dict):
        if "confidence" in node and "value" in node:
            if skip_null_fields and node.get("value") in (None, ""):
                return
            try:
                yield float(node.get("confidence", 0.0))
            except (TypeError, ValueError):
                yield 0.0
            return
        for child in node.values():
            yield from collect_confidences(child, skip_null_fields)
    elif isinstance(node, list):
        for child in node:
            yield from collect_confidences(child, skip_null_fields)


def score(fields):
    """Lowest confidence across every POPULATED extracted field. 0.0 if
    nothing was extracted at all - an empty extraction should never look
    auto-approvable."""
    confidences = list(collect_confidences(fields))
    return min(confidences) if confidences else 0.0


def collect_low_confidence_paths(node, threshold, prefix=""):
    """Dotted paths of every populated leaf scoring below `threshold`,
    e.g. "order.account_number" or "line_items.0.right_eye.sphere".

    The reviewer UI uses these to highlight exactly which fields to check,
    and the same path is what PATCH /fields/ accepts to correct one.
    """
    if isinstance(node, dict):
        if "confidence" in node and "value" in node:
            if node.get("value") in (None, ""):
                return
            try:
                confidence = float(node.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < threshold:
                yield prefix
            return
        for key, child in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from collect_low_confidence_paths(child, threshold, path)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            path = f"{prefix}.{index}" if prefix else str(index)
            yield from collect_low_confidence_paths(child, threshold, path)


def set_by_path(data, path, value):
    """Set a nested leaf from a dotted path, creating the wrapper dict if
    needed. Returns the previous value. Used by the human-edit endpoint so
    a reviewer can correct "line_items.0.right_eye.sphere" directly.

    Numeric segments index into lists, everything else into dicts.
    """
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current.setdefault(part, {})

    last = parts[-1]
    if isinstance(current, list):
        index = int(last)
        old_leaf = current[index] if index < len(current) else {}
        old_value = old_leaf.get("value") if isinstance(old_leaf, dict) else old_leaf
        current[index] = {"value": value, "confidence": 1.0, "edited": True}
    else:
        old_leaf = current.get(last) or {}
        old_value = old_leaf.get("value") if isinstance(old_leaf, dict) else old_leaf
        current[last] = {"value": value, "confidence": 1.0, "edited": True}
    return old_value


def _sender_of(mail):
    return ((mail.get("from") or {}).get("emailAddress") or {}).get("address", "")


def _extract_and_score(order, file_bytes=None):
    """Shared by process_attachment, process_body_order and
    reprocess_order: calls the LLM, parses the result, looks up the OE
    code, and sets order.status. Does NOT save() - the caller decides when
    to save so it can wrap this in its own try/except and still persist a
    FAILED row on error.

    file_bytes=None means the order is in the EMAIL BODY itself with no
    order attachment - a very common case in this mailbox (practices type
    the Rx straight into the email), so the LLM gets a text-only call.
    The body is always passed in the prompt either way, because an
    attachment order often carries its account number or shipping note
    only in the body.
    """
    prompt = load_prompt("order_extraction").format(
        sender=order.sender or "(unknown)",
        subject=order.subject or "(no subject)",
        body=(order.body_text or "(empty)") if settings.INCLUDE_BODY_AS_CONTEXT else "(not provided)",
    )

    raw_text = call_llm(prompt, file_bytes, order.attachment_name or None)
    result = parse_llm_json(raw_text)

    if result.get("is_order") is False:
        order.status = "NOT_AN_ORDER"
        return

    # keep the whole nested shape - order header + line items + notes -
    # so the reviewer sees every field the LLM could find, which is what
    # makes a failed OE match recoverable by a human
    fields = {
        "order": result.get("order") or {},
        "line_items": result.get("line_items") or [],
        "extraction_notes": result.get("extraction_notes", ""),
    }
    order.extracted_data = fields
    order.min_confidence = score(fields)

    oe_code, oe_confidence, oe_candidates, oe_matched, oe_reason = lookup_oe_code(fields)
    order.oe_code = oe_code or ""
    order.oe_confidence = oe_confidence
    order.oe_candidates = oe_candidates
    order.oe_matched = oe_matched
    order.oe_match_reason = oe_reason
    # the winning candidate may also carry the SAP unit of measure
    # ("5P" for a 30-pack) - it fills SAPUoM_Right/Left in the export
    order.oe_uom = ""
    for candidate in oe_candidates or []:
        if candidate.get("oe_code") == oe_code and candidate.get("uom"):
            order.oe_uom = str(candidate["uom"])
            break

    auto_eligible = (
        order.min_confidence >= settings.CONFIDENCE_THRESHOLD
        and (oe_matched or not settings.REQUIRE_OE_MATCH)
    )
    order.status = "AUTO_APPROVED" if auto_eligible else "NEEDS_REVIEW"


def ingest_attachment(mail, att, headers):
    """INGEST half: create the Order row and put the raw document in S3.

    Stops there. It does NOT call the LLM. In SQS mode the message is
    queued after this and a worker does the extraction; in inline mode
    process_attachment() carries straight on. Splitting it this way is
    what lets both modes share exactly the same ingest code.

    Returns (order, file_bytes) or (None, None) when the attachment was
    already ingested by an earlier poll.
    """
    message_id = mail["id"]
    attachment_id = att["id"]
    attachment_name = att.get("name", "")
    file_type = attachment_name.rsplit(".", 1)[-1].lower() if "." in attachment_name else ""

    if Order.objects.filter(message_id=message_id, attachment_id=attachment_id).exists():
        return (None, None)

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

    file_bytes = download_attachment(headers, message_id, attachment_id)

    if settings.S3_INPUT_BUCKET:
        # attachment_id in the key stops two same-named files in one
        # email from overwriting each other in S3
        key = f"{message_id}/{attachment_id}_{attachment_name}"
        upload_to_s3(file_bytes, key)
        order.s3_key = key
        order.save(update_fields=["s3_key"])

    return (order, file_bytes)


def run_extraction(order, file_bytes):
    """PROCESSING half: LLM extract -> OE lookup -> score -> save.

    Shared by inline mode and the SQS worker. Raises on failure so the
    caller decides what that means - inline mode saves a FAILED row, the
    SQS worker additionally leaves the message on the queue for retry.
    """
    order.status = "PROCESSING"
    order.save(update_fields=["status"])

    _extract_and_score(order, file_bytes)
    order.save()

    if order.status == "NOT_AN_ORDER":
        log_audit(order, "EXTRACTED", note="document is not a purchase order")
        return {"status": "not_an_order", "order_id": order.id}

    log_audit(
        order, "EXTRACTED",
        note=(
            f"min_confidence={order.min_confidence:.2f} oe_matched={order.oe_matched} "
            f"oe_reason={order.oe_match_reason}"
        ),
    )
    if order.status == "AUTO_APPROVED":
        log_audit(order, "AUTO_APPROVED", note="all fields met threshold, OE matched")

    return {"status": order.status, "order_id": order.id}


def _mark_failed(order, error):
    order.status = "FAILED"
    order.error_message = str(error)
    order.save(update_fields=["status", "error_message"])
    log_audit(order, "FAILED", note=str(error))


def process_attachment(mail, att, headers):
    """INLINE mode: one attachment -> one Order row, extracted immediately.
    On failure the row is still saved with status=FAILED + error_message,
    so nothing is silently lost. (SQS mode uses ingest_attachment() +
    enqueue_order() instead - see Section G.)"""
    order = None
    try:
        order, file_bytes = ingest_attachment(mail, att, headers)
        if order is None:
            return {"status": "skipped", "attachment_name": att.get("name", "")}
        return run_extraction(order, file_bytes)

    except Exception as e:
        logger.exception("process_attachment failed")
        if order is not None:
            _mark_failed(order, e)
            return {"status": "failed", "order_id": order.id, "error": str(e)}
        return {"status": "failed", "error": str(e)}


def queue_attachment(mail, att, headers):
    """SQS mode: ingest the attachment (row + S3) and put ONE MESSAGE on
    the execution queue. No LLM call happens here - the worker does that.
    Same ingest code as inline mode, so the two modes cannot drift."""
    order = None
    try:
        order, _file_bytes = ingest_attachment(mail, att, headers)
        if order is None:
            return {"status": "skipped", "attachment_name": att.get("name", "")}

        enqueue_order(order)
        return {"status": "queued", "order_id": order.id}

    except Exception as e:
        logger.exception("queue_attachment failed")
        if order is not None:
            _mark_failed(order, e)
            return {"status": "failed", "order_id": order.id, "error": str(e)}
        return {"status": "failed", "error": str(e)}


def process_body_order(mail, headers):
    """The order is typed into the EMAIL BODY with no order attachment.

    This is not an edge case in this mailbox - "Can I please order a top
    up of our store supply... -0.75 x 3, -1.00 x 5", "Trials for Wayne
    Ying / R -7.00 / -1.75 x 20" arrive constantly with nothing attached
    but a signature image. Treated as a first-class order, with
    attachment_id="BODY" so the (message_id, attachment_id) dedupe guard
    still works.
    """
    message_id = mail["id"]

    if Order.objects.filter(message_id=message_id, attachment_id="BODY").exists():
        return {"status": "skipped", "attachment_name": "(email body)"}

    order = Order.objects.create(
        message_id=message_id,
        sender=_sender_of(mail),
        subject=mail.get("subject", ""),
        received_at=mail.get("receivedDateTime") or None,
        body_text=clean_body(mail),
        attachment_id="BODY",
        attachment_name="(email body)",
        file_type="body",
        file_size=0,
        status="NEW",
    )
    log_audit(order, "EMAIL_RECEIVED", note=f"order in email body from {order.sender}")

    try:
        if settings.USE_SQS:
            # same path as attachments - the worker extracts it, and
            # _load_file_bytes() returns None for a BODY order
            enqueue_order(order)
            return {"status": "queued", "order_id": order.id}

        order.status = "PROCESSING"
        order.save(update_fields=["status"])

        _extract_and_score(order, file_bytes=None)  # text-only, no document
        order.save()

        if order.status == "NOT_AN_ORDER":
            log_audit(order, "EXTRACTED", note="email body is not an order")
            return {"status": "not_an_order", "order_id": order.id}

        log_audit(
            order, "EXTRACTED",
            note=(
                f"body order, min_confidence={order.min_confidence:.2f} "
                f"oe_matched={order.oe_matched} oe_reason={order.oe_match_reason}"
            ),
        )
        if order.status == "AUTO_APPROVED":
            log_audit(order, "AUTO_APPROVED", note="all fields met threshold, OE matched")

        return {"status": order.status, "order_id": order.id}

    except Exception as e:
        logger.exception("process_body_order failed for order_id=%s", order.id)
        order.status = "FAILED"
        order.error_message = str(e)
        order.save(update_fields=["status", "error_message"])
        log_audit(order, "FAILED", note=str(e))
        return {"status": "failed", "order_id": order.id, "error": str(e)}


def _record_communication_email(mail, reason, confidence):
    """A classified-out communication email still gets ONE row, so the
    mailbox is fully auditable and a misclassification is visible and
    recoverable rather than silently dropped."""
    message_id = mail["id"]
    if Order.objects.filter(message_id=message_id, attachment_id="EMAIL").exists():
        return None

    order = Order.objects.create(
        message_id=message_id,
        sender=_sender_of(mail),
        subject=mail.get("subject", ""),
        received_at=mail.get("receivedDateTime") or None,
        body_text=clean_body(mail),
        attachment_id="EMAIL",
        attachment_name="(email)",
        file_type="email",
        status="NOT_AN_ORDER",
        error_message=f"classified as communication ({confidence:.2f}): {reason}",
    )
    log_audit(order, "EXTRACTED", note=f"classified as COMMUNICATION ({confidence:.2f}): {reason}")
    return order


def process_email(mail, headers):
    """One email -> N Order rows: one per usable attachment, or one for
    the email body when the order is typed into the email itself."""
    message_id = mail["id"]
    summary = {
        "attachments_found": 0, "orders_created": 0, "queued": 0,
        "skipped": 0, "not_orders": 0, "failed": 0, "errors": [],
    }

    try:
        attachments = list_attachments(headers, message_id)
    except Exception as e:
        logger.exception("Could not list attachments for message_id=%s", message_id)
        summary["errors"].append(str(e))
        return summary

    # Triage BEFORE downloading or extracting anything - see
    # prompts/email_classification.txt. Fails open (treats as order).
    is_order_email, class_confidence, class_reason = classify_email(
        mail, [a.get("name", "") for a in attachments]
    )
    if not is_order_email:
        _record_communication_email(mail, class_reason, class_confidence)
        summary["not_orders"] += 1
        try:
            mark_email_read(headers, message_id)
        except Exception:
            logger.exception("Could not mark message_id=%s as read", message_id)
        return summary

    if not attachments:
        # No order attachment - the order is in the body itself.
        result = process_body_order(mail, headers)
        status = result.get("status")
        if status == "skipped":
            summary["skipped"] += 1
        elif status == "not_an_order":
            summary["not_orders"] += 1
        elif status == "failed":
            summary["failed"] += 1
            summary["errors"].append(result.get("error", ""))
        elif status == "queued":
            summary["queued"] += 1
        else:
            summary["orders_created"] += 1

        try:
            mark_email_read(headers, message_id)
        except Exception:
            logger.exception("Could not mark message_id=%s as read", message_id)
        return summary

    summary["attachments_found"] = len(attachments)
    all_terminal = True

    for att in attachments:
        try:
            result = (
                queue_attachment(mail, att, headers)
                if settings.USE_SQS
                else process_attachment(mail, att, headers)
            )
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
        elif status == "queued":
            summary["queued"] += 1
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
        "orders_created": 0, "queued": 0, "skipped": 0, "not_orders": 0,
        "failed": 0, "errors": [],
    }

    for mail in emails:
        try:
            email_summary = process_email(mail, headers)
        except Exception as e:
            logger.exception("process_email raised unexpectedly for message_id=%s", mail.get("id"))
            summary["errors"].append(str(e))
            continue

        for key in ("attachments_found", "orders_created", "queued", "skipped", "not_orders", "failed"):
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

    log_audit(
        order, "EXTRACTED",
        note=f"reprocessed, now {order.status}, oe_reason={order.oe_match_reason}",
    )
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

# Exactly the 29 columns from the "Required Fields" sheet, in that order.
# NOTE ON THE "1" SUFFIX: columns ending in 1 (AddPower1, Axis1, BaseCurve1,
# Cylinder1, Descriptions1, OrderQuantity1, Sphere1) are the RIGHT eye, and
# the unsuffixed ones (AddPower, Axis, ...) are the LEFT eye. The sample
# JSON had identical values for both eyes so this could not be confirmed
# from the data - it follows the ordering convention (right listed first,
# SAPOECode_Right before SAPOECode_Left). CONFIRM BEFORE GO-LIVE; if it is
# the other way round, swap RIGHT_EYE_SUFFIX/LEFT_EYE_SUFFIX below and
# nothing else changes.
RIGHT_EYE_SUFFIX = "1"
LEFT_EYE_SUFFIX = ""

SAP_CSV_COLUMNS = [
    "AccountNumber",
    "CustomerAddress",
    "CustomerName",
    "DTPOrder",
    "DTPAddressExport",
    "DTPPatientName",
    "AddPower1",            # right eye
    "Axis1",
    "BaseCurve1",
    "Cylinder1",
    "Descriptions1",
    "OrderQuantity1",
    "Sphere1",
    "OrderType",
    "PONumber",
    "PatientName",
    "AddPower",             # left eye
    "Axis",
    "BaseCurve",
    "Cylinder",
    "Descriptions",
    "OrderQuantity",
    "Sphere",
    "Special_Instructions",
    "T_TrialOnly",
    "SAPOECode_Right",      # from the product/OE lookup, not the LLM
    "SAPUoM_Right",
    "SAPOECode_Left",
    "SAPUoM_Left",
]

# Per-eye CSV column stem -> key inside the extraction's right_eye/left_eye
EYE_FIELD_MAP = {
    "AddPower": "add_power",
    "Axis": "axis",
    "BaseCurve": "base_curve",
    "Cylinder": "cylinder",
    "Descriptions": None,       # comes from the line item, not the eye
    "OrderQuantity": "order_quantity",
    "Sphere": "sphere",
}


def _leaf(node, *path):
    """Pull a {"value": x} leaf out of the nested extraction dict.
    Returns "" for anything missing/null so it lands as an empty CSV cell."""
    current = node or {}
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key) or {}
    if isinstance(current, dict):
        value = current.get("value")
    else:
        value = current
    return "" if value in (None, "") else str(value)


def build_sap_rows(order):
    """One Order -> a list of dicts keyed by SAP_CSV_COLUMNS, ONE ROW PER
    LINE ITEM, with the right eye in the "1" columns and the left eye in
    the unsuffixed ones.

    The OE code and UoM columns come from the product lookup
    (order.oe_code), never from the LLM - see lookup_oe_code().
    """
    data = order.extracted_data or {}
    header_fields = data.get("order") or {}

    trial_only = _leaf(header_fields, "trial_only")
    dtp_order = _leaf(header_fields, "dtp_order")

    base = {
        "AccountNumber": _leaf(header_fields, "account_number"),
        "CustomerAddress": _leaf(header_fields, "customer_address"),
        "CustomerName": _leaf(header_fields, "customer_name"),
        "DTPOrder": dtp_order,
        "DTPAddressExport": _leaf(header_fields, "dtp_address_line1"),
        "DTPPatientName": _leaf(header_fields, "dtp_patient_name"),
        "OrderType": _leaf(header_fields, "order_type"),
        "PONumber": _leaf(header_fields, "po_number"),
        "PatientName": _leaf(header_fields, "patient_name"),
        "Special_Instructions": _leaf(header_fields, "special_instructions"),
        "T_TrialOnly": trial_only,
        # one OE code per eye; this backend currently resolves a single
        # order-level code, so it is written to both until the product
        # lookup returns per-eye codes
        "SAPOECode_Right": order.oe_code,
        "SAPUoM_Right": order.oe_uom,
        "SAPOECode_Left": order.oe_code,
        "SAPUoM_Left": order.oe_uom,
    }

    line_items = data.get("line_items") or []
    if not line_items:
        # never silently drop an order from the export
        return [dict(base, **{c: base.get(c, "") for c in SAP_CSV_COLUMNS})]

    rows = []
    for item in line_items:
        row = dict(base)
        description = _leaf(item, "product_description")

        for eye_key, suffix in (("right_eye", RIGHT_EYE_SUFFIX), ("left_eye", LEFT_EYE_SUFFIX)):
            eye = item.get(eye_key) or {}
            for stem, field in EYE_FIELD_MAP.items():
                column = f"{stem}{suffix}"
                row[column] = description if field is None else _leaf(eye, field)

        # A stock/top-up order gives no eye. Put it in the right-eye
        # columns so the row is not blank, and the reviewer decides -
        # extraction_notes will say the eye was unspecified.
        unspecified = item.get("unspecified_eye") or {}
        if any(_leaf(unspecified, f) for f in EYE_FIELD_MAP.values() if f):
            for stem, field in EYE_FIELD_MAP.items():
                column = f"{stem}{RIGHT_EYE_SUFFIX}"
                if field is not None and not row.get(column):
                    row[column] = _leaf(unspecified, field)
            if not row.get(f"Descriptions{RIGHT_EYE_SUFFIX}"):
                row[f"Descriptions{RIGHT_EYE_SUFFIX}"] = description

        rows.append({column: row.get(column, "") for column in SAP_CSV_COLUMNS})

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


# =============================================================================
# Section G - SQS execution queue
# =============================================================================
#
# The architecture diagram's ingest -> queue -> worker path:
#
#   mailbox poller            SQS execution queue          worker
#   --------------            -------------------          ------
#   download attachment       one message per order        poll message
#   put document in S3   ->   automatic retries       ->   extract + score
#   create Order row          DLQ after retry limit        write to RDS
#   send message                                           delete message
#
# RETRIES AND THE DLQ ARE CONFIGURED ON THE QUEUE IN AWS, not here. Set a
# redrive policy on the execution queue pointing at the dead-letter queue
# with maxReceiveCount (e.g. 3). This code's only job is to delete a
# message when - and only when - the work actually succeeded. A message
# that throws is left on the queue, becomes visible again after the
# visibility timeout, and is redelivered until maxReceiveCount is hit, at
# which point SQS itself moves it to the DLQ. Nothing here deletes a
# failed message, which is what makes that work.
#
# Idempotency: SQS is at-least-once, so the same message CAN arrive twice.
# That is safe here because the Order row already exists before the
# message is sent, and process_queued_message() skips any order already in
# a terminal state.

def sqs_client():
    require("SQS_QUEUE_URL")  # static keys are optional - see aws_credential_kwargs()
    import boto3  # imported here, not at module level - see file docstring

    return boto3.client("sqs", region_name=settings.AWS_REGION, **aws_credential_kwargs())


def enqueue_order(order):
    """One message per order, per the diagram.

    The body carries only identifiers - the document itself is already in
    S3 and the row is already in the database, so the message stays tiny
    (far under the 256 KB SQS limit) and there is one source of truth.
    """
    client = sqs_client()
    body = {
        "order_id": order.id,
        "message_id": order.message_id,
        "attachment_id": order.attachment_id,
        "s3_key": order.s3_key,
    }
    response = client.send_message(
        QueueUrl=settings.SQS_QUEUE_URL,
        MessageBody=json.dumps(body),
    )

    order.status = "QUEUED"
    order.save(update_fields=["status"])
    log_audit(order, "QUEUED", note=f"sqs message {response.get('MessageId', '?')}")
    return response.get("MessageId")


def _load_file_bytes(order):
    """Worker-side fetch of the document the ingest step stored. Body
    orders have no file at all - they extract from order.body_text."""
    if order.attachment_id == "BODY" or not order.s3_key:
        return None

    client = s3_client()
    obj = client.get_object(Bucket=settings.S3_INPUT_BUCKET, Key=order.s3_key)
    return obj["Body"].read()


def process_queued_message(message):
    """Handle ONE SQS message. Returns True if the message should be
    deleted, False to leave it on the queue for SQS to retry/DLQ.

    Raises nothing - the caller only needs the delete/keep decision.
    """
    try:
        body = json.loads(message.get("Body", "{}"))
        order_id = body.get("order_id")
    except (json.JSONDecodeError, TypeError, AttributeError):
        logger.exception("Unparseable SQS message body, leaving for the DLQ")
        return False

    order = Order.objects.filter(id=order_id).first()
    if order is None:
        # the row is gone (deleted/purged) - retrying will never help, so
        # drop the message rather than letting it cycle to the DLQ
        logger.warning("SQS message references missing order_id=%s, deleting", order_id)
        return True

    # At-least-once delivery: this message may be a duplicate of one
    # already processed. Only DECIDED states count as done.
    #
    # FAILED IS DELIBERATELY RETRYABLE. _mark_failed() below stamps the row
    # FAILED so a human can see it struggling, but the message stays on the
    # queue - so when SQS redelivers it, this must not mistake that row for
    # completed work and delete the message. Treating FAILED as terminal
    # here would silently defeat retries and the DLQ entirely.
    RETRYABLE = ("NEW", "QUEUED", "PROCESSING", "FAILED")
    if order.status not in RETRYABLE:
        logger.info(
            "Order %s already decided (%s), deleting duplicate message",
            order.id, order.status,
        )
        return True

    try:
        file_bytes = _load_file_bytes(order)
        result = run_extraction(order, file_bytes)
        logger.info("Processed order %s from queue: %s", order.id, result.get("status"))
        return True

    except Exception as e:
        # DO NOT delete - SQS redelivers after the visibility timeout and
        # the queue's redrive policy sends it to the DLQ once
        # maxReceiveCount is exhausted.
        receive_count = (message.get("Attributes") or {}).get("ApproximateReceiveCount", "?")
        logger.exception(
            "Order %s failed on queue attempt %s, leaving message for retry/DLQ",
            order.id, receive_count,
        )
        _mark_failed(order, f"attempt {receive_count}: {e}")
        return False


def drain_sqs_queue():
    """Poll the execution queue and process whatever is waiting.

    Called by the scheduler. Uses long polling so an idle queue costs one
    cheap request rather than a busy loop, and keeps pulling batches until
    the queue is empty or SQS_MAX_BATCHES is reached (so one tick cannot
    run forever).

    Raises ConfigError if SQS isn't configured - the scheduler logs that
    and waits for the next tick.
    """
    client = sqs_client()
    summary = {"received": 0, "processed": 0, "left_for_retry": 0}

    for _ in range(settings.SQS_MAX_BATCHES):
        response = client.receive_message(
            QueueUrl=settings.SQS_QUEUE_URL,
            MaxNumberOfMessages=settings.SQS_MAX_MESSAGES,
            WaitTimeSeconds=settings.SQS_WAIT_TIME_SECONDS,   # long polling
            VisibilityTimeout=settings.SQS_VISIBILITY_TIMEOUT,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages", [])
        if not messages:
            break

        summary["received"] += len(messages)
        for message in messages:
            if process_queued_message(message):
                client.delete_message(
                    QueueUrl=settings.SQS_QUEUE_URL,
                    ReceiptHandle=message["ReceiptHandle"],
                )
                summary["processed"] += 1
            else:
                summary["left_for_retry"] += 1

    return summary
