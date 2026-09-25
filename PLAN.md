# OPTIC BOT — Django Backend Implementation Plan
**Audience:** implementing engineer / Sonnet model
**Rule for the implementer:** keep the code beginner-readable. Function-based views, no class hierarchies, no abstract base classes, no custom managers, no signals, no Celery. If a line needs a comment, write the comment.

---

## 1. Scope

### In scope (build now)
1. Django project + ONE app named `optic_bot`.
2. Outlook shared-mailbox polling every 5 minutes via Microsoft Graph (MSAL client credentials).
3. Download **every** order attachment (PDF / Word / Excel / CSV / image), upload each to the S3 input bucket.
4. Send each document to the LLM (Bedrock `converse` or the JnJ gateway) using a prompt stored in a `prompts/` folder. **One attachment = one order = one LLM call.**
5. Store extracted fields + PER-FIELD confidence in Postgres/SQLite.
6. Auto-approve when every field is at/above a CONFIGURABLE confidence threshold; otherwise flag for human review.
7. REST APIs for the human-in-the-loop: list low-confidence orders, view one, edit fields, approve, reject, read audit trail.
8. All secrets in `.env`.
9. Runs and is verifiable WITHOUT any real credentials.
10. Export an APPROVED/AUTO_APPROVED order to a SAP CSV and upload it to `S3_OUTPUT_BUCKET` (§15 - built with a **placeholder** column layout, since the real SAP spec hasn't been supplied yet).

### Out of scope (explicitly NOT built)
- No React / HTML / template frontend. APIs only.
- No mock data, no fake extractors, no seeded dummy rows.
- No SQS worker in phase 1 (flagged off, see §13).

---

## 2. Final file tree — do not create files beyond this

```
TestBot/
├── .env                      <- real secrets (user fills later, NEVER committed)
├── .env.example              <- template with empty values
├── .gitignore
├── requirements.txt
├── manage.py
├── opticbot/                 <- project config (django-admin startproject opticbot .)
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── optic_bot/                <- the app (python manage.py startapp optic_bot)
│   ├── __init__.py
│   ├── apps.py               <- starts the 5-minute scheduler
│   ├── models.py             <- 2 models
│   ├── views.py              <- ALL APIs (start reading here)
│   ├── urls.py               <- URL table
│   ├── services.py           <- outlook + s3 + llm + processing logic
│   ├── scheduler.py          <- APScheduler, one job, every 5 min
│   └── migrations/
└── prompts/
    ├── order_extraction.txt
    └── oe_rerank.txt         <- added §16, only used when the vector match is ambiguous
```

Delete the auto-generated `optic_bot/tests.py` and `optic_bot/admin.py` — they are not used.
That is **5 hand-written Python files** in the app. Do not add `serializers.py`, `utils.py`, `tasks.py`, `constants.py`, `management/commands/`, or a `core/` package.

---

## 3. `requirements.txt`

```
Django==5.0.6
djangorestframework==3.15.1
python-dotenv==1.0.1
msal==1.28.0
requests==2.32.3
boto3==1.34.120
APScheduler==3.10.4
psycopg2-binary==2.9.9
```

Rationale for DRF: `@api_view` gives clean JSON parsing, status codes, and a **browsable API in the browser** — which matters because there is no frontend to test with. No serializer classes will be used; a plain `order_to_dict()` helper in `views.py` does the conversion. That keeps it fresher-readable.

---

## 4. `.env.example` — every key the code reads

```ini
# ---------- Django ----------
SECRET_KEY=
DEBUG=True
ALLOWED_HOSTS=*

# ---------- Database ----------
# local test = sqlite (leave DB_NAME blank), EC2 = postgres on RDS
DB_ENGINE=sqlite
DB_NAME=
DB_USER=
DB_PASSWORD=
DB_HOST=
DB_PORT=5432

# ---------- Microsoft Outlook / Graph ----------
MS_TENANT_ID=
MS_CLIENT_ID=
MS_CLIENT_SECRET=
MAILBOX_USER_EMAIL=ra-rpatsaspacteam@ITS.JNJ.com
MAIL_TARGET_FOLDER=OPTIC BOT
MAIL_POLL_MINUTES=5
MAIL_BATCH_SIZE=20
MARK_MAIL_AS_READ=True
# body is always requested as PLAIN TEXT, never HTML (see services Section B)
INCLUDE_BODY_AS_CONTEXT=True
MAX_BODY_CHARS=4000

# ---------- Attachments ----------
# one order document per attachment; anything not on this list is ignored
ALLOWED_EXTENSIONS=pdf,docx,doc,xlsx,xls,csv,png,jpg,jpeg
MAX_ATTACHMENT_MB=4.5
MIN_ATTACHMENT_KB=10
SKIP_INLINE_ATTACHMENTS=True

# ---------- AWS ----------
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
S3_INPUT_BUCKET=
S3_OUTPUT_BUCKET=

# ---------- OE master (pgvector RAG agent - built separately by the user) ----------
# Backend calls this endpoint; it does NOT talk to pgvector directly.
OE_RAG_URL=
OE_RAG_API_KEY=
OE_RAG_TIMEOUT=30
OE_MATCH_THRESHOLD=0.82
OE_TOP_K=5

# ---------- LLM ----------
# bedrock  = boto3 bedrock-runtime.converse()
# gateway  = POST to the JnJ internal gateway (x-api-key)
LLM_PROVIDER=bedrock
BEDROCK_MODEL_ID=global.anthropic.claude-opus-4-8
LLM_GATEWAY_URL=https://genaiapigwna.jnj.com/model/{model}/converse
LLM_GATEWAY_API_KEY=
LLM_MAX_TOKENS=4096
LLM_TIMEOUT=120

# ---------- Business rules ----------
CONFIDENCE_THRESHOLD=0.85
REQUIRE_OE_MATCH=True

# ---------- Scheduler ----------
RUN_SCHEDULER=True

# ---------- Not used in phase 1 ----------
USE_SQS=False
SQS_QUEUE_URL=
```

`.gitignore` must contain at minimum: `.env`, `*.sqlite3`, `__pycache__/`, `attachments/`, `.venv/`.

**Rule for the implementer:** the reference screenshots hardcoded the `x-api-key` and client secret directly in the script. In this codebase no secret may ever be written into a `.py` file — every credential is read from `.env` via `os.getenv`, and `.env` is gitignored.

---

## 5. `settings.py` — only the non-default parts

- `load_dotenv()` at the top, then read everything with `os.getenv(...)`.
- One tiny helper in settings: `def env_bool(key, default): return os.getenv(key, default).lower() == "true"`.
- `INSTALLED_APPS` += `rest_framework`, `optic_bot`.
- `DATABASES`: if `DB_ENGINE == "sqlite"` use `db.sqlite3`, else `django.db.backends.postgresql` with the DB_* vars. **This switch is what lets the app run with zero credentials.**
- Expose the business config as module-level settings so `views.py` and `services.py` can import them:
  `CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))`, plus the MS_*, AWS_*, LLM_*, MAIL_* values.
- `PROMPTS_DIR = BASE_DIR / "prompts"`.
- `REST_FRAMEWORK = {"DEFAULT_AUTHENTICATION_CLASSES": [], "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"]}`. **JWT is handled by the frontend, not here** — do not add `djangorestframework-simplejwt`, `PyJWT`, login views or a token refresh endpoint.
- Reviewer identity therefore arrives as data: `reviewed_by` in the request body, or an `X-User-Email` header if the frontend prefers. Write a one-line comment stating the consequence plainly so it is not discovered later: the backend trusts whatever identity the caller sends, so anything that can reach these endpoints can approve an order under any name. Keep the service off the public internet — private subnet + security group, reachable only from the frontend's origin, as the architecture diagram already shows.
- `CORS_ALLOWED_ORIGINS` will be needed once the frontend calls this from a browser. Add `django-cors-headers` **only when the frontend team asks for it**, not now.
- Basic `LOGGING` to console at INFO.

---

## 6. `models.py` — exactly two models

**One row per ATTACHMENT, not per email.** An email with 3 order files produces 3 `Order` rows that share a `message_id`. The dedupe key is therefore the pair `(message_id, attachment_id)`, not `message_id` alone.

```python
class Order(models.Model):
    # ---- source email (repeated on each attachment row - deliberate, keeps the
    #      list API a single table read with no join) ----
    message_id       = CharField(max_length=255, db_index=True)   # NOT unique any more
    sender           = CharField(max_length=255, blank=True)
    subject          = CharField(max_length=500, blank=True)
    received_at      = DateTimeField(null=True, blank=True)
    body_text        = TextField(blank=True)   # PLAIN TEXT only, never HTML

    # ---- this attachment ----
    attachment_id    = CharField(max_length=255)          # Graph attachment id
    attachment_name  = CharField(max_length=255, blank=True)   # "PO_4471.xlsx"
    file_type        = CharField(max_length=10, blank=True)    # pdf/docx/xlsx/csv/png...
    file_size        = IntegerField(default=0)                 # bytes
    s3_key           = CharField(max_length=500, blank=True)

    # ---- extraction result ----
    # {"po_number": {"value": "PO123", "confidence": 0.93, "edited": false}, ...}
    extracted_data   = JSONField(default=dict, blank=True)
    min_confidence   = FloatField(default=0.0, db_index=True)   # lowest field confidence

    # ---- OE code, answered by the pgvector RAG agent (Appendix A) ----
    oe_code          = CharField(max_length=50, blank=True)   # winning candidate
    oe_confidence    = FloatField(default=0.0)                # similarity score 0-1
    oe_matched       = BooleanField(default=False)            # score >= OE_MATCH_THRESHOLD
    # top-k alternatives, shown to the reviewer as a pick-list:
    # [{"oe_code":"OE-IN-0042","customer_name":"...","score":0.91,
    #   "match_type":"vector"}, ...]
    oe_candidates    = JSONField(default=list, blank=True)

    # ---- workflow ----
    # NEW / PROCESSING / NEEDS_REVIEW / AUTO_APPROVED / APPROVED / REJECTED
    # / NOT_AN_ORDER / FAILED
    status           = CharField(max_length=20, default="NEW", db_index=True)
    reviewed_by      = CharField(max_length=150, blank=True)
    reviewed_at      = DateTimeField(null=True, blank=True)
    review_comment   = TextField(blank=True)
    error_message    = TextField(blank=True)

    created_at       = DateTimeField(auto_now_add=True)
    updated_at       = DateTimeField(auto_now=True)

    class Meta:
        # this is the dedupe guard: re-reading an email can never create a
        # second row for an attachment that was already processed
        unique_together = ("message_id", "attachment_id")
        ordering = ["-created_at"]


class AuditLog(models.Model):
    order      = ForeignKey(Order, on_delete=CASCADE, related_name="audit_logs")
    action     = CharField(max_length=50)    # EMAIL_RECEIVED / EXTRACTED / AUTO_APPROVED /
                                             # FIELD_EDITED / APPROVED / REJECTED / FAILED
    actor      = CharField(max_length=150, default="system")
    field_name = CharField(max_length=100, blank=True)
    old_value  = TextField(blank=True)
    new_value  = TextField(blank=True)
    note       = TextField(blank=True)
    created_at = DateTimeField(auto_now_add=True)
```

Status meanings (write this as a comment block at the top of `models.py`):
`NEW` just ingested → `PROCESSING` sent to LLM → `AUTO_APPROVED` passed threshold → `NEEDS_REVIEW` below threshold → `APPROVED`/`REJECTED` human decision → `NOT_AN_ORDER` the file was a T&C / price list / logo, kept for audit only → `FAILED` error, see `error_message`.

`min_confidence` is a stored column on purpose: the "below threshold" API is then a single indexed `WHERE min_confidence < ?` query, no Python looping.

Why one row per attachment rather than an `Email` parent table + `Order` children: it keeps the model count at two, the list API needs no join, and — the real reason — if an email has 3 files and the 2nd one fails, the 1st and 3rd are already saved and the next poll retries only the failed one. A single row per email cannot express partial success.

---

## 7. `views.py` — the entry point, all APIs

Every view is an `@api_view([...])` function. Three small helpers at the top of the file:

```python
def order_to_dict(order, include_fields=True): ...
def log_audit(order, action, actor, **kw): ...
def get_threshold(request):
    """.env default, overridable per-request with ?threshold=0.9"""
```

| # | Method | Path | Purpose |
|---|--------|------|---------|
| 1 | GET | `/api/health/` | Per-dependency status. Never 500s. See §11. |
| 2 | GET | `/api/config/` | Current threshold, mailbox/folder, poll interval, counts per status. |
| 3 | GET | `/api/orders/` | List. Query params: `status=`, `below_threshold=true`, `threshold=`, `search=` (subject/sender/po), `file_type=` (`pdf`/`xlsx`/…), `message_id=` (all attachments of one email), `page=`, `page_size=` (default 20, max 100). Returns `{count, page, results:[...]}` — summary rows only, no `extracted_data`. Each row includes `attachment_name`, `file_type` and `message_id` so the reviewer can see which file a row came from. Default list **excludes** `NOT_AN_ORDER` and `FAILED` unless `status=` asks for them. |
| 4 | GET | `/api/orders/<id>/` | Full detail: every field with `value` + `confidence` + `edited` flag, plus a `low_confidence_fields` list naming which fields are under the threshold, plus a presigned S3 URL to that attachment, plus **`sibling_attachments`**: the other rows sharing this `message_id` as `[{id, attachment_name, file_type, status}]`. A reviewer looking at page 2 of a 3-file order email needs to know the other two exist. Also returns `body_text` (plain text), `oe_code`, `oe_confidence` and the full `oe_candidates` pick-list. |
| 5 | PATCH | `/api/orders/<id>/fields/` | Human edits. Body: `{"fields": {"po_number": "PO123", "quantity": "50"}, "oe_code": "OE-IN-0042", "reviewed_by": "nitin@..."}`. For each field: write the new value, set `confidence = 1.0`, set `edited = true`, write one `AuditLog` row per changed field with old/new value. Recompute `min_confidence`. If `oe_code` is present, set it, set `oe_matched=True`, `oe_confidence=1.0`, and audit it as a `FIELD_EDITED` on `oe_code` — a reviewer correcting a wrong OE code is the most common edit, so it must be a first-class field here. Returns the updated order. |
| 6 | POST | `/api/orders/<id>/approve/` | Body: `{"reviewed_by": "...", "comment": "", "fields": {optional last-minute edits}}`. Applies the edits (reuse the same internal function as #5), sets `status=APPROVED`, `reviewed_by`, `reviewed_at`, audits. Returns 400 if the order is already APPROVED/REJECTED. |
| 7 | POST | `/api/orders/<id>/reject/` | Body: `{"reviewed_by": "...", "reason": "..."}` — `reason` is required. Sets `status=REJECTED`, audits. |
| 8 | GET | `/api/orders/<id>/audit/` | Full audit trail, oldest first. |
| 9 | POST | `/api/poll/` | Manually trigger ONE mail poll right now. Exists so the reviewer need not wait 5 minutes, and so the whole pipeline can be exercised on demand. Returns `{"emails_checked": n, "attachments_found": n, "orders_created": n, "skipped": n, "not_orders": n, "errors": [...]}` — counted per attachment, not per email. |
| 10 | POST | `/api/orders/<id>/reprocess/` | Re-run extraction on an existing order (after a prompt change). ~6 lines, reuses `services.process_email` internals. |
| 11 | POST | `/api/orders/<id>/export/` | Build the SAP CSV for one order and upload it to `S3_OUTPUT_BUCKET`. Only valid when `status` is `APPROVED`/`AUTO_APPROVED` (400 otherwise). See §15 - **placeholder column layout**, not a real SAP spec yet. |

Rules for every view:
- Wrap the body in `try/except Exception as e:` and return `Response({"error": str(e)}, status=500)` — never leak an HTML traceback to an API client.
- Use `get_object_or_404`.
- Validate required body keys explicitly and return 400 with a plain message, e.g. `{"error": "reviewed_by is required"}`.

### `urls.py` (app) — one flat list
```python
urlpatterns = [
    path("health/", views.health),
    path("config/", views.config),
    path("orders/", views.list_orders),
    path("orders/<int:order_id>/", views.get_order),
    path("orders/<int:order_id>/fields/", views.edit_fields),
    path("orders/<int:order_id>/approve/", views.approve_order),
    path("orders/<int:order_id>/reject/", views.reject_order),
    path("orders/<int:order_id>/audit/", views.order_audit),
    path("orders/<int:order_id>/reprocess/", views.reprocess_order),
    path("poll/", views.trigger_poll),
]
```
Project `urls.py`: `path("api/", include("optic_bot.urls"))` + admin.

---

## 8. `services.py` — one file, five labelled sections

Use the `# =====` banner comment style already present in the reference code.

### Section A — config guard
```python
class ConfigError(Exception): pass

def require(*keys):
    """Raise a clear 'Missing X in .env' instead of a confusing crash."""
```
Every external call starts by calling `require(...)`. **This is what makes the app testable with no credentials** — you get `ConfigError: Missing MS_CLIENT_SECRET in .env`, not a stack trace.
Corollary rule: **no boto3/msal client may be constructed at module import time.** Build clients inside functions only, or `import optic_bot.services` will fail on a machine with no credentials.

### Section B — Outlook / Graph (mirrors the screenshot code)
```python
def get_graph_token():
    # msal.ConfidentialClientApplication(CLIENT_ID,
    #     authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    #     client_credential=CLIENT_SECRET)
    # .acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    # raise ConfigError on failure; cache the token in a module-level dict until expiry

def build_headers(token):
    """THE PLAIN-TEXT RULE LIVES HERE."""
    # return {
    #     "Authorization": f"Bearer {token}",
    #     "Prefer": 'outlook.body-content-type="text"',
    # }
    # The Prefer header makes Graph return body.content as PLAIN TEXT with
    # body.contentType == "text". Do NOT fetch HTML and strip tags afterwards -
    # Outlook HTML is full of <style> blocks, conditional comments and tracking
    # pixels, and a regex stripper leaves CSS junk in the middle of the order
    # text. Let Graph do the conversion.
    # Note the exact quoting: outlook.body-content-type="text" - the inner
    # double quotes are part of the header value, so write the Python string
    # with single quotes.

def get_folder_id(headers):
    # GET /v1.0/users/{USER_EMAIL}/mailFolders/inbox/childFolders
    # case-insensitive match on displayName == MAIL_TARGET_FOLDER ("OPTIC BOT")

def fetch_new_emails(headers, folder_id):
    # GET .../mailFolders/{folder_id}/messages
    #     ?$filter=isRead eq false
    #     &$top={MAIL_BATCH_SIZE}
    #     &$orderby=receivedDateTime desc
    #     &$select=id,subject,from,receivedDateTime,body,hasAttachments
    # Pass the headers from build_headers() so body.content arrives as plain text.
    # "new mail" = unread. Second guard: (message_id, attachment_id) is unique,
    # so a re-read message is skipped, never double-processed.

def clean_body(mail):
    """Plain-text body, trimmed, ready to store and to send as LLM context."""
    # body = mail.get("body", {}) ; text = body.get("content", "")
    # Safety net only: if body.get("contentType") == "html" (the Prefer header
    # was dropped by a proxy), fall back to
    #     re.sub(r"<[^>]+>", " ", html.unescape(text))
    # using the stdlib html module - do NOT add BeautifulSoup for this.
    # Then collapse whitespace, strip, and cut to MAX_BODY_CHARS.
    # Quoted reply chains are kept: a forwarded order still contains the order.

def list_attachments(headers, message_id):
    """Metadata only - returns the list of attachments WORTH processing."""
    # GET .../messages/{id}/attachments?$select=id,name,contentType,size,isInline
    # Two steps on purpose: do NOT pull contentBytes for every attachment in one
    # response. A mail with four 4 MB files would blow past Graph's response limit.
    # Keep an attachment only if ALL of these hold:
    #   - "@odata.type" == "#microsoft.graph.fileAttachment"
    #       (skip itemAttachment = forwarded email, referenceAttachment = OneDrive link)
    #   - isInline is False            (drops signature logos)  [SKIP_INLINE_ATTACHMENTS]
    #   - extension in ALLOWED_EXTENSIONS
    #   - MIN_ATTACHMENT_KB <= size <= MAX_ATTACHMENT_MB
    # Log every skipped attachment with the reason - silent drops are how orders
    # go missing.

def download_attachment(headers, message_id, attachment_id):
    # GET .../messages/{id}/attachments/{attachment_id}/$value  -> raw bytes
    # ($value returns the file directly; no base64 decode step, no JSON parse)

def mark_email_read(headers, message_id):
    # PATCH .../messages/{id}  {"isRead": true}   (only if MARK_MAIL_AS_READ)
```

### Section C — S3 + OE lookup
```python
def s3_client()                       # boto3 client, region+keys from settings
def upload_to_s3(file_bytes, key)     # ServerSideEncryption="aws:kms" per the diagram
def presigned_url(key, seconds=3600)  # for the review API; return None if S3 unset
```

**OE master is NOT in this codebase.** It lives in a pgvector database with a RAG agent in front of it, built separately by the user (table spec in Appendix A). This backend never opens a DB connection to it and never computes an embedding — it makes one HTTP call and stores the answer.

```python
def lookup_oe_code(fields, country=None):
    """POST the extracted customer identity to the RAG agent, get OE candidates.
       Returns (oe_code, oe_confidence, candidates_list, matched_bool)."""
    # if not settings.OE_RAG_URL: return (None, 0.0, [], False)
    #     -> the pipeline keeps working before the RAG agent exists; the order
    #        simply lands in NEEDS_REVIEW with no OE code
    #
    # REQUEST  POST {OE_RAG_URL}
    #   headers: {"x-api-key": OE_RAG_API_KEY}
    #   body: {"customer_name":    "...",
    #          "customer_account": "...",   # may be null
    #          "city":             "...",
    #          "country":          "...",
    #          "ship_to_address":  "...",
    #          "top_k": OE_TOP_K}
    #   (values pulled from fields[x]["value"], never the confidence wrapper)
    #
    # RESPONSE (the contract the user's agent must honour)
    #   {"candidates": [
    #      {"oe_code": "OE-IN-0042", "customer_name": "...", "country": "IN",
    #       "score": 0.91, "match_type": "exact|vector"},
    #      ...]}
    #
    # matched = candidates and candidates[0]["score"] >= OE_MATCH_THRESHOLD
    # Store ALL candidates on the Order, not just the winner - the human review
    # API shows them as a pick-list, which is the whole point of top_k.
    # Wrap in try/except: a RAG outage must NOT fail the order. Catch, log,
    # return (None, 0.0, [], False) -> the order goes to NEEDS_REVIEW.
```

### Section D — LLM extraction

**No local file parsing.** Do NOT add PyPDF2, python-docx, openpyxl, pandas or textract. The Bedrock `converse` API accepts PDF, Word and Excel as a native `document` content block and reads them itself. Adding parsers would mean three code paths, three failure modes, and worse extraction (a parser flattens an Excel order grid into unusable text). One code path handles every format.

```python
# extension -> converse "format" value. Anything not in here was already
# filtered out in list_attachments().
DOC_FORMATS = {"pdf": "pdf", "docx": "docx", "doc": "doc",
               "xlsx": "xlsx", "xls": "xls", "csv": "csv",
               "txt": "txt", "md": "md", "html": "html"}
IMAGE_FORMATS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg",
                 "gif": "gif", "webp": "webp"}

def build_content_block(file_bytes, file_name):
    """Images use an 'image' block, everything else a 'document' block."""
    # ext = file_name.rsplit(".", 1)[-1].lower()
    # if ext in IMAGE_FORMATS:
    #     return {"image": {"format": IMAGE_FORMATS[ext],
    #                       "source": {"bytes": file_bytes}}}
    # return {"document": {"format": DOC_FORMATS[ext],
    #                      "name": safe_doc_name(file_name),
    #                      "source": {"bytes": file_bytes}}}

def safe_doc_name(file_name):
    """GOTCHA - this one will cost an hour if skipped.
    Bedrock rejects a document 'name' containing periods, underscores or
    consecutive spaces with a ValidationException. Strip the extension, then
    keep only letters, digits, spaces, hyphens, parentheses and square
    brackets; collapse runs of whitespace. "PO_4471.v2.xlsx" -> "PO 4471 v2".
    The name is only a label for the model, so rewriting it is safe."""

def load_prompt(name):
    # read prompts/{name}.txt -> str  (no templating engine, just .format())

def call_llm(prompt_text, file_bytes, file_name):
    # ONE function, an if/else on settings.LLM_PROVIDER. Same converse payload
    # both ways:
    #   {"messages": [{"role": "user",
    #                  "content": [build_content_block(file_bytes, file_name),
    #                              {"text": prompt_text}]}],
    #    "inferenceConfig": {"maxTokens": LLM_MAX_TOKENS, "temperature": 0}}
    #
    #   "bedrock" -> boto3.client("bedrock-runtime").converse(
    #                   modelId=BEDROCK_MODEL_ID, **payload)
    #                boto3 takes RAW bytes in source.bytes.
    #   "gateway" -> requests.post(LLM_GATEWAY_URL.format(model=BEDROCK_MODEL_ID),
    #                   headers={"Content-Type": "application/json",
    #                            "x-api-key": LLM_GATEWAY_API_KEY},
    #                   json=payload, timeout=LLM_TIMEOUT)
    #                JSON cannot carry raw bytes, so for the gateway the same
    #                field must be base64.b64encode(file_bytes).decode(). Handle
    #                this with one `if provider == "gateway"` inside
    #                build_content_block - it is the ONLY difference between the
    #                two providers.
    #
    # Put the document block BEFORE the text block: the model reads the file,
    # then the instruction.
    # Response text at data["output"]["message"]["content"][0]["text"]

def parse_llm_json(text):
    # strip ```json fences, json.loads, raise a clear error if it is not valid JSON
```

Limits to respect (confirm against the AWS Bedrock docs for your region and model before go-live — these are the documented Converse limits, not guesses you should trust blindly): roughly 4.5 MB per document and at most 5 documents per request. Sending one attachment per call, as this design does, stays inside both. `MAX_ATTACHMENT_MB` in `.env` is the guard; an oversized file is saved as `FAILED` with `"attachment too large: 8.2 MB"` so it lands in the review queue instead of vanishing.

### Section E — the pipeline
```python
def score(fields):
    # returns min(f["confidence"] for f in fields.values()), 0.0 if empty

def process_email(mail, headers):
    """One email -> N Order rows, one per usable attachment."""
    # 1 atts = list_attachments(headers, mail["id"])
    # 2 if not atts: record ONE row with status="FAILED",
    #     error_message="no supported attachment found" -> it surfaces in the
    #     review queue instead of disappearing. (Order-in-the-email-body is
    #     phase 2, see 13.)
    # 3 for each attachment: process_attachment(...) in its OWN try/except, so
    #     file 2 failing never stops files 1 and 3
    # 4 mark_email_read ONLY if every attachment ended in a terminal state.
    #     If any raised before its row was saved, leave the mail unread so the
    #     next poll retries it - the unique_together guard means the already
    #     saved attachments are skipped, not duplicated.
    # 5 return a per-email summary dict

def process_attachment(mail, att, headers):
    """One attachment -> one Order row. On failure the row is still saved with
       status=FAILED + error_message, so nothing is silently lost."""
    # 1 skip if Order.objects.filter(message_id=..., attachment_id=...).exists()
    # 2 create Order(status="NEW") with the mail headers, clean_body(mail) and
    #   the attachment metadata
    # 3 download_attachment -> upload_to_s3 under
    #     {message_id}/{attachment_id}_{attachment_name}  -> save s3_key
    #     (the attachment_id in the key stops two files of the same name in one
    #      mail from overwriting each other in S3)
    # 4 status = "PROCESSING"
    # 5 prompt = load_prompt("order_extraction")
    #   if INCLUDE_BODY_AS_CONTEXT and order.body_text:
    #       prompt += "\n\nEMAIL BODY (context only):\n" + order.body_text
    #   call_llm(prompt, bytes, name) -> parse_llm_json
    #   if is_order is False -> status="NOT_AN_ORDER", save, return
    # 6 oe_code, oe_confidence, oe_candidates, oe_matched = lookup_oe_code(fields)
    # 7 min_confidence = score(fields)
    # 8 auto-eligible = min_confidence >= CONFIDENCE_THRESHOLD
    #                   and (oe_matched or not REQUIRE_OE_MATCH)
    #    -> status = "AUTO_APPROVED"  else  "NEEDS_REVIEW"
    # 9 log_audit

def poll_mailbox():
    """Called by the scheduler AND by POST /api/poll/. Returns a summary dict."""
    # token -> headers -> folder_id -> fetch_new_emails -> for each: process_email
    # never raises: collects per-email AND per-attachment errors into the summary
    # counts are per attachment: emails_checked / attachments_found /
    # orders_created / skipped / not_orders / errors[]
```

---

## 9. `scheduler.py` + `apps.py` — the 5-minute poll

`scheduler.py`:
```python
from apscheduler.schedulers.background import BackgroundScheduler

def start():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_poll,                             # thin wrapper that logs the summary dict
        "interval",
        minutes=settings.MAIL_POLL_MINUTES,   # 5 by default, from .env
        id="poll_outlook",
        max_instances=1,                      # never overlap two polls
        replace_existing=True,
        coalesce=True,                        # missed runs fire once, not N times
        # datetime.now(timezone.utc), NOT bare datetime.now() - the scheduler
        # runs in UTC, and a naive local timestamp on a machine ahead of UTC
        # (e.g. IST, UTC+5:30) gets read as "5:30 from now", not "now".
        next_run_time=datetime.now(timezone.utc),  # run once immediately on boot
    )
    scheduler.start()
```

`apps.py`:
```python
def ready(self):
    if os.getenv("RUN_SCHEDULER", "False").lower() == "true":
        if os.environ.get("RUN_MAIN") == "true" or not settings.DEBUG:
            from . import scheduler
            scheduler.start()
```
The `RUN_MAIN` check stops the dev-server autoreloader from starting the scheduler twice. Note in a comment: on EC2 under gunicorn, run with `--workers 1`, or set `RUN_SCHEDULER=False` on the web workers and run one dedicated scheduler process.

---

## 10. `prompts/order_extraction.txt`

Plain text, loaded with `load_prompt("order_extraction")`. Contents must specify:
- Role: "You extract purchase-order data from a customer order document."
- The exact field list: `customer_po_number, customer_name, customer_account, order_date, requested_delivery_date, ship_to_address, ship_to_country, currency, incoterms, contact_email, total_amount, line_items[]` where each line item has `material_code, description, quantity, uom, unit_price`.
- **Output contract, stated strictly:**
```json
{"is_order": true,
 "fields": {
  "customer_po_number": {"value": "PO-4471", "confidence": 0.97},
  "order_date":         {"value": "2026-09-20", "confidence": 0.81}
}}
```
- Rules: return ONLY JSON, no prose, no markdown fences. `confidence` is 0.0–1.0 and must reflect how clearly the value was readable in the document. If a field is absent use `{"value": null, "confidence": 0.0}`. Never invent a value. Dates as `YYYY-MM-DD`. Quantities as plain numbers.
- **`is_order` guard — needed because an email carries several attachments and not all are orders.** Terms & conditions, price lists, catalogues, signature images and company letterhead are NOT orders. If the document is not a purchase order, return `{"is_order": false, "fields": {}}` and nothing else. `process_attachment` then saves the row as `NOT_AN_ORDER`, which keeps junk out of the human review queue while still leaving an auditable record that the file was seen.
- One line telling the model the document may be a scan, a spreadsheet grid, or a Word letter, and that it should read tables row by row. The same prompt serves every format.
- A closing instruction covering the appended email body: *"An EMAIL BODY section may follow. Use it only to fill fields missing from the attachment (for example a delivery date or PO number written in the mail itself). The attachment always wins on conflict. Never treat a signature block or disclaimer as order data."* The body arrives as plain text, so there is no HTML for the model to trip over.
- Do **not** ask the model for the OE code. It has no access to the OE master; that is the RAG agent's job (Appendix A).

Keeping the prompt in a file (not a Python string) is what lets the prompt be tuned without a code deploy.

---

## 11. Verification WITHOUT credentials

This is the acceptance gate. Run in order, with an empty `.env` (only `SECRET_KEY`, `DEBUG=True`, `DB_ENGINE=sqlite`, `RUN_SCHEDULER=False`):

| # | Command / call | Must produce |
|---|----------------|--------------|
| 1 | `pip install -r requirements.txt` | clean install |
| 2 | `python manage.py check` | `System check identified no issues` |
| 3 | `python manage.py makemigrations optic_bot` | 1 migration file, 2 models |
| 4 | `python manage.py migrate` | applies on SQLite, no Postgres needed |
| 5 | `python manage.py runserver` | boots, no scheduler noise |
| 6 | `GET /api/health/` | **200**, each dependency reported independently: `{"database":"ok","outlook":"not_configured: MS_CLIENT_ID","s3":"not_configured: S3_INPUT_BUCKET","llm":"not_configured: LLM_GATEWAY_API_KEY","oe_rag":"not_configured: OE_RAG_URL","confidence_threshold":0.85,"scheduler":"disabled"}` — must NOT 500 |
| 7 | `GET /api/config/` | 200, threshold + zeroed status counts |
| 8 | `GET /api/orders/?below_threshold=true` | 200, `{"count":0,"results":[]}` — proves the query and threshold logic resolve |
| 9 | `POST /api/poll/` | **400**, `{"error":"Missing MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID in .env"}` — a clean, named failure. This is the proof the Graph wiring is correct and only waiting on secrets. |
| 10 | `GET /api/orders/999/` | 404 JSON, not an HTML error page |
| 11 | `python -c "import optic_bot.services"` | imports with no credentials present (proves no module-level boto3/msal client) |
| 12 | Set `RUN_SCHEDULER=True`, `runserver` | log line `Scheduler started - polling Outlook every 5 minutes`, then one immediate attempt logging the same clean ConfigError, then quiet |
| 13 | `python -c "from optic_bot.services import safe_doc_name as f; print(f('PO_4471.v2 final.xlsx'))"` | `PO 4471 v2 final` — no periods, no underscores, no double spaces. This is pure string logic, needs no credentials, and prevents a Bedrock `ValidationException` in production. |
| 14 | `python -c "from optic_bot.services import build_content_block as b; [print(list(b(b'x', 'a.'+e))[0]) for e in ['pdf','docx','xlsx','csv','png']]"` | `document, document, document, document, image` — proves every required format maps to a block type and none raises `KeyError` |

Steps 13–14 are the only "unit tests" in the build, and they exist because both are pure functions with no I/O: the format map and the filename sanitiser are exactly the two places a multi-format pipeline breaks, and both are checkable with zero credentials.

Then the credential smoke test, once the user supplies `.env` values — in this order, so a failure points at exactly one thing:
1. `GET /api/health/` → `"outlook":"ok"` means the MSAL token was acquired and the "OPTIC BOT" folder was found.
2. Send one real order email to the monitored folder — **use a mail carrying a PDF, an Excel and a Word file at once**, which is the case this design exists for.
3. `POST /api/poll/` → `{"emails_checked":1,"attachments_found":3,"orders_created":3}`.
4. `GET /api/orders/?message_id=...` → 3 rows, one per file, each with its own `file_type` and confidence.
5. `GET /api/orders/?below_threshold=true` → only the rows where a field scored under 0.85.
6. `PATCH /api/orders/1/fields/` then `POST /api/orders/1/approve/` → status `APPROVED`, audit trail shows every edit.
7. `POST /api/poll/` a second time without new mail → `{"emails_checked":0}`, and if the mail is re-marked unread, `{"attachments_found":3,"orders_created":0,"skipped":3}` — proves the `(message_id, attachment_id)` dedupe holds.

No mock objects, no fake extractor, no seeded rows are used anywhere. Steps 1–12 test real wiring; the credential path is tested with real credentials.

---

## 12. Deliberate decisions (and why)

| Decision | Chosen | Why |
|---|---|---|
| API layer | DRF `@api_view` functions, **no serializer classes** | Browsable API to test without a frontend; still reads like plain functions |
| DB | `DB_ENGINE` switch: SQLite local, Postgres/RDS on EC2 | The only way to run with zero credentials |
| SQS | **Skipped in phase 1**, `USE_SQS=False` in `.env` | `poll_mailbox()` processes inline. Adding SQS means a second worker process for no phase-1 benefit. The env flag reserves the seam. |
| Scheduling | APScheduler started from `apps.py` | Zero extra files. Prod alternative noted in a comment: disable it and call `POST /api/poll/` from OS cron or EventBridge. |
| Threshold | `.env` default + `?threshold=` per-request override | "Configurable" at deploy time *and* at query time, without a settings table |
| Auth | `AllowAny`, no JWT libraries | **User decision: JWT lives in the frontend.** Backend takes reviewer identity as data and is protected at the network layer. |
| Email body | Graph `Prefer: outlook.body-content-type="text"` | **User decision: plain text, never HTML.** Converted by Graph, not by a regex stripper afterwards. |
| OE master | HTTP call to the user's pgvector RAG agent | **User decision.** Backend stores top-k candidates and never embeds or queries vectors itself. Contract in Appendix A. |
| Confidence | denormalised `min_confidence` column | Makes the low-confidence API a single indexed query |
| Multi-attachment | one `Order` row per attachment, dedupe on `(message_id, attachment_id)` | Keeps 2 models and no join, and is the only shape that survives file 2 of 3 failing |
| PDF / Word / Excel | Bedrock `converse` **document block**, no local parsing libs | One code path for all formats. A local parser flattens an Excel order grid into unusable text and doubles the failure modes. |
| Non-order files | `is_order` flag in the prompt → `NOT_AN_ORDER` status | T&Cs and logos stay out of the human queue but remain auditable |

---

## 13. Phase 2 backlog (do NOT build now)
1. CloudWatch logging handler, plus the diagram's alarm on DLQ depth (§18).
2. Duplicate-PO detection (same `customer_po_number` already APPROVED) — more likely now that one email can yield several rows.
3. **ZIP attachments** — unzip in memory, treat each inner file as its own attachment row. Skipped now because it needs a recursion guard and a zip-bomb size cap.
4. Attachments larger than `LLM_MAX_DOCUMENT_MB` — split a large PDF by page range, or route to Bedrock Data Automation. Today these are stored and flagged `FAILED` for manual entry, never dropped (§17.1).
5. Merging the several attachments of one email into a single order when they are pages of one document rather than separate orders. Needs a business rule from the user; today each file is its own order.
6. Per-eye OE codes — the CSV has `SAPOECode_Right`/`_Left`, but the lookup currently resolves one code per order and writes it to both (§17.5).

Built ahead of schedule and moved out of this list: CSV export (§15), OE match reasoning (§16), the contact lens rebuild (§17), and the SQS execution queue (§18).

---

## 14. Build order for the implementer
1. `requirements.txt`, `.env.example`, `.gitignore`
2. `django-admin startproject opticbot .` → `python manage.py startapp optic_bot` → delete `tests.py`, `admin.py`
3. `settings.py` (dotenv, DB switch, config constants)
4. `models.py` → `makemigrations` → `migrate`
5. `prompts/order_extraction.txt`
6. `services.py` sections A→E
7. `views.py` + `urls.py`
8. `scheduler.py` + `apps.py`
9. Run verification table §11 steps 1–14 and paste the actual output. Do not report done before step 14 passes.

**The RAG agent is not a dependency of this build.** With `OE_RAG_URL` empty, `lookup_oe_code()` returns `(None, 0.0, [], False)` and every order lands in `NEEDS_REVIEW` with a blank OE code that the reviewer fills in via endpoint #5. Build and test the whole pipeline before the vector DB exists; point `OE_RAG_URL` at it when it is ready, and nothing else changes.

---

## 15. SAP CSV export (built ahead of schedule, PLACEHOLDER layout)

Originally phase 2 (§13 item 1), built early because it's the next box in the architecture diagram after approval. **The column layout is a guess, not a real SAP import spec** - nobody has supplied one, so this must be revisited before a real SAP import runs on this file.

**Endpoint:** `POST /api/orders/<id>/export/` - only callable when `status` is `APPROVED` or `AUTO_APPROVED` (400 otherwise, naming the current status). Can be called again after a later field correction; it just overwrites the same S3 key and updates `exported_at`.

**Model fields added to `Order`:** `exported_at` (nullable timestamp) and `export_s3_key`. Export is deliberately **not a workflow status** - conflating it with `status` would mean an order could no longer be re-approved or corrected after its first export. `GET /api/orders/<id>/` now also returns `export_url` (a presigned link to the CSV in `S3_OUTPUT_BUCKET`, `null` until exported).

**`services.py` Section F** (`SAP_CSV_COLUMNS`, `build_sap_rows()`, `export_order_to_csv()`): one row per **line item**, not per order - a common SAP flat-file shape where header fields (PO number, customer, ship-to, and critically `SoldToParty` = `order.oe_code`, the vector-matched code, never anything the LLM extracted) repeat on every item line. An order with no line items still produces one row with blank item columns, so it's never silently dropped from the export. Written with the stdlib `csv` module to an in-memory buffer, encoded `utf-8-sig` (the BOM keeps Excel/SAP from misreading special characters), then uploaded via the same `upload_to_s3()` function attachments use - it now takes an optional `bucket=` argument so it can target `S3_OUTPUT_BUCKET` instead of `S3_INPUT_BUCKET`. `s3_client()` was correspondingly loosened to only require AWS credentials, not a specific bucket, since it now serves both.

**Before this touches a real SAP import:** replace `SAP_CSV_COLUMNS` and the field mapping in `build_sap_rows()` with the actual column names, column order, and date/decimal format your SAP import expects. Everything else (CSV writing, S3 upload, the `EXPORTED` audit log entry) stays the same regardless of what the columns are called.

---

## 16. OE match reasoning (LLM explains EVERY OE decision, for audit)

**User decision, explicit correction of an earlier version of this section.** The first cut of this feature only called the LLM when the vector score was ambiguous, to save cost. The user overrode that: they want the reason **every time**, not just the edge cases, because the reason is what an audit trail is for - a bare similarity number doesn't tell a later reviewer *why* an order was routed the way it was.

**Note: this section predates the Appendix A correction (direct pgvector query, product-centric matching, no HTTP agent) - read it for the "always reason, not just ambiguous" decision, and read Appendix A for the actual current mechanics (`query_oe_master`, pack-size-as-required-exact-match, etc.). The table below is updated; the prose above it describes the original (superseded) customer-matching design.**

**`lookup_oe_code()` now always asks the LLM to explain, whenever the direct pgvector query (`query_oe_master()`, Appendix A) returns at least one candidate** (skipped only if `OE_MATCHING_ENABLED` is False, or no candidates came back at all - there's nothing to reason about then). It calls `rerank_oe_candidates(fields, candidates)`, which sends the candidate list *and* the order's extracted product context (product descriptions, pack size, base curve, trial flag) to the LLM via `prompts/oe_rerank.txt`, asking it to weigh context the vector search doesn't enforce directly - pack size as a required exact match, base curve, lens type - and to explain its reasoning either way. The vector similarity score is passed to the LLM too (as context in the candidate list), so a clean 0.97 top score should still come back a confident match - it's just explained now, not silently accepted.

**Decision rule:** if the LLM is confident (`confidence >= OE_RERANK_THRESHOLD`, default 0.75) **and** the `oe_code` it returned is actually one of the offered candidates (a hallucinated code is discarded, never trusted), that becomes the match. Otherwise `oe_code` stays blank and the order goes to `NEEDS_REVIEW` with all candidates attached for a human to pick from - but the reason is recorded regardless of which way the decision went.

**`Order.oe_match_reason`** (new `TextField`) holds that reason as a top-level, always-populated field - not buried in the `oe_candidates` JSON. Every return path of `lookup_oe_code()` guarantees a non-empty string:

| Situation | `oe_match_reason` |
|---|---|
| `OE_MATCHING_ENABLED=False` | `"OE_MATCHING_ENABLED is False"` |
| No product description was extracted | `"no product description was extracted to match on"` |
| `oe_master` returned zero candidates | `"oe_master returned no candidates"` |
| `OE_RERANK_ENABLED=False` | `"vector score 0.97 >= OE_MATCH_THRESHOLD 0.82 (LLM reasoning disabled)"` (or the below-threshold equivalent) |
| LLM found a confident match | whatever the LLM wrote, e.g. `"pack size 30P matches exactly; base curve 8.5 matches; brand/variant text matches"` |
| LLM found no confident match | the LLM's own explanation, e.g. `"candidate's pack size (1P) does not match the order's 30-pack"` |
| LLM call itself failed (bad JSON, network error, not configured) | `"rerank failed: <error>"` |
| The pgvector query itself failed | `"OE pgvector lookup failed: <error>"` |

`lookup_oe_code()` now returns a **5-tuple**: `(oe_code, oe_confidence, candidates_list, matched_bool, reason)`. `_extract_and_score()` writes `reason` straight to `order.oe_match_reason`, and it's folded into the `EXTRACTED` audit log note too, so it shows up both on the order (`GET /api/orders/<id>/` and the list view - it's not gated behind `include_fields`) and in the audit trail (`GET /api/orders/<id>/audit/`).

**`OE_RERANK_ENABLED=False`** is the one remaining escape hatch: skips the LLM call entirely, falls back to a bare `OE_MATCH_THRESHOLD` check on the vector score - faster and free, but `oe_match_reason` becomes a short templated string instead of an LLM explanation (still never empty, just less rich).

**`call_llm()` generalized to support a text-only call** (`call_llm(prompt_text)`, no `file_bytes`/`file_name`) for this reasoning step, since there's no document involved - only the extraction path passes a file. Same provider branching (bedrock/gateway), same everything else.

**Never raises, same as `lookup_oe_code()` always did:** any failure in the rerank call is caught inside `rerank_oe_candidates()` and turned into a reason string explaining the failure, not an exception. A broken reranker degrades gracefully - the pipeline still finishes, the order still gets a reason, it just says the rerank failed instead of explaining a match.

**Cost/latency tradeoff, stated plainly:** this now calls the LLM on every order that has at least one OE candidate, not just the ambiguous ones. That's slower and costs more than the original design, and is a deliberate trade the user made for audit completeness over efficiency.

**Verified** (mocked RAG + LLM responses, no real credentials, 8 branches): no RAG URL; a clean 0.97-score winner (confirmed the LLM is now called even here, where the old version would have skipped it); ambiguous + confident LLM match; ambiguous + LLM says no match; LLM call raising an exception; RAG agent returning zero candidates; `OE_RERANK_ENABLED=False`; the RAG POST itself failing. Every branch returned a non-empty `reason`. Also verified end-to-end through `process_attachment()` with mocked extraction + rerank LLM calls: `oe_match_reason` lands correctly on the saved `Order` row, in its audit log note, and in both the list and detail API responses.

---

## 17. Contact lens domain rebuild (from the real sample data)

**This section supersedes the generic "purchase order" assumptions in §6, §10 and §15.** The reference material added to `reference-images/` (the required-columns spreadsheet, the RPA JSON, and eight real sample emails) showed this is a **Johnson & Johnson Vision Care contact lens ordering** system, not a generic PO system. Mailboxes: `RA-JJVC-CUST-SERVICE@ITS.JNJ.com` and `Orders@VISAU.JNJ.com`.

### 17.1 Two bugs the sample data exposed (both would have lost real orders)

| Bug | Evidence | Fix |
|---|---|---|
| Attachment size filters silently dropped genuine orders | LensesOnline's real order `Order 320681.xls` is **5.5 KB**; a real order PDF is **13.35 KB** - both were near or below the old 10 KB floor | **All size limits removed.** An attachment may be 2 KB or 50 MB; it is always downloaded, stored in S3 and processed. Size is never a reason to skip a file. Signature logos are excluded by the `isInline` check instead, and anything that slips through is rejected by the prompt's `is_order` flag |
| Emails with no attachment were marked `FAILED` | **2 of 8 samples** are orders typed straight into the body with only signature images attached ("Store boxes please", "Trials Klein") | New `process_body_order()` - a body order is now a first-class order with `attachment_id="BODY"`, extracted by a text-only LLM call |

**Oversized files are never dropped, only flagged.** The LLM provider has its own hard per-document limit (`LLM_MAX_DOCUMENT_MB`, default 4.5 MB - Bedrock's documented figure; verify for your model and region). That value is **not a filter**: a larger file is still downloaded and stored in S3, and the order is saved as `FAILED` carrying an explicit message ("is 50.0 MB, above the 4.5 MB the LLM accepts in one call... needs manual entry, or split into smaller files"). It surfaces in the review queue asking for human action instead of vanishing or throwing a cryptic provider error. Splitting large PDFs automatically remains a phase 2 item.

### 17.2 Email triage - `prompts/email_classification.txt` (NEW)

One cheap text-only LLM call on sender/subject/body/attachment-names, made **before** anything is downloaded or extracted, so delivery chasers, invoice queries, complaints, marketing and out-of-office replies never reach the expensive step. `classify_email()` **fails open**: if it is disabled, unavailable, or returns junk, the email is treated as an ORDER, and it only skips an email when it is *confident* (>= `EMAIL_CLASSIFICATION_THRESHOLD`, default 0.80) it is not one. Discarding a real order is far worse than paying to extract a communication email. A classified-out email still gets one `NOT_AN_ORDER` row recording the classifier's reason, so a misclassification is visible and recoverable rather than silently dropped.

The prompt is written around what these emails actually look like: it must catch orders whose subject never says "order" ("Store boxes please", "Trials Klein"), whose product names are misspelled ("Acuvus oasys astig"), or whose entire content is an attachment. It must reject "where is my order" while accepting "please resend order 40337, it never arrived" - the test is whether NEW product must be supplied.

### 17.3 Extraction - `prompts/order_extraction.txt` (REWRITTEN)

Now an optical-domain prompt. The rules that matter most:

- **The `x` ambiguity, which is the single most dangerous trap in this data.** After a cylinder, `x NNN` is the AXIS (`-0.75 / -2.75 x 180`). In a bulk power list with no cylinder, `x N` is the QUANTITY (`-0.75 x 3` = three boxes of -0.75). Both appear in the samples. The prompt teaches the test: is there a cylinder immediately before the `x`?
- **`Order Quantity 0` is an instruction, not a blank.** The Lighthouse PDF shows a full prescription for both eyes but quantity 0 on the left - that eye is not being ordered. Capture the Rx, record quantity 0, never assume 1.
- Rx shorthand (`R -7.00 / -1.75 x 20`), eye markers (R/OD/L/OS/OU), trials, DTP (direct-to-patient) vs practice shipping, account numbers written five different ways and often only in the subject line, pack size vs quantity ("Boxes of 90" is a pack size).
- **Product names are copied verbatim, misspellings included** - they are matched by the OE lookup, not by the extractor.
- Output is nested: `order` (header) + `line_items[]`, each with `right_eye` / `left_eye` / `unspecified_eye`, every leaf carrying its own confidence, plus an `extraction_notes` string for the reviewer.

### 17.4 The real CSV columns

`SAP_CSV_COLUMNS` is now exactly the 29 columns from the "Required Fields" sheet, in order. Verified: the BaileyNelson sample reproduces the reference CSV row **field-for-field**.

**Assumption flagged for confirmation:** columns ending in `1` (`Sphere1`, `BaseCurve1`, ...) are treated as the **RIGHT** eye and the unsuffixed ones as the **LEFT**. The sample JSON had identical values for both eyes, so this could not be proven from the data - it follows the ordering convention (`SAPOECode_Right` precedes `SAPOECode_Left`). If it is the other way round, swap `RIGHT_EYE_SUFFIX`/`LEFT_EYE_SUFFIX` in `services.py` and nothing else changes.

**Also unconfirmed:** `T_TrialOnly` is mapped from the extracted `trial_only` flag. In the sample JSON `T_TrialOnly` is `""` while a separate `TrialOnly` is `"True"`, so `T_TrialOnly` may be one of five RPA-internal trial-type flags (`A_/I_/O_/R_/T_TrialOnly`) rather than the real trial indicator. Mapping it keeps the column useful; confirm before go-live.

A stock top-up order with no eye specified has its values promoted into the right-eye columns so the row is not blank, and `extraction_notes` records that the eye was unspecified.

### 17.5 The OE lookup is a PRODUCT lookup, not a customer lookup

This is the most important architectural correction, and it **changes what belongs in the pgvector table described in Appendix A**.

The JSON carries `SAPOECode_Right` **and** `SAPOECode_Left` - one per eye - alongside `SAPUoM`, `SAPProductGroup` ("Spherical"), and `SAPReqAttributes` ("Base Curve, Power"). An OE code of `MX` sits next to a product description of "Acuvue Oasys **Max** 1-Day". The OE code is derived from the **product**, not the customer.

That also matches where the fuzzy-matching difficulty actually is. Account numbers are stated explicitly and accurately in every sample (`6279505`, `6222573`, `6220512`, `6235954`, `6346628`) - they need no vector search. Product names are written a different way every time - "Acuvue Oasys Dailies", "Daily 1 Day Oasys 90pk", "1-Day Acuvue Oasys for Astigmatism (30)", "Acuvus oasys astig" - and *that* is what needs fuzzy matching to a canonical SAP product.

`lookup_oe_code()` now sends a `product_descriptions` list alongside the customer identity, so the RAG agent can match on either. **Appendix A's table should be re-modelled around the product master** (product description + aliases -> OE code, UoM, product group, required attributes, pack size) rather than the customer master. The returned candidate may include a `uom`, which is stored on `Order.oe_uom` and fills `SAPUoM_Right`/`SAPUoM_Left`.

### 17.6 Nested data means path-based review

`extracted_data` is now nested, so the review API works in **dotted paths**:
- `low_confidence_fields` returns paths like `order.account_number`, `line_items.0.right_eye.sphere`
- `PATCH /api/orders/<id>/fields/` accepts those same paths as keys
- `score()` recurses, and **ignores null-valued leaves** - a spherical lens has no cylinder and a stock order has no patient, and counting those zeros would drag every order's `min_confidence` to 0.0 and make the threshold meaningless
- `GET /api/orders/<id>/` also returns `extraction_notes`

**Verified against all four hard samples:** store top-up (8 powers -> 8 rows), Lighthouse (left eye quantity 0 preserved), LensesOnline (2 products -> 2 rows), OPSM trial (toric, different Rx per eye, correctly flagging the store-code account "B538" and the misspelled product as low confidence). Plus end-to-end: a body-only order becomes `NEEDS_REVIEW` with notes, and a "RE: order ETA?" email is classified out after exactly one LLM call with no extraction.

---

## 18. SQS execution queue (built — the diagram's ingest → queue → worker path)

Previously phase 2 (§13 item 1). Now built, behind `USE_SQS`. With `USE_SQS=False` (the default) nothing changes — the poller still extracts inline, and that path is regression-tested. With `USE_SQS=True` the pipeline splits exactly as the architecture diagram draws it:

```
mailbox poller            SQS execution queue          worker (drain_sqs)
--------------            -------------------          ------------------
download attachment       one message per order        poll message
put document in S3   ->   automatic retries       ->   extract + OE + score
create Order row          DLQ after retry limit        write to RDS
send message (QUEUED)                                  delete message
```

### 18.1 The split

`process_attachment()` was refactored into two halves so **both modes share identical ingest code and cannot drift**:

- `ingest_attachment()` — creates the `Order` row, downloads the attachment, puts it in S3. **No LLM call.**
- `run_extraction()` — LLM extract → OE lookup → score → save.

Inline mode calls both back-to-back. SQS mode calls `ingest_attachment()` then `enqueue_order()`, and the worker calls `run_extraction()` later. Body orders (§17) go down the same path — `_load_file_bytes()` returns `None` for an `attachment_id="BODY"` row, giving the worker a text-only call.

New status **`QUEUED`**: document is in S3, message sent, waiting for a worker.

### 18.2 What the message carries

Only identifiers — the document is already in S3 and the row already in the database, so the payload stays tiny (far under the 256 KB SQS limit) and there is one source of truth:

```json
{"order_id": 42, "message_id": "AAMk...", "attachment_id": "a1", "s3_key": "AAMk.../a1_order.pdf"}
```

### 18.3 Retries and the DLQ live in AWS, not in this code

**Configure a redrive policy on the execution queue** pointing at the dead-letter queue with `maxReceiveCount` (e.g. 3). This code's only job is to **delete a message when, and only when, the work actually succeeded**. A message that throws is left on the queue, becomes visible again after `SQS_VISIBILITY_TIMEOUT`, and is redelivered until `maxReceiveCount` is exhausted — at which point SQS itself moves it to the DLQ. Nothing here deletes a failed message, which is what makes that work.

**A bug worth recording, because it silently defeats the whole mechanism.** `_mark_failed()` stamps the row `FAILED` so a human can see it struggling. The duplicate-delivery guard originally treated any non-`NEW`/`QUEUED`/`PROCESSING` status as "already done" and deleted the message — which meant the *first* retry of a failed order was discarded as a duplicate, so retries and the DLQ never happened at all. `FAILED` is therefore **deliberately retryable**; only genuinely *decided* states (`NEEDS_REVIEW`, `AUTO_APPROVED`, `APPROVED`, `REJECTED`, `NOT_AN_ORDER`) count as done. Caught by the drain test, not by reading the code.

### 18.4 Idempotency

SQS is at-least-once, so the same message can arrive twice. That is safe here because:
1. The `Order` row exists **before** the message is sent, and `(message_id, attachment_id)` is unique — re-ingesting the same attachment is impossible.
2. `process_queued_message()` drops a message whose order is already decided, without re-calling the LLM.
3. A message pointing at a deleted order row is dropped rather than cycled to the DLQ — retrying it could never help.
4. An unparseable message body is left on the queue for the DLQ.

### 18.5 Settings

| Key | Default | Notes |
|---|---|---|
| `USE_SQS` | `False` | `True` switches the poller to ingest-only and starts the worker job |
| `SQS_QUEUE_URL` | — | execution queue |
| `SQS_DLQ_URL` | — | monitoring only; **AWS** does the redrive, not this code |
| `SQS_VISIBILITY_TIMEOUT` | `300` | must comfortably exceed one extraction (LLM calls run 30–120 s) or SQS redelivers work still in progress |
| `SQS_WAIT_TIME_SECONDS` | `20` | long polling — an idle queue costs one cheap request, not a busy loop |
| `SQS_MAX_MESSAGES` | `10` | per receive (AWS maximum) |
| `SQS_MAX_BATCHES` | `5` | caps one worker tick so a backlog cannot make a single run last forever |
| `SQS_WORKER_MINUTES` | `1` | worker interval |

### 18.6 Scheduling and scaling

`scheduler.py` now registers two jobs: `poll_outlook` (always) and `drain_sqs` (only when `USE_SQS=True`). Both log a clean named `ConfigError` and keep running when unconfigured.

**The SQS worker is safe to run in multiple processes** — that is the point of a queue; SQS hands a message to one consumer at a time and the idempotency guards above cover redelivery. **The mailbox poller is not** — keep it to a single process, or two schedulers will race on the same unread mail.

`GET /api/health/` now reports `"sqs"`, either `not_configured: SQS_QUEUE_URL` or `disabled (inline mode)`.

### 18.7 Verified (mocked SQS/S3, no credentials)

Ingest→worker round trip (`QUEUED` → worker → `NEEDS_REVIEW`, message deleted, audit `EMAIL_RECEIVED → QUEUED → EXTRACTED`); failure leaves the message on the queue; retry after visibility expiry succeeds and recovers the row from `FAILED`; duplicate delivery of a decided order makes **no** LLM call; missing order row dropped; garbage body left for the DLQ; inline mode regression-tested unchanged; both scheduler jobs register and fail cleanly with no credentials.

**Still not built:** the CloudWatch alarm on DLQ depth (diagram's "CloudWatch alarm fires") — that is queue/infra configuration plus §13's CloudWatch item, not application code.

---

# Appendix A — OE master pgvector table (product-centric, queried DIRECTLY by this backend)

**Superseded design, corrected live during AWS testing.** The original version of this appendix put an HTTP-hop RAG agent between this backend and pgvector (`OE_RAG_URL`), with the schema built around matching *customers*. Both were wrong, corrected by the user directly:

1. **No separate agent, no URL.** The backend opens its own connection to the SAME Postgres instance `DATABASES['default']` already uses (`DB_HOST`/`DB_NAME`/etc.) and runs the cosine-similarity SQL itself — `services.query_oe_master()`, via `django.db.connection`. `OE_RAG_URL`/`OE_RAG_API_KEY`/`OE_RAG_TIMEOUT` are gone; `OE_MATCHING_ENABLED` is the new on/off switch.
2. **OE code is derived from the PRODUCT, not the customer** — confirmed by real master-data columns the user supplied (`reference-images/masterdata_embeddings.jpeg`), not by guessing from Appendix A's original customer-record shape.
3. **`uom` (`30P`/`1P`/`10P`) is the PACK SIZE, and it is a REQUIRED EXACT MATCH** — confirmed directly by the user, not inferred. The same product at a different pack size is a *different* `oe_code`, and matching the right product with the wrong pack is simply the wrong SKU, not a near-miss. This replaced an earlier, wrong guess in this appendix that `product_type` (`00`/`05`) encoded a trial-vs-full-pack distinction — `uom` already says so directly; that guess added a layer of speculation the data didn't need.

## A.1 The real schema

Columns confirmed directly from the user's master-data export:

| Column | Example | Notes |
|---|---|---|
| `product_type` | `00`, `05` | Meaning genuinely unconfirmed - **not** assumed to mean trial-vs-full-pack (see point 3 above; `uom` already encodes pack size directly). Stored and returned to the reranker, but nothing currently depends on decoding it. |
| `oe_code` | `1FA`, `SFB`, `SFZ` | The answer. |
| `uom` | `30P`, `1P`, `10P` | **Pack size - a required exact match**, not a soft signal. `prompts/oe_rerank.txt` treats a pack-size mismatch as disqualifying regardless of how well everything else matches. Also feeds `SAPUoM_Right`/`SAPUoM_Left` in the CSV (§15). |
| `fam_code` | `GE`, `GL`, `GT`, `HC`, `HG` | Family/grouping code. Purpose beyond grouping not confirmed - stored and returned, not currently used to filter or rank. |
| `base_curve` | `8.5` | Matches `right_eye.base_curve` / `left_eye.base_curve` from extraction directly - weighed right after pack size. |
| `brand` | `1DL` | Short brand code (e.g. "1-Day"). |
| `brand_name` | `1-DAY DEFINE WITH LACREON` | Marketing name. |
| `variant_name` | `ACCENT`, `FRESH BLUE`, `FRESH GRAYZEL` | Colour/style variant. |
| `type` | `SPHERICAL` | Lens type (presumably also `TORIC`/`MULTIFOCAL` for other rows - not seen in the visible sample). |

**One column is unresolved: column G**, sitting between `brand` (F) and `brand_name` (H) in the source spreadsheet, was cropped out of the screenshot the user shared. Not guessed at, not included in the schema below - confirm with the user and add it if it matters for matching.

**Pack size is not yet a hard SQL filter.** The order's extracted `pack_size` (e.g. "30 pack", "90pk") and the master data's `uom` (e.g. "30P") are unlikely to share an exact string format, so A.3's query is vector-only and the exact-match enforcement currently lives entirely in the LLM reranker's instructions (`prompts/oe_rerank.txt`), not in SQL. Once the real mapping between extracted `pack_size` text and `uom` codes is confirmed against real data, add a normalized `WHERE uom = %s` (or a `CASE`-based pack-size-to-uom translation) to `query_oe_master()` so a wrong-pack candidate cannot beat the threshold on vector similarity alone.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE oe_master (
    id               BIGSERIAL PRIMARY KEY,
    product_type     VARCHAR(10),
    oe_code          VARCHAR(20)  NOT NULL,
    uom              VARCHAR(10),
    fam_code         VARCHAR(10),
    base_curve       NUMERIC(4,2),
    brand            VARCHAR(20),
    brand_name       VARCHAR(255),
    variant_name     VARCHAR(255),
    type             VARCHAR(30),            -- SPHERICAL / TORIC / MULTIFOCAL ...

    -- the exact string that was embedded (see A.2) - keep this so a bad
    -- match is debuggable: you can see what was actually compared
    embedding_text   TEXT NOT NULL,
    embedding        vector(1024) NOT NULL,  -- must match OE_EMBEDDING_DIMENSION

    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    embedding_model  VARCHAR(100),           -- record what generated `embedding`
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX oe_master_embedding_idx ON oe_master
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX oe_master_active_idx ON oe_master (is_active);
```

Build the HNSW index **after** bulk-loading rows, not before.

## A.2 `embedding_text` — the one thing that must match on both sides

`services.build_product_label(brand_name, variant_name, type)` is the single function that turns master-data columns into a display label, e.g. `"1-DAY DEFINE WITH LACREON ACCENT SPHERICAL"`. **Whatever text you embed into `oe_master.embedding` when populating the table must be built the same way this function builds it** — same field order, same normalization (case, whitespace) — or matches degrade silently with no error, just quietly-wrong scores. If you embed something structurally different (e.g. include `base_curve` or `fam_code` in the embedded text), update `build_product_label()` to match, since the query side also calls it (indirectly, via the extracted order's `product_description` free text — see A.3).

## A.3 The query, exactly as the backend runs it

`services.get_query_embedding(text)` embeds the ORDER's extracted `product_description` (free text as written in the email/attachment, e.g. `"1-Day Acuvue Define - Accent Style"` or `"Acuvus oasys astig"`) using Bedrock (`OE_EMBEDDING_MODEL_ID`, default `amazon.titan-embed-text-v2:0`, 1024-dim — **must match `oe_master.embedding`'s dimension and the model used to populate it**, see `OE_EMBEDDING_DIMENSION`).

`services.query_oe_master(embedding, top_k)` then runs, on the same connection Django already has open:

```sql
SELECT product_type, oe_code, uom, fam_code, base_curve, brand,
       brand_name, variant_name, type,
       1 - (embedding <=> %s::vector) AS score
FROM oe_master
WHERE is_active
ORDER BY embedding <=> %s::vector
LIMIT %s
```

No exact-match shortcut on an account/product code — unlike the old customer-centric design, there's no equivalent stated field to shortcut on here (SAP product codes aren't usually written verbatim in a practice's order email the way an account number is). If that turns out to be wrong once real data is seen, add one.

Tuning note unchanged from the original design: with an HNSW index, `SET hnsw.ef_search = 100;` before the query trades a little latency for better recall once the catalog is large.

## A.4 The full decision, in `services.lookup_oe_code()`

1. `OE_MATCHING_ENABLED=False` → skip entirely, reason `"OE_MATCHING_ENABLED is False"`.
2. No `product_description` was extracted from the order → skip, reason `"no product description was extracted to match on"`.
3. Embed the first line item's product description, query `oe_master`.
4. Top candidate score `>= OE_MATCH_THRESHOLD` (0.82 default) → matched directly, **unless** `OE_RERANK_ENABLED` (default `True`), in which case every lookup — not just ambiguous ones — is always explained by the LLM reranker (§16). `OE_RERANK_ENABLED=False` restores the bare-threshold-only behavior with a templated (non-LLM) reason.
5. `rerank_oe_candidates()` sends the slim candidate list (`oe_code`, `name`, `uom`, `base_curve`, `product_type`, `score`) plus order context (`product_descriptions`, `pack_size`, `base_curve`, `trial_only`) to `prompts/oe_rerank.txt`, which treats **pack size (`pack_size` vs candidate `uom`) as a required exact match first** — a right-product-wrong-pack candidate is disqualified outright, not just penalized — then weighs base curve, then brand/variant text match, then lens-type (spherical/toric) consistency.

`Order.oe_uom` is filled from the winning candidate's `uom` (used to be looked up from an HTTP response's `uom` field; now it's a straight SQL column).

## A.5 Verified so far / still open

**Verified with real AWS during testing:** `get_query_embedding()` against real Bedrock Titan v2 returned a genuine 1024-dim vector for `"Daily 1 Day Oasys 90pk"`. `oe_master_status()` (used by `GET /api/health/`) correctly reports `not_configured: DB_ENGINE must be postgres for pgvector` when running on SQLite. The SQL in A.3 has **not yet run against a real `oe_master` table** — that needs the EC2 Postgres box reachable (§18's blocker) and the table actually populated.

**Open, not guessed at:**
- Column G's identity (A.1).
- Whether `product_type` `00`/`05` really means full-pack/trial (A.1) - the reranker is told to punt rather than assume.
- Whether `fam_code` should participate in matching at all, or is purely informational.
- Real row counts / whether an exact-match shortcut is worth adding once real order text is seen against the real catalog.
