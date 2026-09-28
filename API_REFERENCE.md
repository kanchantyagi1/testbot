# OPTIC BOT API Reference

Request/response payloads for every endpoint — human review, order data, CSV export.

**Base URL:** `{host}/api/`
**Auth:** none — this backend takes reviewer identity as plain data (`reviewed_by` in the request body). The frontend owns JWT/login entirely; keep this API reachable only from your app's own origin, not exposed directly to browsers.
**All responses are JSON.** Every endpoint returns a clean JSON error body on failure — never an HTML error page.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | [`/health/`](#get-health) | dependency status, never 500s |
| GET | [`/config/`](#get-config) | current threshold + status counts |
| GET | [`/orders/`](#get-orders) | list, filterable, paginated |
| GET | [`/orders/{id}/`](#get-ordersid) | full detail |
| PATCH | [`/orders/{id}/fields/`](#patch-ordersidfields) | human corrects a field |
| POST | [`/orders/{id}/approve/`](#post-ordersidapprove) | human approves |
| POST | [`/orders/{id}/reject/`](#post-ordersidreject) | human rejects |
| GET | [`/orders/{id}/audit/`](#get-ordersidaudit) | full history, oldest first |
| POST | [`/orders/{id}/reprocess/`](#post-ordersidreprocess) | re-run extraction |
| POST | [`/orders/{id}/export/`](#post-ordersidexport) | build the SAP CSV |
| POST | [`/poll/`](#post-poll) | check the mailbox right now |

Also see: [Human-in-the-loop: what's shown vs. what's submitted](#human-in-the-loop-whats-shown-vs-whats-submitted), [The `fields` object, in full](#the-fields-object-in-full) and [Error shape](#error-shape).

---

## Human-in-the-loop: what's shown vs. what's submitted

Two separate questions, answered separately: what does the reviewer **see** on the review screen, and what does the reviewer **send back**? The wording below matches exactly what the code checks — "Required" means the API returns a `400` naming that exact field if it's missing.

### What the reviewer sees — `GET /orders/{id}/`

Every field below comes from one API call. Nothing here is optional to *return* — the API always includes every key — but several are nullable, meaning the value itself can be empty/unset depending on where the order is in its lifecycle.

| Field | Always present? | Meaning |
|---|---|---|
| `id`, `status`, `created_at`, `updated_at` | always, never null | order identity + lifecycle |
| `sender`, `subject`, `received_at` | always (may be blank string) | the source email |
| `attachment_name`, `file_type`, `file_size` | always (blank/0 for a body-only order) | the source document, if any |
| `min_confidence` | always, `0.0` if nothing extracted | lowest confidence across every populated field - drives the review queue |
| `fields` | always present, `{}` before extraction | **the reviewable data** - see the `fields` object section below |
| `low_confidence_fields` | always present, `[]` if nothing is low | dotted paths to highlight in the UI |
| `extraction_notes` | always present, may be empty string | free-text flags from the LLM - ambiguities, assumptions it refused to make |
| `oe_code` | **nullable** (empty string until matched) | blank means no OE match yet - show the picker |
| `oe_confidence`, `oe_matched` | always present | `0.0`/`false` until matched |
| `oe_match_reason` | always present once extraction has run | **always has a value once the order is extracted, even when no match was found** - always safe to show as "why" text |
| `oe_candidates` | always present, `[]` if none | the pick-list for the OE code selector - show these when `oe_code` is blank |
| `oe_uom` | **nullable** (empty string until matched) | pack-size unit that came with the matched OE code |
| `reviewed_by`, `reviewed_at`, `review_comment` | **nullable/blank** until a human acts | blank = nobody has approved/rejected this yet |
| `error_message` | **nullable**, only set on `status: "FAILED"` | show as an error banner when non-empty |
| `source_url` | **nullable** | presigned link to the original document - `null` if S3 isn't configured, or this is a body-only order (nothing to link to) |
| `export_url` | **nullable** | presigned link to the exported CSV - `null` until exported |
| `sibling_attachments` | always present, `[]` if none | other order rows from the same email |

### What the reviewer submits

There is **no single "fields" column** in a request - `"fields"` is a JSON *object* (a map), not a value. Its keys are dotted paths (`"order.account_number"`, `"line_items.0.right_eye.sphere"`) and its values are the corrected value for that path. Send only the paths that actually changed; anything you don't include is left untouched.

| Endpoint | Field | Required? | Format | Notes |
|---|---|---|---|---|
| `PATCH /fields/` | `fields` | **Optional*** | object: `{"dotted.path": "new value", ...}` | *at least one of `fields`/`oe_code` is required |
| | `oe_code` | **Optional*** | string | picks/overrides the OE match |
| | `reviewed_by` | Optional | string (email/name) | who made the edit - shown in the audit trail; omitted edits are logged as `"unknown"` |
| `POST /approve/` | `reviewed_by` | **Required** | string | 400 `"reviewed_by is required"` if missing |
| | `comment` | Optional | string | free-text note, shown in the audit trail |
| | `fields` | Optional | same shape as above | last-minute correction applied before approving, in the same call |
| | `oe_code` | Optional | string | same as above |
| `POST /reject/` | `reviewed_by` | **Required** | string | 400 if missing |
| | `reason` | **Required** | string | 400 `"reason is required"` if missing - this is mandatory on reject, unlike `comment` on approve |
| `POST /reprocess/` | *(none)* | — | — | no request body at all |
| `POST /export/` | *(none)* | — | — | no request body at all |
| `POST /poll/` | *(none)* | — | — | no request body at all |

**In plain terms for the UI:**
- The "Approve" button only needs to know who's clicking it (`reviewed_by`) - everything else is optional.
- The "Reject" button needs who's clicking it **and** a reason - don't let it submit without both, the API will 400 either one missing.
- The field-correction form can submit as many or as few dotted-path corrections as the reviewer actually changed - there's no fixed list of "the fields," it's just an object of whatever changed.
- Picking an OE code from the candidate list (or typing one in) is submitted the exact same way in all three of `PATCH /fields/`, `POST /approve/`, and `POST /reject/` — as the `oe_code` key.

---

## GET /health/

Poll this on app load / a status page. Every dependency is checked independently — one being unconfigured never breaks the others.

**200 Response**
```json
{
  "database": "ok",
  "aws_credentials": "ok (account 239884529750)",
  "outlook": "ok",
  "s3": "ok",
  "llm": "ok",
  "oe_master": "ok (312 active row(s))",
  "sqs": "ok",
  "confidence_threshold": 0.85,
  "scheduler": "enabled"
}
```

Any dependency that isn't configured reports `"not_configured: KEY_NAME, OTHER_KEY"` — naming exactly which `.env` keys are missing — instead of a vague failure.

---

## GET /config/

Good for a dashboard header — how many orders are in each state right now.

**200 Response**
```json
{
  "confidence_threshold": 0.85,
  "mailbox": "ra-rpatsaspacteam@ITS.JNJ.com",
  "mail_target_folder": "OPTIC BOT",
  "mail_poll_minutes": 5,
  "require_oe_match": true,
  "status_counts": {
    "NEW": 0,
    "QUEUED": 2,
    "PROCESSING": 0,
    "NOT_AN_ORDER": 4,
    "NEEDS_REVIEW": 11,
    "AUTO_APPROVED": 6,
    "APPROVED": 38,
    "REJECTED": 3,
    "FAILED": 1
  }
}
```

---

## GET /orders/

Summary rows only — no `fields`/`body_text`/`oe_candidates` (use the detail endpoint for those). By default hides `NOT_AN_ORDER` and `FAILED` rows unless you explicitly filter for them.

**Query parameters**

| Param | Type | Notes |
|---|---|---|
| `status` | string | `NEW` / `QUEUED` / `PROCESSING` / `NOT_AN_ORDER` / `NEEDS_REVIEW` / `AUTO_APPROVED` / `APPROVED` / `REJECTED` / `FAILED` |
| `below_threshold` | bool | `true` → only rows where `min_confidence < threshold` |
| `threshold` | float | overrides `confidence_threshold` for this request only, e.g. `?threshold=0.9` |
| `file_type` | string | `pdf` / `xlsx` / `csv` / `docx` / `body` … |
| `message_id` | string | all attachments/rows from one email |
| `search` | string | matches subject / sender / attachment name / OE code |
| `page` | int | default 1 |
| `page_size` | int | default 20, max 100 |

**Example**
```
GET /api/orders/?below_threshold=true&page=1&page_size=20
```

**200 Response**
```json
{
  "count": 11,
  "page": 1,
  "page_size": 20,
  "results": [
    {
      "id": 42,
      "message_id": "AAMkAD...",
      "sender": "info@lighthouseoptom.com.au",
      "subject": "[EXTERNAL] URGENT Contact Lens Order No. 2476",
      "received_at": "2026-09-25T05:17:19Z",
      "attachment_id": "att-1",
      "attachment_name": "Contact Lens Order_2476.pdf",
      "file_type": "pdf",
      "file_size": 13670,
      "status": "NEEDS_REVIEW",
      "min_confidence": 0.75,
      "oe_code": "",
      "oe_confidence": 0.0,
      "oe_matched": false,
      "oe_match_reason": "top vector score 0.48 below OE_MATCH_THRESHOLD 0.82",
      "oe_uom": "",
      "reviewed_by": "",
      "reviewed_at": null,
      "review_comment": "",
      "error_message": "",
      "exported_at": null,
      "created_at": "2026-09-25T05:17:45Z",
      "updated_at": "2026-09-25T05:17:52Z"
    }
  ]
}
```

---

## GET /orders/{id}/

Everything the list has, plus the full extracted fields, the document link, and siblings from the same email.

Accepts `?threshold=0.9` the same way the list does — it changes which paths appear in `low_confidence_fields`.

**200 Response**
```json
{
  "id": 42,
  "message_id": "AAMkAD...",
  "sender": "info@lighthouseoptom.com.au",
  "subject": "[EXTERNAL] URGENT Contact Lens Order No. 2476",
  "status": "NEEDS_REVIEW",
  "min_confidence": 0.75,
  "oe_code": "",
  "oe_confidence": 0.0,
  "oe_matched": false,
  "oe_match_reason": "top vector score 0.48 below OE_MATCH_THRESHOLD 0.82",
  "oe_uom": "",
  "reviewed_by": "",
  "reviewed_at": null,
  "review_comment": "",
  "error_message": "",
  "exported_at": null,
  "created_at": "2026-09-25T05:17:45Z",
  "updated_at": "2026-09-25T05:17:52Z",

  "fields": { "...": "see 'The fields object' below" },
  "oe_candidates": [
    {"oe_code": "OM3", "name": "OASYS MAX 1-DAY STANDARD SPHERICAL",
     "uom": "90P", "score": 0.48, "match_type": "pgvector-cosine"}
  ],
  "body_text": "PLEASE SUPPLY ASAP, required urgently\n\nLIGHTHOUSE OPTOMETRISTS...",
  "low_confidence_fields": [
    "order.po_date",
    "line_items.0.right_eye.sphere"
  ],
  "extraction_notes": "Order quantity for left eye is explicitly 0 on the printed form - this is a real instruction, not a missing value.",

  "source_url": "https://opticbot-input.s3.amazonaws.com/...?X-Amz-Signature=...",
  "export_url": null,
  "sibling_attachments": [
    {"id": 43, "attachment_name": "image001.png", "file_type": "png", "status": "NOT_AN_ORDER"}
  ]
}
```

| Field | Notes |
|---|---|
| `low_confidence_fields` | dotted paths into `fields` that scored below threshold — highlight these in the UI. Same paths `PATCH /fields/` accepts. |
| `source_url` | presigned S3 link to the original document, 1 hr expiry. `null` if S3 isn't configured or this order has no attachment (body-only order). |
| `export_url` | presigned link to the exported SAP CSV. `null` until the order has been exported. |
| `sibling_attachments` | other order rows that came from the *same email* (an email can have multiple order attachments) — show these as tabs/links. |

---

## PATCH /orders/{id}/fields/

**Keys are dotted paths** into the nested `fields` object — exactly what `low_confidence_fields` returns. This is the one thing to get right in the frontend: don't flatten the field names, pass the path straight through.

**Request body**
```json
{
  "fields": {
    "order.account_number": "6346628",
    "line_items.0.right_eye.sphere": "-4.50",
    "line_items.0.right_eye.base_curve": "8.60"
  },
  "oe_code": "OM3",
  "reviewed_by": "jane@yourcompany.com"
}
```

`fields` and `oe_code` are both optional but at least one is required. Editing `oe_code` here sets `oe_matched=true` and `oe_confidence=1.0` — it's how a reviewer picks a different candidate from `oe_candidates`, or types one in when nothing matched.

**200 Response**
The full updated order (same shape as `GET /orders/{id}/`). Every edited leaf now has `"confidence": 1.0, "edited": true`, and `min_confidence` is recomputed.

**400 Response**
```json
{"error": "fields or oe_code is required"}
```

Every field edit is written to the audit trail with the old and new value — nothing to build separately for that.

---

## POST /orders/{id}/approve/

Can include last-minute field/OE edits in the same call (applied before approval) instead of a separate PATCH first.

**Request body**
```json
{
  "reviewed_by": "jane@yourcompany.com",
  "comment": "verified against the PDF",
  "fields": { "order.po_date": "2026-01-17" },
  "oe_code": "OM3"
}
```
Only `reviewed_by` is required.

**200 Response** — Full updated order, `status: "APPROVED"`.

**400 Responses**
```json
{"error": "reviewed_by is required"}
{"error": "Order already APPROVED"}
```

---

## POST /orders/{id}/reject/

**Request body**
```json
{
  "reviewed_by": "jane@yourcompany.com",
  "reason": "duplicate of order #38, already processed"
}
```
Both fields required — `reason` is mandatory here (optional as `comment` on approve).

**200 Response** — Full updated order, `status: "REJECTED"`.

**400 Responses**
```json
{"error": "reviewed_by is required"}
{"error": "reason is required"}
{"error": "Order already REJECTED"}
```

---

## GET /orders/{id}/audit/

**200 Response**
```json
[
  {
    "id": 101,
    "action": "EMAIL_RECEIVED",
    "actor": "system",
    "field_name": "",
    "old_value": "",
    "new_value": "",
    "note": "from info@lighthouseoptom.com.au, subject: ...",
    "created_at": "2026-09-25T05:17:45Z"
  },
  {
    "id": 102,
    "action": "EXTRACTED",
    "actor": "system",
    "field_name": "",
    "old_value": "",
    "new_value": "",
    "note": "min_confidence=0.75 oe_matched=false oe_reason=top vector score 0.48 below threshold",
    "created_at": "2026-09-25T05:17:52Z"
  },
  {
    "id": 103,
    "action": "FIELD_EDITED",
    "actor": "jane@yourcompany.com",
    "field_name": "line_items.0.right_eye.sphere",
    "old_value": "-4.25",
    "new_value": "-4.50",
    "note": "",
    "created_at": "2026-09-25T05:19:10Z"
  },
  {
    "id": 104,
    "action": "APPROVED",
    "actor": "jane@yourcompany.com",
    "field_name": "",
    "old_value": "",
    "new_value": "",
    "note": "verified against the PDF",
    "created_at": "2026-09-25T05:19:22Z"
  }
]
```

| action | Meaning |
|---|---|
| `EMAIL_RECEIVED` | row created |
| `EXTRACTED` | LLM extraction + OE match finished |
| `FIELD_EDITED` | one per changed field/OE code |
| `AUTO_APPROVED` | passed thresholds with no human |
| `APPROVED` / `REJECTED` | human decision |
| `QUEUED` | sent to SQS (only when `USE_SQS=True`) |
| `EXPORTED` | CSV written to S3 |
| `FAILED` | see `note` for the error |

---

## POST /orders/{id}/reprocess/

No request body. Re-downloads the stored document from S3 and re-runs extraction + OE matching — use after a prompt change, or if the first pass looks wrong.

**200 Response** — Full updated order.

**400 Response**
```json
{"error": "Cannot reprocess: no s3_key stored for this order"}
```

Only works for attachment-based orders that were stored in S3. A body-only order (typed directly in the email) has nothing to re-download.

---

## POST /orders/{id}/export/

No request body. Only valid on `APPROVED` / `AUTO_APPROVED` orders. Safe to call again later (e.g. after a field correction) — it just overwrites the same S3 object and updates `exported_at`.

**200 Response** — Full updated order — check `export_url` for the presigned CSV link.

**400 Response**
```json
{"error": "Order must be APPROVED or AUTO_APPROVED to export (current status: NEEDS_REVIEW)"}
```

---

## POST /poll/

No request body. The scheduler already does this automatically every 5 minutes — this is for a manual "check now" button so a reviewer doesn't have to wait.

Each poll fetches `OPTIC BOT` mail by **date** (everything received since the last watermark, minus a 60-minute overlap) — not by unread state, so opening an email never hides it from the bot. Every email not yet seen is classified; order emails are **moved** to the `01 New Orders` child folder (`moved_to_orders`) and ingested straight away from there — body text + attachments to S3, one Order row per attachment. Non-orders stay in `OPTIC BOT` with a `NOT_AN_ORDER` row. A failed move/classification is retried on the next poll.

The **first poll ever** (no watermark yet) reads `OPTIC BOT` back `MAIL_FIRST_RUN_DAYS` days — `1` = the last day, `7` = the last week, `0` (default) = every email whatever its age. Mail older than the window is never touched. Emails are handled oldest first, `MAIL_BATCH_SIZE` (20) new ones per poll — call this endpoint repeatedly to drain a large backlog faster. The watermark has no age cap, so a backlog drains across polls without skipping anything; from then on only mail since the watermark is fetched.

An email that keeps failing holds the watermark back and is retried every poll; after `MAIL_RETRY_WINDOW_HOURS` (72) of continuous failure it is given up on — listed in `errors` and recorded as a `FAILED` order with `attachment_id: "TRIAGE"` so a reviewer sees it — and the poll moves past it.

Only one poll runs at a time (across the scheduler, this endpoint and every server). If one is already running this returns `{"busy": true, "errors": []}` immediately.

If `MAIL_TEST_LIMIT` is set (e.g. `30`), the bot handles at most that many emails **in total** and then stops fetching mail; the response then carries `"test_limit": 30, "test_limit_reached": true`. Raise or unset it to continue from where it stopped.

A second step then sweeps `01 New Orders` (the 72h behind the watermark; the first-run window on the first poll) for emails that have **no Order rows** — an ingest that failed right after the move, or an email dragged in by hand — and ingests them (`swept`).

Graph calls use immutable ids, so an email keeps the **same id** after the move. `message_id` on an order is that id, and it is the `GraphMailId` column (last column) of the exported CSV.

`emails_checked` = emails newly classified this poll (ones already handled and re-fetched inside the overlap are not counted). The other counts are per attachment.

**200 Response**
```json
{
  "emails_checked": 3,
  "moved_to_orders": 2,
  "swept": 0,
  "attachments_found": 2,
  "orders_created": 0,
  "queued": 2,
  "skipped": 0,
  "not_orders": 1,
  "failed": 0,
  "errors": []
}
```

**400 Response**
```json
{"error": "Missing MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID in .env"}
```

---

## The `fields` object, in full

This is what `GET /orders/{id}/` returns in `fields`, and what the dotted paths in `low_confidence_fields` / `PATCH /fields/` point into. **Every leaf value is wrapped** as `{"value": ..., "confidence": 0.0-1.0}` — never a bare value.

### `order` (header fields)
```json
"order": {
  "account_number":       {"value": "6279505", "confidence": 0.98},
  "customer_name":        {"value": "BAILEYNELSON", "confidence": 0.95},
  "customer_address":     {"value": "NEWTOWN NSW 2042", "confidence": 0.9},
  "po_number":            {"value": "344114", "confidence": 0.97},
  "po_date":              {"value": "2026-09-22", "confidence": 0.8},
  "order_type":           {"value": "DX Order", "confidence": 0.7},
  "order_form_type":      {"value": "DX", "confidence": 0.7},
  "patient_name":         {"value": "Layal El-Khatib", "confidence": 0.93},
  "placed_by":            {"value": "Margaret", "confidence": 0.6},
  "trial_only":           {"value": false, "confidence": 0.9},
  "special_instructions": {"value": "pick it up over counter", "confidence": 0.9},
  "dtp_order":            {"value": false, "confidence": 0.9},
  "dtp_patient_name":     {"value": null, "confidence": 0.0},
  "dtp_address_line1":    {"value": null, "confidence": 0.0},
  "dtp_address_line2":    {"value": null, "confidence": 0.0},
  "dtp_city":             {"value": null, "confidence": 0.0},
  "dtp_state":            {"value": null, "confidence": 0.0},
  "dtp_zip":              {"value": null, "confidence": 0.0}
}
```

### `line_items` (array)

Each item has a product description, pack size, and **three** eye objects — `right_eye`, `left_eye`, `unspecified_eye` (used when the order doesn't specify an eye, e.g. a stock top-up order). All three always exist per item, with nulls where they don't apply.

```json
"line_items": [
  {
    "product_description": {"value": "Daily 1 Day Oasys 90pk", "confidence": 0.9},
    "pack_size":           {"value": "90", "confidence": 0.85},
    "right_eye": {
      "order_quantity": {"value": "1", "confidence": 0.95},
      "base_curve":     {"value": "8.50", "confidence": 0.95},
      "sphere":         {"value": "-4.50", "confidence": 0.9},
      "cylinder":       {"value": null, "confidence": 0.0},
      "axis":           {"value": null, "confidence": 0.0},
      "add_power":      {"value": null, "confidence": 0.0},
      "diameter":       {"value": null, "confidence": 0.0},
      "colour":         {"value": null, "confidence": 0.0}
    },
    "left_eye": {
      "order_quantity": {"value": "0", "confidence": 0.95},
      "base_curve":     {"value": "8.50", "confidence": 0.95},
      "sphere":         {"value": "-4.50", "confidence": 0.9},
      "cylinder": {"value": null, "confidence": 0.0},
      "axis": {"value": null, "confidence": 0.0},
      "add_power": {"value": null, "confidence": 0.0},
      "diameter": {"value": null, "confidence": 0.0},
      "colour": {"value": null, "confidence": 0.0}
    },
    "unspecified_eye": {
      "order_quantity": {"value": null, "confidence": 0.0},
      "base_curve": {"value": null, "confidence": 0.0},
      "sphere": {"value": null, "confidence": 0.0},
      "cylinder": {"value": null, "confidence": 0.0},
      "axis": {"value": null, "confidence": 0.0},
      "add_power": {"value": null, "confidence": 0.0},
      "diameter": {"value": null, "confidence": 0.0},
      "colour": {"value": null, "confidence": 0.0}
    }
  }
]
```

> **`order_quantity` can legitimately be `"0"`** — some printed order forms give a full prescription for both eyes but only order one of them. Render it, don't hide it or assume 1.

### Dotted path examples

| Path | Points to |
|---|---|
| `order.account_number` | `fields.order.account_number` |
| `line_items.0.product_description` | first line item's product name |
| `line_items.0.right_eye.sphere` | first item's right-eye sphere |
| `line_items.1.left_eye.base_curve` | second item's left-eye base curve |

---

## Error shape

Every non-2xx response, from every endpoint, is exactly this shape. Never an HTML page, never a stack trace.

```json
{ "error": "human-readable message" }
```

| Status | When |
|---|---|
| 400 | bad/missing request data, or a required `.env` value is missing (names exactly which keys) |
| 404 | order id doesn't exist — DRF's own `{"detail": "..."}` shape, not the `{"error": ...}` shape above |
| 500 | unexpected server error — still JSON, message may be less specific |

---

*All timestamps ISO 8601 UTC. No auth on this API — the frontend owns JWT.*
