# OPTIC BOT — Knowledge Transfer

This document explains how the OPTIC BOT backend works, file by file and
function by function. Read sections 1–4 first for the big picture, then use
sections 5–10 as a reference while you read the code.

| | |
|---|---|
| Stack | Python 3, Django 5 + Django REST Framework, APScheduler, boto3, MSAL, requests |
| Runs on | EC2 (gunicorn), Postgres + pgvector, AWS S3 / SQS / Bedrock, Microsoft Graph |
| Main code | `optic_bot/services.py` (the pipeline), `optic_bot/views.py` (the API) |
| Tests | `optic_bot/tests.py` (unit), `optic_bot/tests_e2e.py` (end to end) — 35 tests |

---

## Contents

1. [What the system does](#1-what-the-system-does)
2. [Architecture](#2-architecture)
3. [Repository map](#3-repository-map)
4. [The life of one email, step by step](#4-the-life-of-one-email-step-by-step)
5. [Data model — `models.py`](#5-data-model--modelspy)
6. [The pipeline — `services.py`](#6-the-pipeline--servicespy)
7. [The API — `views.py` and `urls.py`](#7-the-api--viewspy-and-urlspy)
8. [Background jobs — `scheduler.py` and `apps.py`](#8-background-jobs--schedulerpy-and-appspy)
9. [Configuration — `settings.py` / `.env`](#9-configuration--settingspy--env)
10. [Prompts](#10-prompts)
11. [Tests](#11-tests)
12. [Setup scripts](#12-setup-scripts)
13. [Operations runbook](#13-operations-runbook)
14. [Known limitations and open items](#14-known-limitations-and-open-items)
15. [Glossary](#15-glossary)

---

## 1. What the system does

Opticians' practices email contact-lens orders to a shared J&J mailbox. Some
send a PDF/Excel/Word order form; many just type the prescription into the
email body ("Trials for Wayne Ying / R -7.00 / -1.75 x 20"). The mailbox also
receives plenty of mail that is *not* an order — delivery chasers, invoice
queries, out-of-office replies.

OPTIC BOT:

1. **Watches** the `OPTIC BOT` folder of the shared mailbox (Microsoft Graph).
2. **Classifies** each new email with an LLM: *order* or *communication*.
3. **Moves** order emails into the `01 New Orders` sub-folder.
4. **Stores** each order email's body text and attachments in S3.
5. **Extracts** the order (account, PO, patient, per-eye sphere/cylinder/axis…)
   with an LLM (Claude on AWS Bedrock), with a confidence score per field.
6. **Matches** the product to a SAP **OE code** using pgvector similarity search
   over the product master, plus an LLM that picks and explains the match.
7. **Decides**: every field confident *and* OE matched → `AUTO_APPROVED`;
   otherwise → `NEEDS_REVIEW` for a human.
8. Lets a **human reviewer** (through a separate frontend calling this API)
   correct fields, approve or reject.
9. **Exports** approved orders as a SAP CSV to the S3 output bucket, including
   the email's Graph id (`GraphMailId`) for traceability.

---

## 2. Architecture

```
                         Microsoft Graph (shared mailbox)
                     ┌─────────────────────────────────────┐
                     │ Inbox                               │
                     │  └─ OPTIC BOT          ◄── polled   │
                     │       └─ 01 New Orders ◄── moved to │
                     └──────────────┬──────────────────────┘
                                    │ HTTPS (MSAL app token)
 ┌──────────────────────────────────▼───────────────────────────────────┐
 │ Django app on EC2                                                    │
 │                                                                      │
 │  scheduler.py ──every 5 min──► services.poll_mailbox()               │
 │  POST /api/poll/ ────────────► (same function, same lease)           │
 │                                                                      │
 │     1 triage  : classify (LLM) ─► move to 01 New Orders              │
 │     2 ingest  : body + attachments ─► S3 input bucket, Order rows    │
 │     3 extract : LLM extraction ─► score ─► OE lookup ─► status       │
 │        (inline, or via SQS queue + drain_sqs worker)                 │
 │                                                                      │
 │  views.py  REST API ◄──── reviewer frontend (approve / edit / export)│
 └───────┬──────────────┬──────────────┬───────────────┬────────────────┘
         │              │              │               │
    Postgres       S3 input/       SQS queue       Bedrock
    (orders,       output          (+ DLQ)         (Claude: classify,
     audit,        buckets                          extract, OE rerank;
     watermark,                                     Titan: embeddings)
     oe_master
     + pgvector)
```

**Two execution modes** (`USE_SQS`):

* `USE_SQS=False` — **inline**. The poller does everything in one pass.
  Simple; good for testing.
* `USE_SQS=True` — **queued**. The poller only ingests (S3 + DB row) and sends
  one small SQS message per order. A worker job (`drain_sqs`, every minute)
  does the LLM extraction. Failures are retried by SQS and end up in the DLQ.

---

## 3. Repository map

```
TestBot/
├── manage.py                 Django entry point
├── requirements.txt
├── .env.example              every setting, documented (copy to .env)
├── README.md / PLAN.md       original design notes
├── API_REFERENCE.md          request/response examples for every endpoint
├── KNOWLEDGE_TRANSFER.md     this file
├── opticbot/                 Django PROJECT package
│   ├── settings.py           all configuration, read from .env
│   ├── urls.py               mounts optic_bot.urls under /api/
│   ├── wsgi.py / asgi.py     server entry points
├── optic_bot/                Django APP package — all the real code
│   ├── models.py             Order, MailWatermark, AuditLog
│   ├── services.py           the pipeline (sections A–G, ~2,200 lines)
│   ├── views.py              REST endpoints
│   ├── urls.py               endpoint routes
│   ├── scheduler.py          APScheduler jobs
│   ├── apps.py               starts the scheduler on boot
│   ├── migrations/           0001 … 0007
│   ├── tests.py              unit tests (poll logic, watermark, CSV)
│   └── tests_e2e.py          end-to-end tests with fake Graph/S3/SQS/LLM
├── prompts/                  LLM prompts as text files (editable without deploy)
│   ├── email_classification.txt
│   ├── order_extraction.txt
│   └── oe_rerank.txt
└── scripts/                  (gitignored) one-off AWS / Postgres setup helpers
```

---

## 4. The life of one email, step by step

This is the single most useful section. Function names link to section 6.

### 4.1 A poll starts

The scheduler (or `POST /api/poll/`) calls **`poll_mailbox()`**.

1. **Lease.** `acquire_poll_lease()` does an atomic `UPDATE` on the
   `MailWatermark` row. If another poll holds the lease, this call returns
   `{"busy": true}` immediately. Only one poll ever runs at a time — across
   threads, processes and servers.
2. **Test limit.** If `MAIL_TEST_LIMIT` is set and already reached, return now
   (no Graph call at all).
3. **Graph login + folders.** `get_graph_token()` → `build_headers()` →
   `get_folder_id()` (finds `OPTIC BOT` under Inbox) →
   `get_orders_folder_id()` (finds `01 New Orders` under `OPTIC BOT`).

### 4.2 Which emails are "new"? — the watermark

The bot does **not** use the unread flag (a person opening an email would hide
it from the bot). Instead it keeps a **watermark**: the `receivedDateTime` of
the newest email it has finished with, stored in `MailWatermark.received_at`.

* **First run** (watermark empty): read back `MAIL_FIRST_RUN_DAYS` days
  (`0` = the whole folder, any age).
* **Every later run**: read from *watermark − 60 minutes*
  (`MAIL_OVERLAP_MINUTES`). The overlap catches mail that syncs late.
* Emails already in the DB are **skipped** before any LLM or full-email fetch,
  so the overlap is free.

`fetch_emails_since()` lists the folder **oldest first**, only `id` and
`receivedDateTime` (light), 500 per page, using **keyset paging** (see 6.B —
this is important, it prevents emails being skipped while others are moved).

### 4.3 Triage (per new email, up to `MAIL_BATCH_SIZE` = 20 per poll)

For each listed email with no Order rows yet:

1. `renew_poll_lease()` — keep the lease alive during long polls.
2. `get_message()` — fetch the full email (subject, sender, plain-text body).
3. `triage_email()`:
   * `list_attachments()` — keep only real file attachments with an allowed
     extension; drop inline images (signature logos).
   * `classify_email()` — one LLM call: ORDER or COMMUNICATION.
     **Fails open**: any doubt or error → treated as an order.
   * **Communication** → one `Order` row with `status=NOT_AN_ORDER`,
     `attachment_id="EMAIL"`; the email stays in `OPTIC BOT`; marked read.
   * **Order** → `move_email()` to `01 New Orders`. Because every Graph call
     asks for **immutable ids**, the email keeps the **same id** after the move.

### 4.4 Ingest (for each email just moved)

`process_order_email()`:

1. `upload_body_to_s3()` → `s3://<input>/<graph-id>/body.txt` (full body).
2. If there are **attachments**: for each one, `queue_attachment()` (SQS mode)
   or `process_attachment()` (inline) → `ingest_attachment()`:
   * creates an `Order` row (`status=NEW`) — unique on
     `(message_id, attachment_id)`, so it can never be created twice;
   * `download_attachment()` → `upload_to_s3()` at
     `<graph-id>/<attachment-id>_<file name>`.
3. If there are **no attachments**: `process_body_order()` — the order is in
   the body; one `Order` row with `attachment_id="BODY"`.
4. `mark_email_read()` — cosmetic, for humans.

### 4.5 Extraction

Inline: straight away. SQS mode: `enqueue_order()` sends
`{order_id, message_id, attachment_id, s3_key}`; later the worker
(`drain_sqs_queue()` → `process_queued_message()`) loads the file back from S3
(`_load_file_bytes()`) and continues here.

`run_extraction()` → `_extract_and_score()`:

1. Build the prompt from `prompts/order_extraction.txt` (sender, subject, body).
2. `call_llm()` — Bedrock `converse`, with the document attached
   (`build_content_block()`), temperature 0.
3. `parse_llm_json()` — the model returns nested JSON; every leaf is
   `{"value": …, "confidence": 0.0–1.0}`.
4. `is_order: false` → `NOT_AN_ORDER` (e.g. a price list attached by mistake).
5. `score()` — `min_confidence` = the lowest confidence among populated fields.
6. `lookup_oe_code()` — embed the product description (Titan), cosine search
   `oe_master` (pgvector), then `rerank_oe_candidates()` asks the LLM to pick
   and explain.
7. **Decision**: `min_confidence >= CONFIDENCE_THRESHOLD` (0.85) **and**
   OE matched (if `REQUIRE_OE_MATCH`) → `AUTO_APPROVED`, else `NEEDS_REVIEW`.

### 4.6 After the batch

* **Watermark** advances to the newest email in the *unbroken* run of handled
  emails (`advance_watermark()`). If an email failed, the watermark stops just
  before it so it is retried next poll. If it keeps failing for
  `MAIL_RETRY_WINDOW_HOURS` (72 h) the bot gives up, writes a `FAILED` row
  with `attachment_id="TRIAGE"` (`_record_given_up_email()`), and moves on.
* **Sweep** (`sweep_orders_folder()`): lists `01 New Orders` and ingests any
  email that has **no Order rows** — an ingest that failed right after the
  move, or an email someone dragged in by hand.
* The lease is released.

### 4.7 Review and export (human, through the API)

`GET /api/orders/` → reviewer opens one → `PATCH …/fields/` to correct →
`POST …/approve/` → `POST …/export/` → `export_order_to_csv()` →
`s3://<output>/exports/<date>/order-<id>-<PO>.csv`, one row per line item,
last column `GraphMailId`.

### 4.8 Status lifecycle

```
                     ┌──────────► NOT_AN_ORDER   (communication, or LLM says not an order)
                     │
 NEW ──► (QUEUED) ──► PROCESSING ──► AUTO_APPROVED ──► (exported)
                     │           └─► NEEDS_REVIEW ──► APPROVED ──► (exported)
                     │                            └─► REJECTED
                     └──────────► FAILED  (exception; SQS retries FAILED rows,
                                           TRIAGE rows = bot gave up)
```

`QUEUED` exists only in SQS mode. "Exported" is not a status — it is
`exported_at` / `export_s3_key` being set; export can be repeated.

---

## 5. Data model — `models.py`

### `class Order(models.Model)`

**One row per attachment** (not per email). An email with 3 PDFs → 3 rows
sharing one `message_id`. Special `attachment_id` values:

| `attachment_id` | Meaning |
|---|---|
| Graph attachment id | a real attachment |
| `"BODY"` | order typed into the email body, no attachment |
| `"EMAIL"` | whole email classified as communication (`NOT_AN_ORDER`) |
| `"TRIAGE"` | the bot gave up on this email after 72 h of failures (`FAILED`) |

**Unique constraint:** `(message_id, attachment_id)` — the dedupe guard.

| Field | Type | Meaning |
|---|---|---|
| `message_id` | char(255), indexed | Graph **immutable** message id. Same in `OPTIC BOT` and `01 New Orders`. = S3 prefix = CSV `GraphMailId` |
| `sender` | char | from-address |
| `subject` | char(500) | |
| `received_at` | datetime | Graph `receivedDateTime` |
| `body_text` | text | plain-text body, truncated to `MAX_BODY_CHARS` (full body is in S3) |
| `attachment_id` | char | see table above |
| `attachment_name` | char | e.g. `PO_4471.pdf`, `(email body)` |
| `file_type` | char(10) | `pdf`, `xlsx`, `body`, `email` … |
| `file_size` | int | bytes |
| `s3_key` | char | input-bucket key of the document (empty for BODY/EMAIL) |
| `extracted_data` | JSON | `{"order": {...}, "line_items": [...], "extraction_notes": "..."}`, every leaf `{"value","confidence"[,"edited"]}` |
| `min_confidence` | float, indexed | lowest populated-field confidence |
| `oe_code` | char | chosen SAP OE code |
| `oe_confidence` | float | similarity / LLM confidence |
| `oe_matched` | bool | OE match accepted |
| `oe_candidates` | JSON list | top-k candidates, shown to reviewer as a pick-list |
| `oe_uom` | char | SAP unit of measure of the winner (e.g. `5P`) |
| `oe_match_reason` | text | plain-English reason for the OE decision (audit) |
| `status` | char, indexed | see 4.8 |
| `reviewed_by`, `reviewed_at`, `review_comment` | | human review |
| `error_message` | text | why it FAILED / why NOT_AN_ORDER |
| `exported_at`, `export_s3_key` | | last CSV export |
| `created_at`, `updated_at` | auto | |

`__str__` → `Order #12 [NEEDS_REVIEW] PO_4471.pdf`.

### `class MailWatermark(models.Model)`

**The poller's memory.** One row, `name="incoming"`.

| Field | Meaning |
|---|---|
| `name` | unique key (`"incoming"`) |
| `received_at` | the watermark. `NULL` = first run not done yet. Reset to `NULL` to re-run a first run |
| `stuck_since` | when the current failure pinning the watermark started; drives the 72 h give-up |
| `sweep_from` | oldest email that was moved but whose ingest left no rows; the sweep reaches back at least this far until it is cleared |
| `locked_until` | the poll lease; `NULL` or in the past = free |
| `updated_at` | auto |

### `class AuditLog(models.Model)`

One row per event on an order: `EMAIL_RECEIVED`, `QUEUED`, `EXTRACTED`,
`AUTO_APPROVED`, `FIELD_EDITED`, `APPROVED`, `REJECTED`, `FAILED`, `EXPORTED`.
Fields: `order` (FK, cascade), `action`, `actor` (`system` or reviewer),
`field_name`, `old_value`, `new_value`, `note`, `created_at`. Oldest first.

### Migrations

| # | What |
|---|---|
| 0001 | Order + AuditLog |
| 0002 | `exported_at`, `export_s3_key` |
| 0003 | `oe_match_reason` |
| 0004 | `oe_uom` |
| 0005 | status choices (QUEUED) |
| 0006 | `MailWatermark` (`received_at`, `stuck_since`) |
| 0007 | `MailWatermark.sweep_from`, `locked_until` |

---

## 6. The pipeline — `services.py`

**Rule for this file:** no boto3 / MSAL client is ever built at import time.
Every client is built inside a function after `require()` has checked its
settings — so the app boots with an empty `.env`.

The file is split into sections A–G. Every function is listed below in file
order with: **what it does**, **inputs → output**, **side effects**, and
**gotchas**.

### Section A — config guard and shared helpers

#### `class ConfigError(Exception)`
Raised when a required setting is missing (or a folder is not found). Views
turn it into HTTP 400; the scheduler logs it and waits for the next tick.

#### `require(*setting_names)`
Raises `ConfigError("Missing X, Y in .env")` naming **every** missing setting.
No return value.

#### `aws_credential_kwargs() → dict`
Returns explicit `aws_access_key_id/secret` if both are set in `.env`,
otherwise `{}` so boto3 uses its default chain (env vars, `~/.aws`, **EC2
instance role** — the production path). Deliberately does not `require()`.

#### `aws_credentials_status() → str`
Real check for `/api/health/`: one STS `GetCallerIdentity` call.
`"ok (account 1234…)"` or the error text.

#### `oe_master_status() → str`
Real check for `/api/health/`: is pgvector installed, does `oe_master` have
active rows? Returns a human-readable status telling you the next missing step.

#### `log_audit(order, action, actor="system", field_name="", old_value="", new_value="", note="")`
Creates one `AuditLog` row. Every status change and field edit goes through it.

### Section B — Outlook / Microsoft Graph

Module constant `GRAPH_BASE = "https://graph.microsoft.com/v1.0"`.
`_token_cache` holds the MSAL token in memory.

#### `get_graph_token() → str`
MSAL client-credentials login (`MS_CLIENT_ID/SECRET/TENANT_ID`). Caches the
token until 60 s before expiry. Raises `ConfigError` if settings are missing
or login fails.

#### `build_headers(token) → dict`
The headers used on **every** Graph call:
```
Authorization: Bearer <token>
Prefer: outlook.body-content-type="text", IdType="ImmutableId"
```
* `body-content-type="text"` — Graph converts HTML bodies to plain text (much
  cleaner than stripping tags ourselves).
* `IdType="ImmutableId"` — a message keeps the **same id** when moved between
  folders. The whole design (DB key, S3 prefix, CSV `GraphMailId`) relies on
  it. ⚠ Immutable and normal ids are different formats — never mix them.

#### `get_folder_id(headers) → str`
Lists Inbox's child folders and returns the id of `MAIL_TARGET_FOLDER`
(`OPTIC BOT`), case-insensitive. `ConfigError` if not found.

#### `get_orders_folder_id(headers, parent_folder_id) → str`
Same, for `MAIL_ORDERS_FOLDER` (`01 New Orders`) under `OPTIC BOT`.

#### Constants
| Name | Value | Purpose |
|---|---|---|
| `MAIL_FIELDS` | `id,subject,from,receivedDateTime,body,hasAttachments,isRead` | full-email `$select` |
| `SCAN_FIELDS` | `id,receivedDateTime` | light folder scan |
| `SCAN_PAGE_SIZE` | 500 | Graph page size |
| `MAX_PAGES` | 60 | safety cap per listing (30k ids) |

#### `_graph_iso(dt) → str`
Datetime → `2026-09-28T10:00:00Z` for Graph `$filter`.

#### `_iter_pages(headers, url, params)` (generator)
Follows Graph's `@odata.nextLink`, yielding items lazily. Used **only** for
the orders-folder sweep listing (which does not move emails). Stops after
`MAX_PAGES` with a warning.

#### `fetch_emails_since(headers, folder_id, since)` (generator)
Yields `{id, receivedDateTime}` for every email in the folder received at or
after `since` (`None` = no lower bound), **oldest first**.

**Keyset paging, not `nextLink`** — Graph's `nextLink` pages with `$skip`,
and the poller *moves emails out* of this folder while iterating, which would
shift the list and silently skip emails. So each page is a fresh query
`receivedDateTime ge <last seen>`, de-duplicated by id. Lazy: stops fetching as
soon as the caller stops iterating (a 10k folder is not downloaded to process
20). ⚠ Limit: more than 500 emails with the *exact same* received second would
stop the scan (only plausible after a bulk import).

#### `list_message_ids_since(headers, folder_id, since) → list[str]`
All message ids in a folder since `since` — used by the sweep.

#### `get_message(headers, message_id) → dict`
One full email (`MAIL_FIELDS`).

#### Watermark and lease helpers

`WATERMARK_NAME = "incoming"`, `POLL_LEASE_MINUTES = 30`.

| Function | What it does |
|---|---|
| `get_watermark_row()` | get-or-create the single `MailWatermark` row |
| `advance_watermark(row, received_at)` | moves `received_at` **forward only** |
| `acquire_poll_lease() → bool` | atomic `UPDATE … WHERE locked_until IS NULL OR < now` → sets it to now + 30 min. Works on Postgres and SQLite, across servers |
| `renew_poll_lease()` | pushes the lease out another 30 min; called before each email that needs work, so a long inline poll never loses its lease |
| `release_poll_lease()` | clears the lease (always, via `finally`) |
| `set_stuck_since(row, when)` | sets/clears the give-up clock |
| `fetch_floor(watermark)` | where the fetch starts: `watermark − overlap`; first run → `now − MAIL_FIRST_RUN_DAYS` or `None` (everything). **No age cap** on the watermark, so a big backlog drains without gaps |
| `sweep_floor(watermark)` | where the sweep starts: `watermark − MAIL_RETRY_WINDOW_HOURS`; first run → same as `fetch_floor(None)` |

#### `clean_body(mail, truncate=True) → str`
Plain-text body, whitespace-normalised. Falls back to a regex tag-stripper if
Graph ever returns HTML. `truncate=True` cuts to `MAX_BODY_CHARS` (DB/LLM);
`False` keeps everything (S3 copy).

#### `list_attachments(headers, message_id) → list[dict]`
Attachment **metadata** (no content). Keeps only
`#microsoft.graph.fileAttachment`, skips inline images when
`SKIP_INLINE_ATTACHMENTS`, skips extensions not in `ALLOWED_EXTENSIONS`.
**No size filter** — a 50 MB order is still an order.

#### `download_attachment(headers, message_id, attachment_id) → bytes`
Raw bytes via the `/$value` endpoint.

#### `move_email(headers, message_id, destination_folder_id) → str`
`POST /messages/{id}/move`. Returns the id Graph reports. With immutable ids
it equals the input; the caller logs a warning if it ever doesn't.

#### `mark_email_read(headers, message_id)`
`PATCH isRead=true` if `MARK_MAIL_AS_READ`. Cosmetic only — never used to
decide what to fetch.

### Section C — S3 and OE lookup

#### `s3_client()`
boto3 S3 client (region + optional static keys). Used for both buckets.

#### `upload_to_s3(file_bytes, key, bucket=None) → key`
`put_object` with `ServerSideEncryption="aws:kms"`. Default bucket =
`S3_INPUT_BUCKET`. `ConfigError` if no bucket configured.

#### `body_s3_key(message_id) → str`
`"<message_id>/body.txt"`.

#### `upload_body_to_s3(mail) → key | None`
Uploads the **full** plain-text body once per email. No-op if
`S3_INPUT_BUCKET` is empty.

#### `presigned_url(key, seconds=3600, bucket=None) → str | None`
Temporary download link for the reviewer UI. Never raises.

**S3 layout**
```
<S3_INPUT_BUCKET>/
    <graph-id>/body.txt
    <graph-id>/<attachment-id>_<file name>
<S3_OUTPUT_BUCKET>/
    exports/<YYYY-MM-DD>/order-<order id>-<PO>.csv
```

#### `get_query_embedding(text) → list[float]`
Bedrock `invoke_model` with `OE_EMBEDDING_MODEL_ID` (Titan v2, 1024-dim,
normalised). ⚠ Must be the same model/dimension that filled `oe_master`.

#### `build_product_label(brand_name, variant_name, lens_type=None) → str`
Joins master-data columns into a readable product label.

#### `query_oe_master(embedding, top_k) → list[dict]`
SQL cosine search: `ORDER BY embedding <=> vector LIMIT k` on active rows.
Returns candidates `{oe_code, name, uom, product_type, fam_code, base_curve,
brand, score, match_type}`. Raises on DB errors (caller handles).

#### `lookup_oe_code(fields) → (oe_code, confidence, candidates, matched, reason)`
1. Takes the **first line item's** product description.
2. Embeds it, queries `oe_master`.
3. If `OE_RERANK_ENABLED` (default): LLM picks + explains
   (`rerank_oe_candidates`), accepted if confidence ≥ `OE_RERANK_THRESHOLD`.
   Otherwise: accept the top vector hit if score ≥ `OE_MATCH_THRESHOLD`.
**Never raises**; `reason` is always a non-empty string.

#### `rerank_oe_candidates(fields, candidates) → (oe_code|None, confidence, reason)`
Text-only LLM call with `prompts/oe_rerank.txt`, giving order context (product
descriptions, pack size, base curve, trial-only) and slimmed candidates.
Rejects any code the model invents that was not in the candidate list. Never
raises.

### Section D — LLM

`DOC_FORMATS` / `IMAGE_FORMATS` map file extensions to Bedrock block formats.

#### `safe_doc_name(file_name) → str`
Bedrock rejects document names with dots/underscores —
`"PO_4471.v2.xlsx"` → `"PO 4471 v2"`.

#### `build_content_block(file_bytes, file_name, provider=None) → dict`
Image → `{"image": …}`, document → `{"document": …}`. Base64 for the HTTP
gateway, raw bytes for boto3. Raises `ValueError` if the file exceeds
`LLM_MAX_DOCUMENT_MB` (the order is then saved `FAILED` with a clear message —
the file is still in S3). `ConfigError` for unsupported extensions.

#### `load_prompt(name) → str`
Reads `prompts/<name>.txt`. Prompts use `{placeholders}`, so literal JSON
braces inside them are doubled `{{ }}`.

#### `_check_not_truncated(stop_reason)`
Raises a clear error when the model hit `LLM_MAX_TOKENS` (instead of a
confusing JSON parse error).

#### `call_llm(prompt_text, file_bytes=None, file_name=None) → str`
One call to Claude. `LLM_PROVIDER="bedrock"` → boto3 `converse`;
`"gateway"` → HTTP POST to `LLM_GATEWAY_URL`. Document block first, then the
text. Temperature 0. Returns the model's text.

#### `parse_llm_json(text) → dict`
Strips ```` ```json ```` fences, `json.loads`. Raises a readable `ValueError`.

#### `classify_email(mail, attachment_names) → (is_order, confidence, reason)`
Text-only LLM triage with `prompts/email_classification.txt`. Returns
not-an-order **only** if the model says COMMUNICATION with confidence ≥
`EMAIL_CLASSIFICATION_THRESHOLD`. **Fails open**: disabled, error or junk →
treated as an order (extracting a non-order is cheap; losing an order is not).

#### `collect_confidences(node, skip_null_fields=True)` (generator)
Walks the nested extraction and yields every leaf confidence. Leaves whose
value is null are skipped (a spherical lens has no cylinder — that must not
drag the score to 0).

#### `score(fields) → float`
`min(collect_confidences(fields))`, or 0.0 if nothing was extracted.

#### `collect_low_confidence_paths(node, threshold, prefix="")` (generator)
Dotted paths of populated leaves below `threshold`, e.g.
`line_items.0.right_eye.sphere`. The UI highlights these; the same paths are
what `PATCH /fields/` accepts.

#### `set_by_path(data, path, value) → old_value`
Writes a leaf by dotted path as `{"value": v, "confidence": 1.0, "edited": true}`.
Numeric segments index lists.

#### `_sender_of(mail) → str`
`mail.from.emailAddress.address`, safely.

### Section E — the pipeline

#### `_extract_and_score(order, file_bytes=None)`
The core extraction step (shared by inline, SQS worker and reprocess). Builds
the prompt, calls the LLM, parses, sets `extracted_data`, `min_confidence`,
all `oe_*` fields, and `status` (`NOT_AN_ORDER` / `AUTO_APPROVED` /
`NEEDS_REVIEW`). **Does not save** — the caller saves (so it can still save a
FAILED row on error). `file_bytes=None` = body-only order (text-only call).

#### `ingest_attachment(mail, att, headers) → (order, file_bytes) | (None, None)`
Creates the `Order` row (`NEW`) + audit `EMAIL_RECEIVED`, downloads the file,
uploads it to S3 and saves `s3_key`. Returns `(None, None)` if this attachment
already has a row (dedupe). No LLM call.

#### `run_extraction(order, file_bytes) → dict`
Sets `PROCESSING`, runs `_extract_and_score`, saves, writes audit entries.
**Raises** on failure (caller decides what failure means).

#### `_mark_failed(order, error)`
`status=FAILED`, `error_message`, audit `FAILED`.

#### `process_attachment(mail, att, headers) → dict`
**Inline mode**: ingest + extract. Any error → the row is saved `FAILED`
(nothing is silently lost). Returns `{"status": …}`.

#### `queue_attachment(mail, att, headers) → dict`
**SQS mode**: ingest + `enqueue_order()`. No LLM here.

#### `process_body_order(mail, headers) → dict`
Order typed in the body: creates the `BODY` row, then enqueues (SQS) or
extracts text-only (inline). Errors → `FAILED` row.

#### `_record_communication_email(mail, reason, confidence) → Order | None`
Writes the single `NOT_AN_ORDER` row (`attachment_id="EMAIL"`) for a
communication email, so the mailbox is fully auditable and a
misclassification can be spotted.

#### `triage_email(mail, headers, orders_folder_id) → (outcome, error)`
Per email in `OPTIC BOT`. `outcome` is one of:
* `"skipped"` — already has Order rows;
* `"not_order"` — classified as communication (row written, left in place);
* `"moved"` — classified as order and moved to `01 New Orders`;
* `"failed"` — listing attachments or the move failed (retried next poll).

Logs a warning if the move ever returns a different id.

#### `process_order_email(mail, headers) → summary dict`
Per email **in `01 New Orders`**: body → S3, then each attachment through
`queue_attachment`/`process_attachment`, or `process_body_order` if none.
Marks the email read when every attachment reached a terminal state. Safe to
call twice for the same email.

#### `poll_mailbox() → summary dict`
**Public entry point** (scheduler + `POST /api/poll/`). Takes the lease; if
busy returns `{"busy": true, "errors": []}`; otherwise runs
`_poll_mailbox_locked()` and always releases the lease.

#### `_poll_mailbox_locked() → summary dict`
The poll itself (see section 4). Summary keys:

| Key | Meaning |
|---|---|
| `emails_checked` | emails newly triaged this poll |
| `moved_to_orders` | moved to `01 New Orders` |
| `swept` | recovered by the sweep |
| `attachments_found`, `orders_created`, `queued`, `skipped`, `not_orders`, `failed` | per attachment |
| `errors` | list of error strings |
| `test_limit`, `test_limit_reached` | only when `MAIL_TEST_LIMIT` is set |

Watermark rules implemented here:
* advances only over an **unbroken** run of handled emails;
* the first failure pins it and starts `stuck_since`; after
  `MAIL_RETRY_WINDOW_HOURS` the email is given up (`TRIAGE` row) and the
  watermark moves past it;
* an email that was moved but whose ingest left no rows sets `sweep_from`
  so the sweep is guaranteed to reach it, even if the watermark jumps far.

#### `mail_limit_room() → int | None`
`None` if `MAIL_TEST_LIMIT` is 0 (off). Otherwise `limit − (distinct
message_ids with Order rows)`, never negative. Counted from the DB, so it
survives restarts.

#### `sweep_orders_folder(headers, orders_folder_id, since, skip_ids, summary, merge, budget) → bool`
Lists `01 New Orders` ids since `since`, ingests those with no Order rows (up
to `budget`), skipping ids already attempted this poll. Returns **True if
clean** (everything in the window now has rows) — only then is `sweep_from`
cleared. Never raises.

#### `_record_given_up_email(mail, listed, message)`
Writes the `FAILED` / `attachment_id="TRIAGE"` row for an email the bot has
stopped retrying, so a person can find and handle it.

#### `reprocess_order(order) → order`
Re-runs extraction from the file in S3 (e.g. after a prompt change). ⚠ Needs
`s3_key` — so it works for attachment orders only, **not** BODY orders.

### Section F — SAP CSV export

⚠ **Placeholder layout** — the 29 columns come from the "Required Fields"
sheet; confirm against the real SAP import spec before go-live.

| Name | Purpose |
|---|---|
| `RIGHT_EYE_SUFFIX = "1"`, `LEFT_EYE_SUFFIX = ""` | columns ending in `1` = right eye (convention — **confirm**; swap these two constants if wrong) |
| `SAP_CSV_COLUMNS` | 29 SAP columns + `GraphMailId` (30 total, `GraphMailId` last) |
| `EYE_FIELD_MAP` | CSV column stem → extraction key (`Sphere` → `sphere` …) |

#### `_leaf(node, *path) → str`
Safely reads `{"value": x}` at a path; `""` for missing/null.

#### `build_sap_rows(order) → list[dict]`
**One row per line item**, header fields repeated on each. Right eye → `…1`
columns, left eye → unsuffixed. A stock order with no eye
(`unspecified_eye`) is placed in the right-eye columns. OE code/UoM come from
the OE lookup (both eyes get the order-level code). An order with no line
items still produces one row. `GraphMailId = order.message_id`.

#### `export_order_to_csv(order) → key`
Builds the CSV (UTF-8 with BOM for Excel/SAP), uploads to
`S3_OUTPUT_BUCKET` at `exports/<date>/order-<id>-<PO>.csv`, sets
`exported_at`/`export_s3_key`, audit `EXPORTED`. Re-exporting overwrites.

### Section G — SQS execution queue

#### `sqs_client()`
boto3 SQS client; requires `SQS_QUEUE_URL`.

#### `enqueue_order(order) → message id`
Sends `{order_id, message_id, attachment_id, s3_key}` (tiny — the document is
in S3), sets `QUEUED`, audit `QUEUED`.

#### `_load_file_bytes(order) → bytes | None`
Worker side: reads the document from S3. `None` for BODY orders.

#### `process_queued_message(message) → bool (delete?)`
Handles one SQS message. Unparseable → keep (goes to DLQ). Missing order →
delete. Order already decided (not `NEW/QUEUED/PROCESSING/FAILED`) →
duplicate delivery, delete. Otherwise extract; success → delete; failure →
mark `FAILED` but **keep** the message so SQS retries (after
`maxReceiveCount`, AWS moves it to the DLQ). `FAILED` is deliberately
retryable.

#### `drain_sqs_queue() → {"received", "processed", "left_for_retry"}`
Long-polls the queue, up to `SQS_MAX_BATCHES` × `SQS_MAX_MESSAGES` per run.

---

## 7. The API — `views.py` and `urls.py`

All routes are under `/api/` (`opticbot/urls.py` → `optic_bot/urls.py`).
⚠ **No authentication in this service** — JWT is handled by the frontend;
keep this service on a private network. Full request/response examples are
in `API_REFERENCE.md`.

### Helpers

| Function | What it does |
|---|---|
| `handle_errors(view_func)` | decorator on every view: `ConfigError` → 400, `Http404` → 404, anything else → 500 JSON (never an HTML traceback) |
| `get_threshold(request)` | `CONFIDENCE_THRESHOLD`, or `?threshold=0.9` override |
| `order_to_dict(order, include_fields=True, threshold=None)` | serialises an Order; with fields it adds `fields`, `oe_candidates`, `body_text`, `low_confidence_fields`, `extraction_notes` |
| `_apply_field_edits(order, field_updates, actor)` | applies `{dotted.path: value}` edits, recomputes `min_confidence`, audit per field (does not save) |
| `_apply_oe_edit(order, oe_code, actor)` | reviewer picks an OE code → `oe_matched=True`, confidence 1.0, audit |

### Endpoints

| Method | Path | View | Notes |
|---|---|---|---|
| GET | `/api/health/` | `health` | DB, AWS (real STS call), Outlook/S3/LLM config, `oe_master` status, SQS, scheduler. Never 500s |
| GET | `/api/config/` | `config` | threshold, mailbox, both folder names, poll minutes, counts per status |
| GET | `/api/orders/` | `list_orders` | filters: `status`, `below_threshold=true`, `file_type`, `message_id`, `search`; `page`, `page_size` (≤100). ⚠ **Hides `NOT_AN_ORDER` and `FAILED` unless `?status=` is given** — use `?status=FAILED` to see failures and give-ups |
| GET | `/api/orders/<id>/` | `get_order` | full detail + presigned `source_url`, `export_url`, sibling attachments of the same email |
| PATCH | `/api/orders/<id>/fields/` | `edit_fields` | body `{"fields": {path: value}, "oe_code": "...", "reviewed_by": "..."}` |
| POST | `/api/orders/<id>/approve/` | `approve_order` | `reviewed_by` required; optional `fields`, `oe_code`, `comment` |
| POST | `/api/orders/<id>/reject/` | `reject_order` | `reviewed_by` and `reason` required |
| GET | `/api/orders/<id>/audit/` | `order_audit` | audit trail, oldest first |
| POST | `/api/orders/<id>/reprocess/` | `reprocess_order_view` | re-extract from S3 (attachment orders only) |
| POST | `/api/orders/<id>/export/` | `export_order_view` | only `APPROVED`/`AUTO_APPROVED`; writes the CSV |
| POST | `/api/poll/` | `trigger_poll` | run one poll now; `{"busy": true}` if one is already running |

---

## 8. Background jobs — `scheduler.py` and `apps.py`

#### `OpticBotConfig.ready()` (`apps.py`)
On Django start, if `RUN_SCHEDULER=True`, starts the scheduler — once (the
`RUN_MAIN` check avoids a double start under the dev autoreloader).

#### `scheduler.start()`
Creates an APScheduler `BackgroundScheduler` (UTC) with:
* `poll_outlook` → `run_poll()` every `MAIL_POLL_MINUTES`, runs once at boot,
  `max_instances=1`, `coalesce=True`;
* `drain_sqs` → `run_sqs_worker()` every `SQS_WORKER_MINUTES` (only if
  `USE_SQS`).

#### `run_poll()` / `run_sqs_worker()`
Thin wrappers that log results and never let an exception kill the scheduler
thread (`ConfigError` → warning).

**Multiple processes:** the poll is now protected by the DB lease, so several
gunicorn workers or servers each running the scheduler is safe (only one
polls). The SQS worker is safe to run anywhere (SQS gives each message to one
consumer).

---

## 9. Configuration — `settings.py` / `.env`

`settings.py` loads `.env` with python-dotenv. `env_bool()` treats
`true/1/yes` as True.

### Mailbox polling

| Setting | Default | Meaning |
|---|---|---|
| `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` | — | Azure app registration (needs **Mail.ReadWrite** application permission) |
| `MAILBOX_USER_EMAIL` | — | the shared mailbox |
| `MAIL_TARGET_FOLDER` | `OPTIC BOT` | folder under Inbox that is polled |
| `MAIL_ORDERS_FOLDER` | `01 New Orders` | sub-folder order emails are moved to |
| `MAIL_POLL_MINUTES` | 5 | scheduler interval |
| `MAIL_BATCH_SIZE` | 20 | max new emails handled per poll (and per sweep) |
| `MAIL_FIRST_RUN_DAYS` | 0 | first run only: days back to read (`0` = everything) |
| `MAIL_TEST_LIMIT` | 0 | stop after N emails **in total** (`0` = off). For go-live testing |
| `MAIL_OVERLAP_MINUTES` | 60 | re-read window behind the watermark |
| `MAIL_RETRY_WINDOW_HOURS` | 72 | give up on a failing email after this long; sweep depth |
| `MARK_MAIL_AS_READ` | True | cosmetic |
| `INCLUDE_BODY_AS_CONTEXT` | True | send the body to the extraction prompt |
| `MAX_BODY_CHARS` | 4000 | body truncation for DB/LLM (S3 has the full body) |
| `ALLOWED_EXTENSIONS` | pdf,docx,doc,xlsx,xls,csv,png,jpg,jpeg | attachments processed |
| `SKIP_INLINE_ATTACHMENTS` | True | ignore signature images |
| `LLM_MAX_DOCUMENT_MB` | 4.5 | above this an attachment is saved FAILED for manual entry |

### AWS / LLM / OE

| Setting | Default | Meaning |
|---|---|---|
| `AWS_REGION` | us-east-1 | |
| `AWS_ACCESS_KEY_ID/SECRET` | empty | optional; empty = instance role |
| `S3_INPUT_BUCKET`, `S3_OUTPUT_BUCKET` | — | documents / CSV exports |
| `LLM_PROVIDER` | bedrock | or `gateway` |
| `BEDROCK_MODEL_ID` | Claude model id | |
| `LLM_GATEWAY_URL`, `LLM_GATEWAY_API_KEY` | — | gateway mode only |
| `LLM_MAX_TOKENS` | 8192 | raise if big orders get truncated |
| `LLM_TIMEOUT` | 120 | seconds (gateway) |
| `EMAIL_CLASSIFICATION_ENABLED` | True | |
| `EMAIL_CLASSIFICATION_THRESHOLD` | 0.80 | confidence needed to skip as communication |
| `OE_MATCHING_ENABLED` | True | |
| `OE_EMBEDDING_MODEL_ID` / `_DIMENSION` | Titan v2 / 1024 | must match `oe_master` |
| `OE_TOP_K` | 5 | candidates |
| `OE_MATCH_THRESHOLD` | 0.82 | vector-only acceptance |
| `OE_RERANK_ENABLED` | True | LLM picks + explains |
| `OE_RERANK_THRESHOLD` | 0.75 | LLM confidence needed |

### Business rules, DB, scheduler, SQS

| Setting | Default | Meaning |
|---|---|---|
| `CONFIDENCE_THRESHOLD` | 0.85 | auto-approve threshold |
| `REQUIRE_OE_MATCH` | True | auto-approve also needs an OE match |
| `DB_ENGINE` | sqlite | `postgres` in production (`DB_NAME/USER/PASSWORD/HOST/PORT`) |
| `RUN_SCHEDULER` | False | start background jobs |
| `USE_SQS` | False | queued mode |
| `SQS_QUEUE_URL`, `SQS_DLQ_URL` | — | |
| `SQS_VISIBILITY_TIMEOUT` | 300 | must exceed one extraction |
| `SQS_WAIT_TIME_SECONDS` | 20 | long polling |
| `SQS_MAX_MESSAGES` / `SQS_MAX_BATCHES` | 10 / 5 | per worker run |
| `SQS_WORKER_MINUTES` | 1 | worker interval |

---

## 10. Prompts

Plain text files in `prompts/`, loaded with `load_prompt()`. Edit them
without a code deploy (restart not needed — read on every call). Placeholders
are `{name}`; literal braces are `{{ }}`.

| File | Used by | Returns |
|---|---|---|
| `email_classification.txt` | `classify_email` | `{"classification": "ORDER"|"COMMUNICATION", "confidence", "reason"}` |
| `order_extraction.txt` | `_extract_and_score` | `{"is_order", "order": {...}, "line_items": [{product_description, pack_size, right_eye, left_eye, unspecified_eye}], "extraction_notes"}` — every leaf `{"value","confidence"}` |
| `oe_rerank.txt` | `rerank_oe_candidates` | `{"match", "oe_code", "confidence", "reason"}` |

---

## 11. Tests

Run (the `.env` points at remote Postgres, so force SQLite):

```bash
DB_ENGINE=sqlite python manage.py test optic_bot
```

### `tests.py` — unit tests (18)

`PollMailboxTests` mocks the Graph helper functions and checks the poll
logic: move + ingest + watermark; first run reads everything then switches to
the watermark; day window passed to fetch and sweep; test limit stops and
resumes; overlap re-fetch never re-classifies; failed move pins and retries;
backlog drains without skipping; stuck email is given up after the window;
stuck clock resets on recovery; sweep recovers a failed ingest; sweep leaves
emails that already have rows.
`GraphContractTests`: immutable-id header, `GraphMailId` CSV column,
`fetch_floor`/`sweep_floor`, day window, watermark only moves forward, lazy
paging.

### `tests_e2e.py` — end to end (17)

Runs the **real** code from Graph HTTP calls to the CSV against fakes:
`FakeGraph` (HTTP-level mailbox with folders, `$filter`, `$orderby`,
`$select`, paging, attachments, move, immutable ids), `FakeS3`, `FakeSQS`,
and a fake LLM. Only MSAL and the pgvector OE lookup are stubbed.

| Test | Proves |
|---|---|
| 01 | full inline run: moves, S3 layout, rows, statuses, CSV (30 cols, `GraphMailId`, eyes, PO file name) |
| 02 | a second poll does nothing and costs nothing |
| 03 | an email a person already opened is still processed |
| 04 | SQS mode: queue then worker |
| 05 | test limit 5 on a 1,200-email folder: touches 5, one light page, resumes |
| 06 | `MAIL_FIRST_RUN_DAYS=1` never touches older mail |
| 07 | a 40-email backlog drains completely, each classified once |
| 08 | late-syncing mail caught by the overlap |
| 09 | failing email retried until it works |
| 10 | poison email given up after 72 h → visible `TRIAGE` row, never retried |
| 11 | failed ingest recovered even when the watermark jumps 50 days |
| 12 | email dragged into `01 New Orders` by hand is ingested |
| 13 | only one poll at a time; an expired lease is taken over |
| 13b | the lease is renewed during a long poll |
| 14 | lease released even when the poll errors |
| 15 | a changed id on move is detected and logged |
| 16 | multi-page listings while moving emails lose nothing |

**What the tests cannot prove** (needs one live run): that Graph accepts the
combined `Prefer` header, that ids really stay stable on move in your tenant,
IAM/Graph permissions, and the migrations on Postgres.

---

## 12. Setup scripts

`scripts/` is gitignored scaffolding, not app code. See `scripts/README.md`.

| Script | Does |
|---|---|
| `setup_postgres_ec2.sh` | installs Postgres on EC2, creates DB/user, prints `DB_*` lines (note: port may be 5433) |
| `setup_oe_master.py` → `build()` | creates `oe_master` (pgvector) and seeds a small catalog with real Titan embeddings |
| `setup_aws.py` → `ensure_bucket()`, `ensure_queue()`, `main()` | idempotently creates S3 buckets, SQS queue + DLQ (redrive, maxReceiveCount 3), checks Bedrock model access, prints `.env` lines |
| `ssm_run.py` → `run(script, timeout)` | runs a shell script on the EC2 instance via SSM, output to `ssm_last_output.txt` |
| `iam_policy.json` | IAM permissions the app needs |

---

## 13. Operations runbook

### First go-live (safe rollout)

1. `python manage.py migrate` (applies 0006 and 0007).
2. Make sure the Order table has no old test rows (old rows use non-immutable
   ids and also count toward the test limit).
3. In `.env`: `MAIL_FIRST_RUN_DAYS=1`, `MAIL_TEST_LIMIT=5`, `USE_SQS=False`.
4. `POST /api/poll/`, then check:
   * 5 emails handled; orders moved to `01 New Orders`;
   * the moved email's id is unchanged (no "Graph id changed on move" warning
     in the log);
   * S3 has `<id>/body.txt` and the attachments;
   * `GET /api/orders/` shows rows with extraction and OE results;
   * export one approved order and check `GraphMailId`.
5. Raise `MAIL_TEST_LIMIT` to 30, repeat. Then set it to `0` and choose
   `MAIL_FIRST_RUN_DAYS` for the real backlog **before** the first real run
   (it only applies while the watermark is empty — see "re-run" below).
6. Switch to `USE_SQS=True`, `RUN_SCHEDULER=True` for production.

### Common tasks

| Task | How |
|---|---|
| See what the poller is doing | log lines from `optic_bot.services`; `MailWatermark` row |
| Run a poll now | `POST /api/poll/` |
| See failures / give-ups | `GET /api/orders/?status=FAILED` (`attachment_id=TRIAGE` = given up) |
| Re-extract after a prompt change | `POST /api/orders/<id>/reprocess/` |
| Re-run the "first run" (e.g. backfill older mail) | set `MailWatermark.received_at = NULL` (and adjust `MAIL_FIRST_RUN_DAYS`). Already-handled emails are skipped, not reprocessed |
| Poll stuck "busy" after a crash | wait ≤ 30 min (lease expires) or set `locked_until = NULL` |
| Speed up a backlog | raise `MAIL_BATCH_SIZE` or call `POST /api/poll/` repeatedly |
| Orders stuck `QUEUED` | the SQS worker isn't running (`USE_SQS`/`RUN_SCHEDULER`) or lacks IAM `sqs:ReceiveMessage` |
| Everything `NEEDS_REVIEW` with no OE code | `oe_master` empty/not matching, or `OE_MATCHING_ENABLED=False` |

---

## 14. Known limitations and open items

1. **SAP CSV layout is a placeholder** — confirm column names/order and the
   right/left eye suffix convention with the SAP team.
2. **One OE code per order** (from the first line item), written to both eyes.
   Per-line / per-eye OE codes are a future item.
3. **No API authentication** — relies on network isolation + the frontend.
4. **`reprocess` doesn't work for body-only orders** (no file in S3).
5. **SQS enqueue failure** leaves the row `FAILED` but it is not re-queued
   automatically — reprocess it by hand.
6. **> 500 emails with the identical received second** would stop the scan
   (only plausible after a bulk import).
7. **Given-up emails** are hidden from the default order list — check
   `?status=FAILED` regularly.
8. **Live-only assumptions** (see section 11): combined `Prefer` header,
   immutable ids on move, permissions.
9. **The sweep ingests hand-dragged emails without classifying them** — the
   folder is treated as "a person decided this is an order".

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **Graph** | Microsoft Graph API — how the bot reads/moves mail |
| **Immutable id** | a Graph message id that does not change when the email is moved |
| **Watermark** | the received time of the newest email the poller has finished with |
| **Overlap** | re-reading 60 min behind the watermark to catch late mail |
| **Lease** | the DB lock that allows only one poll at a time |
| **Sweep** | the check of `01 New Orders` for emails with no Order rows |
| **Triage** | classify + move one email |
| **Ingest** | store body/attachments in S3 and create Order rows |
| **Extraction** | the LLM reading the order into structured fields |
| **OE code** | SAP product code for a lens product |
| **oe_master** | Postgres table of products with embedding vectors (pgvector) |
| **Rerank** | LLM choosing among the vector-search OE candidates, with a reason |
| **DLQ** | SQS dead-letter queue — messages that failed 3 times |
| **Fail open** | on doubt/error, treat as an order rather than drop it |
