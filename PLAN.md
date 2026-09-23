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
    └── order_extraction.txt
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
1. S3 → SQS → DLQ wiring behind `USE_SQS=True`.
2. CloudWatch logging handler and alarms.
3. Duplicate-PO detection (same `customer_po_number` already APPROVED) — more likely now that one email can yield several rows.
4. Orders written in the **email body** with no attachment (currently `FAILED: no supported attachment found`). Send `mail["body"]["content"]` as a `{"text": ...}` block instead of a document block — the same `call_llm` handles it.
5. **ZIP attachments** — unzip in memory, treat each inner file as its own attachment row. Skipped now because it needs a recursion guard and a zip-bomb size cap.
6. Attachments larger than `MAX_ATTACHMENT_MB` — split a large PDF by page range, or route to Bedrock Data Automation for async processing.
7. Merging the several attachments of one email into a single order when they are pages of one document rather than separate orders. Needs a business rule from the user; today each file is its own order.

CSV export (originally listed here) was built ahead of schedule - see §15.

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

# Appendix A — OE master pgvector table (built by the user, not by this backend)

This backend never connects to this database and never computes an embedding. It makes one HTTP call to your RAG agent and stores the answer. This appendix defines the table so your agent and this backend agree.

## A.1 Table

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- for the lexical fallback in A.4

CREATE TABLE oe_master (
    -- ---------- identity ----------
    id                   BIGSERIAL PRIMARY KEY,
    oe_code              VARCHAR(50)  NOT NULL,   -- THE ANSWER the pipeline wants
    oe_description       VARCHAR(255),            -- human label, shown in the review UI

    -- ---------- what the extracted fields are matched AGAINST ----------
    customer_name        VARCHAR(255) NOT NULL,   -- legal/registered name
    customer_aliases     TEXT[],                  -- trading names, abbreviations, old names
    customer_account     VARCHAR(50),             -- SAP sold-to / ship-to - see A.4
    country              VARCHAR(100),
    country_code         CHAR(2),                 -- ISO-3166 alpha-2, always uppercase
    region               VARCHAR(50),             -- EMEA / APAC / NA / LATAM
    city                 VARCHAR(100),
    postal_code          VARCHAR(20),
    address_line         VARCHAR(500),

    -- ---------- SAP context that makes the OE code unique ----------
    sales_org            VARCHAR(10),
    distribution_channel VARCHAR(10),
    division             VARCHAR(10),
    plant                VARCHAR(10),
    currency             CHAR(3),

    -- ---------- the vector ----------
    embedding_text       TEXT NOT NULL,           -- EXACT string that was embedded
    embedding            vector(1024) NOT NULL,   -- dimension must match your model, A.3

    -- ---------- lifecycle ----------
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    valid_from           DATE,
    valid_to             DATE,
    source_file          VARCHAR(255),            -- which master upload this row came from
    embedding_model      VARCHAR(100),            -- which model produced `embedding`
    created_at           TIMESTAMPTZ DEFAULT now(),
    updated_at           TIMESTAMPTZ DEFAULT now()
);
```

## A.2 Indexes

```sql
-- vector search. Cosine, matching the normalised embeddings in A.3.
CREATE INDEX oe_master_embedding_idx ON oe_master
    USING hnsw (embedding vector_cosine_ops);

-- the exact-match shortcut (A.4) - this is the index that does the most work
CREATE INDEX oe_master_account_idx ON oe_master (upper(customer_account))
    WHERE is_active;

-- narrowing vector search by country
CREATE INDEX oe_master_country_idx ON oe_master (country_code) WHERE is_active;

-- lexical fallback for near-miss names
CREATE INDEX oe_master_name_trgm_idx ON oe_master
    USING gin (customer_name gin_trgm_ops);

-- one row per real-world combination; stops duplicate OE codes creeping in
CREATE UNIQUE INDEX oe_master_unique_idx ON oe_master
    (oe_code, coalesce(customer_account,''), coalesce(country_code,''),
     coalesce(sales_org,''));
```

Build the HNSW index **after** bulk-loading the rows, not before — it is far faster that way.

## A.3 The three rules that decide whether this works

**1. Row granularity = one row per (customer × country × sales org), not one per customer.**
If "Acme Health" in India and "Acme Health" in Singapore have different OE codes, they are two rows. The whole point of the vector search is choosing between them, so they must both exist as separate candidates.

**2. `embedding_text` must be built by ONE function, used at index time and query time.**
This is the single most common way a vector search quietly returns garbage: rows embedded as `"ACME HEALTH PVT LTD, MUMBAI, INDIA"` but queried as `"Acme Health Private Limited Mumbai"`. Pick a template and freeze it:

```
{customer_name} | {city} | {country} | {sales_org}
```

Normalise both sides identically before embedding: uppercase; strip punctuation; collapse whitespace; drop legal suffixes (`PVT LTD`, `PRIVATE LIMITED`, `LTD`, `LIMITED`, `GMBH`, `INC`, `LLC`, `SA`, `BV`, `AG`, `CO`). Store the result in `embedding_text` so that when a match looks wrong you can see exactly what was compared. Emit one row per alias in `customer_aliases` too — same `oe_code`, different `embedding_text` — so "J&J", "Johnson and Johnson" and "JNJ" all reach the same code.

**3. `vector(N)` must match your embedding model, and it is fixed at table creation.**

| model | dimension |
|---|---|
| `amazon.titan-embed-text-v2:0` | 1024 (also supports 512 / 256) |
| `amazon.titan-embed-text-v1` | 1536 |
| `cohere.embed-english-v3` / `embed-multilingual-v3` | 1024 |

Confirm the dimension from the model's own response before you `CREATE TABLE` — changing it later means dropping the column and re-embedding everything. Record the model name in `embedding_model` so a future model swap is detectable rather than silently mixing incompatible vectors in one column. Normalise vectors to unit length at insert; then cosine distance `<=>` and inner product agree.

## A.4 Query order — try exact before vector

Roughly 60–80% of real orders carry the customer's SAP account number. Embedding those is wasted latency and a chance to be wrong:

```sql
-- step 1: exact. If the PO gave an account number, trust it.
SELECT oe_code, customer_name, country_code, 1.0 AS score, 'exact' AS match_type
FROM   oe_master
WHERE  is_active AND upper(customer_account) = upper($1)
LIMIT  1;

-- step 2: only if step 1 found nothing - vector search, filtered by country
-- when the extraction gave one (it cuts the candidate set enormously).
SELECT oe_code, customer_name, country_code,
       1 - (embedding <=> $1::vector) AS score,   -- cosine similarity, 0..1
       'vector' AS match_type
FROM   oe_master
WHERE  is_active
  AND  ($2::char(2) IS NULL OR country_code = $2)
  AND  (valid_to IS NULL OR valid_to >= current_date)
ORDER  BY embedding <=> $1::vector
LIMIT  $3;                                        -- top_k, default 5
```

Return `score` as **similarity (higher = better, 0 to 1)**, not raw distance. This backend compares it directly against `OE_MATCH_THRESHOLD=0.82` and stores it in `Order.oe_confidence`.

Tuning note: with an HNSW index, `SET hnsw.ef_search = 100;` before the query trades a little latency for noticeably better recall on a master file of this kind of size.

## A.5 The contract your agent must expose

One POST endpoint. This is all the backend knows about your system.

**Request** — `POST {OE_RAG_URL}`, header `x-api-key: {OE_RAG_API_KEY}`
```json
{
  "customer_name":    "Acme Health Pvt Ltd",
  "customer_account": "0001234567",
  "city":             "Mumbai",
  "country":          "India",
  "ship_to_address":  "Plot 12, MIDC, Andheri East, Mumbai 400093",
  "top_k": 5
}
```
Any value may be `null` — extraction is imperfect and the agent must tolerate a missing account number or country.

**Response**
```json
{"candidates": [
  {"oe_code": "OE-IN-0042", "customer_name": "Acme Health Private Limited",
   "country": "IN", "score": 0.91, "match_type": "vector"},
  {"oe_code": "OE-IN-0067", "customer_name": "Acme Healthcare India",
   "country": "IN", "score": 0.74, "match_type": "vector"}
]}
```
Ordered best-first. Return `{"candidates": []}` when nothing matches — an empty list is a valid answer and sends the order to human review. Do not return an HTTP error for "no match"; reserve non-200 for a genuine failure.

The backend stores the whole list in `Order.oe_candidates` and shows it to the reviewer as a pick-list, so returning 5 weak candidates is more useful than returning none — the human picks the right one in a click, and that click is captured in the audit trail as a `FIELD_EDITED` on `oe_code`.

## A.6 Data you must prepare per row

Minimum viable master file to load: `oe_code`, `customer_name`, `country_code`, `sales_org`, `customer_account`. Everything else in A.1 improves precision but is optional on day one. Include **inactive/expired customers** as rows with `is_active = false` rather than deleting them — an old PO referencing a retired account should be recognisable as retired, not silently unmatched.
