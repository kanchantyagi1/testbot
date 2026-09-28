"""
END-TO-END tests: the real pipeline code, from the HTTP calls to Microsoft
Graph down to the exported SAP CSV, run against in-memory fakes of the four
external systems:

    FakeGraph  - the shared mailbox, served at the HTTP level (requests.get /
                 post / patch are patched), with folders, paging, $filter,
                 $orderby, $select, attachments, move, and immutable ids
    FakeS3     - input + output buckets
    FakeSQS    - the execution queue
    fake LLM   - call_llm answers classification and extraction prompts

Only get_graph_token (MSAL) and lookup_oe_code (pgvector) are stubbed at the
function level. Everything else - list/move/download, S3 keys, Order rows,
extraction scoring, status decisions, watermark, sweep, lease, CSV - is the
production code.

    DB_ENGINE=sqlite python manage.py test optic_bot
"""

import csv
import io
import json
import re
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

from django.test import TestCase, override_settings
from django.utils import timezone

from . import services as s
from .models import MailWatermark, Order

MAILBOX = "bot@example.com"
OB, NO, INBOX = "folder-optic-bot", "folder-01-new-orders", "inbox"


# =============================================================================
# fakes
# =============================================================================

class FakeResponse:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} from fake Graph")


class FakeGraph:
    """Just enough of Microsoft Graph v1.0 mail to run the pipeline."""

    def __init__(self, immutable_ids=True):
        self.messages = {}          # id -> dict
        self.calls = []             # (method, path, params)
        self.prefer_violations = []
        self.immutable_ids = immutable_ids
        self.fail_move = set()      # ids whose move returns 500
        self.fail_get = set()       # ids whose GET returns 500
        self._n = 0

    # ---- building the mailbox ----
    def add(self, folder, received, subject, body="", sender="practice@example.com",
            attachments=(), is_read=False):
        self._n += 1
        mid = f"AAMkImmutable{self._n:05d}="
        self.messages[mid] = {
            "id": mid, "folder": folder, "subject": subject, "isRead": is_read,
            "receivedDateTime": received.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "from": {"emailAddress": {"address": sender}},
            "body": {"contentType": "text", "content": body},
            "attachments": [
                {"id": f"att{i}", "name": name, "bytes": data, "isInline": inline,
                 "@odata.type": "#microsoft.graph.fileAttachment"}
                for i, (name, data, inline) in enumerate(
                    a if len(a) == 3 else (a[0], a[1], False) for a in attachments
                )
            ],
        }
        return mid

    def in_folder(self, folder):
        return sorted(m["id"] for m in self.messages.values() if m["folder"] == folder)

    def count(self, method, pattern):
        return sum(1 for m, p, _ in self.calls if m == method and re.search(pattern, p))

    # ---- HTTP ----
    def _route(self, method, url, headers, params):
        prefer = (headers or {}).get("Prefer", "")
        if 'IdType="ImmutableId"' not in prefer or 'outlook.body-content-type="text"' not in prefer:
            self.prefer_violations.append((method, url))

        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        query.update({k: str(v) for k, v in (params or {}).items()})
        prefix = f"/v1.0/users/{MAILBOX}/"
        assert parsed.path.startswith(prefix), parsed.path
        path = parsed.path[len(prefix):]
        self.calls.append((method, path, query))
        return path, query

    def get(self, url, headers=None, params=None, timeout=None):
        path, q = self._route("GET", url, headers, params)

        if path == "mailFolders/inbox/childFolders":
            return FakeResponse(payload={"value": [
                {"id": "folder-other", "displayName": "Archive"},
                {"id": OB, "displayName": "Optic Bot"},            # case-insensitive match
            ]})
        if path == f"mailFolders/{OB}/childFolders":
            return FakeResponse(payload={"value": [{"id": NO, "displayName": "01 New Orders"}]})

        m = re.fullmatch(r"mailFolders/([^/]+)/messages", path)
        if m:
            return self._list(m.group(1), q, url)

        m = re.fullmatch(r"messages/([^/]+)/attachments/([^/]+)/\$value", path)
        if m:
            att = self._att(m.group(1), m.group(2))
            return FakeResponse(content=att["bytes"])

        m = re.fullmatch(r"messages/([^/]+)/attachments", path)
        if m:
            msg = self._msg(m.group(1))
            return FakeResponse(payload={"value": [
                {k: v for k, v in a.items() if k != "bytes"} | {"size": len(a["bytes"])}
                for a in msg["attachments"]
            ]})

        m = re.fullmatch(r"messages/([^/]+)", path)
        if m:
            if m.group(1) in self.fail_get:
                return FakeResponse(status=500)
            return FakeResponse(payload=self._view(self._msg(m.group(1)), q.get("$select")))

        raise AssertionError(f"unexpected GET {path}")

    def post(self, url, headers=None, json=None, timeout=None):
        path, _ = self._route("POST", url, headers, None)
        m = re.fullmatch(r"messages/([^/]+)/move", path)
        assert m, f"unexpected POST {path}"
        mid = m.group(1)
        if mid in self.fail_move:
            return FakeResponse(status=500)
        msg = self._msg(mid)
        msg["folder"] = json["destinationId"]
        if not self.immutable_ids:   # what Graph does WITHOUT the ImmutableId preference
            new_id = mid + "-moved"
            self.messages[new_id] = dict(msg, id=new_id)
            del self.messages[mid]
            return FakeResponse(status=201, payload={"id": new_id})
        return FakeResponse(status=201, payload={"id": mid})

    def patch(self, url, headers=None, json=None, timeout=None):
        path, _ = self._route("PATCH", url, headers, None)
        self._msg(path.split("/")[1]).update(json)
        return FakeResponse(payload={})

    # ---- helpers ----
    def _msg(self, mid):
        if mid not in self.messages:
            raise AssertionError(f"unknown message id {mid}")  # a stale (mutable) id would land here
        return self.messages[mid]

    def _att(self, mid, aid):
        return next(a for a in self._msg(mid)["attachments"] if a["id"] == aid)

    def _view(self, msg, select):
        fields = select.split(",") if select else list(msg)
        return {f: msg[f] for f in fields if f in msg}

    def _list(self, folder, q, url):
        assert "isRead" not in q.get("$filter", ""), "must never fetch by read state"
        items = [m for m in self.messages.values() if m["folder"] == folder]
        f = q.get("$filter")
        if f:
            since = re.fullmatch(r"receivedDateTime ge (\S+)", f).group(1)
            items = [m for m in items if m["receivedDateTime"] >= since]
        assert q.get("$orderby") == "receivedDateTime asc"
        items.sort(key=lambda m: (m["receivedDateTime"], m["id"]))
        top, skip = int(q.get("$top", 10)), int(q.get("$skip", 0))
        page = items[skip:skip + top]
        payload = {"value": [self._view(m, q.get("$select")) for m in page]}
        if skip + top < len(items):
            nq = {k: v for k, v in q.items()}
            nq["$skip"] = skip + top
            payload["@odata.nextLink"] = (
                f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/mailFolders/{folder}/messages?"
                + urlencode(nq)
            )
        return FakeResponse(payload=payload)


class FakeS3:
    def __init__(self):
        self.objects = {}           # (bucket, key) -> bytes
        self.fail_prefixes = set()  # keys starting with these raise on put

    def put_object(self, Bucket, Key, Body, **kwargs):
        if any(Key.startswith(p) for p in self.fail_prefixes):
            raise RuntimeError("S3 unavailable")
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def generate_presigned_url(self, *a, **kw):
        return "https://s3/presigned"

    def keys(self, bucket):
        return sorted(k for b, k in self.objects if b == bucket)


class FakeSQS:
    def __init__(self):
        self.visible, self.in_flight, self.n = [], {}, 0

    def send_message(self, QueueUrl, MessageBody):
        self.n += 1
        self.visible.append({"MessageId": f"m{self.n}", "Body": MessageBody,
                             "ReceiptHandle": f"r{self.n}", "Attributes": {"ApproximateReceiveCount": "1"}})
        return {"MessageId": f"m{self.n}"}

    def receive_message(self, QueueUrl, MaxNumberOfMessages, **kw):
        batch, self.visible = self.visible[:MaxNumberOfMessages], self.visible[MaxNumberOfMessages:]
        for m in batch:
            self.in_flight[m["ReceiptHandle"]] = m
        return {"Messages": batch}

    def delete_message(self, QueueUrl, ReceiptHandle):
        del self.in_flight[ReceiptHandle]


def leaf(v, c=0.95):
    return {"value": v, "confidence": c}


def fake_llm_factory(llm_log):
    """Classification: '[COMM]' in the subject -> COMMUNICATION.
    Extraction: an order whose PO number is taken from the subject."""

    def call_llm(prompt, file_bytes=None, file_name=None):
        if '"is_order"' not in prompt:  # the classification prompt
            llm_log.append(("classify", None))
            if "[COMM]" in prompt:
                return json.dumps({"classification": "COMMUNICATION", "confidence": 0.97,
                                   "reason": "delivery chaser"})
            return json.dumps({"classification": "ORDER", "confidence": 0.95, "reason": "has Rx"})

        llm_log.append(("extract", file_name))
        po = re.search(r"PO-(\d+)", prompt)
        return json.dumps({
            "is_order": True,
            "order": {"po_number": leaf(po.group(1) if po else None),
                      "account_number": leaf("100200"), "customer_name": leaf("Bailey Nelson")},
            "line_items": [{
                "product_description": leaf("Acuvue Oasys 1-Day 30pk"),
                "right_eye": {"sphere": leaf("-1.25"), "order_quantity": leaf("2")},
                "left_eye": {"sphere": leaf("-1.50"), "order_quantity": leaf("2")},
                "unspecified_eye": {},
            }],
            "extraction_notes": "",
        })

    return call_llm


BASE_SETTINGS = dict(
    MAILBOX_USER_EMAIL=MAILBOX, MAIL_TARGET_FOLDER="OPTIC BOT", MAIL_ORDERS_FOLDER="01 New Orders",
    S3_INPUT_BUCKET="in-bucket", S3_OUTPUT_BUCKET="out-bucket", SQS_QUEUE_URL="https://sqs/q",
    USE_SQS=False, EMAIL_CLASSIFICATION_ENABLED=True, EMAIL_CLASSIFICATION_THRESHOLD=0.8,
    CONFIDENCE_THRESHOLD=0.85, REQUIRE_OE_MATCH=True, OE_MATCHING_ENABLED=True,
    MAIL_BATCH_SIZE=20, MAIL_TEST_LIMIT=0, MAIL_FIRST_RUN_DAYS=0,
    MAIL_OVERLAP_MINUTES=60, MAIL_RETRY_WINDOW_HOURS=72,
    MARK_MAIL_AS_READ=True, INCLUDE_BODY_AS_CONTEXT=True, SKIP_INLINE_ATTACHMENTS=True,
    ALLOWED_EXTENSIONS=["pdf", "docx", "doc", "xlsx", "xls", "csv", "png", "jpg", "jpeg"],
)


@override_settings(**BASE_SETTINGS)
class EndToEnd(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.graph, self.s3, self.sqs, self.llm = FakeGraph(), FakeS3(), FakeSQS(), []
        for name, target in {
            "get": self.graph.get, "post": self.graph.post, "patch": self.graph.patch,
        }.items():
            mock.patch.object(s.requests, name, side_effect=target).start()
        mock.patch.object(s, "get_graph_token", return_value="token").start()
        mock.patch.object(s, "s3_client", return_value=self.s3).start()
        mock.patch.object(s, "sqs_client", return_value=self.sqs).start()
        mock.patch.object(s, "call_llm", side_effect=fake_llm_factory(self.llm)).start()
        mock.patch.object(s, "lookup_oe_code", return_value=(
            "OE-777", 0.96, [{"oe_code": "OE-777", "uom": "5P", "score": 0.96}], True, "exact match",
        )).start()
        self.addCleanup(mock.patch.stopall)

    def ago(self, **kw):
        return self.now - timedelta(**kw)

    def classify_calls(self):
        return sum(1 for kind, _ in self.llm if kind == "classify")

    # -------------------------------------------------------------------------
    def test_01_full_pipeline_inline(self):
        """Order with PDF + inline logo, a body-only order, and a delivery
        chaser: move, S3 layout, Order rows, statuses, and the SAP CSV."""
        g = self.graph
        pdf = g.add(OB, self.ago(hours=3), "PO-4471 order", body="Please supply as attached. Acct 100200",
                    attachments=[("PO_4471.pdf", b"%PDF-1.4 order"), ("logo.png", b"img", True)])
        body_only = g.add(OB, self.ago(hours=2), "Top up PO-5500", body="-0.75 x 3, -1.00 x 5")
        chaser = g.add(OB, self.ago(hours=1), "[COMM] where is my delivery?", body="Any update?")

        out = s.poll_mailbox()

        # mailbox: orders moved to 01 New Orders WITH THE SAME ID; chaser left alone
        self.assertEqual(g.in_folder(NO), sorted([pdf, body_only]))
        self.assertEqual(g.in_folder(OB), [chaser])
        self.assertEqual(g.prefer_violations, [])                 # every call: text body + immutable ids

        # S3: only 01 New Orders mail; body.txt + the real attachment, not the inline logo
        self.assertEqual(self.s3.keys("in-bucket"), sorted([
            f"{pdf}/body.txt", f"{pdf}/att0_PO_4471.pdf", f"{body_only}/body.txt",
        ]))
        self.assertEqual(self.s3.objects[("in-bucket", f"{body_only}/body.txt")], b"-0.75 x 3, -1.00 x 5")

        # DB: one row per attachment / body; chaser recorded but not extracted
        rows = {(o.message_id, o.attachment_id): o for o in Order.objects.all()}
        self.assertEqual(rows[(pdf, "att0")].status, "AUTO_APPROVED")
        self.assertEqual(rows[(pdf, "att0")].s3_key, f"{pdf}/att0_PO_4471.pdf")
        self.assertEqual(rows[(body_only, "BODY")].status, "AUTO_APPROVED")
        self.assertEqual(rows[(chaser, "EMAIL")].status, "NOT_AN_ORDER")
        self.assertEqual(len(rows), 3)

        self.assertEqual(out["emails_checked"], 3)
        self.assertEqual(out["moved_to_orders"], 2)
        self.assertEqual(out["not_orders"], 1)
        self.assertEqual(out["orders_created"], 2)
        self.assertEqual(out["errors"], [])
        self.assertEqual([k for k, _ in self.llm].count("extract"), 2)

        # CSV: GraphMailId is the id the email has in 01 New Orders (== OPTIC BOT id)
        order = rows[(pdf, "att0")]
        key = s.export_order_to_csv(order)
        self.assertRegex(key, r"^exports/\d{4}-\d{2}-\d{2}/order-\d+-4471\.csv$")   # real PO, not no-po
        text = self.s3.objects[("out-bucket", key)].decode("utf-8-sig")
        csv_rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(csv_rows), 1)
        self.assertEqual(list(csv_rows[0])[-1], "GraphMailId")
        self.assertEqual(len(csv_rows[0]), 30)
        self.assertEqual(csv_rows[0]["GraphMailId"], pdf)
        self.assertEqual(csv_rows[0]["PONumber"], "4471")
        self.assertEqual(csv_rows[0]["SAPOECode_Right"], "OE-777")
        self.assertEqual(csv_rows[0]["Sphere1"], "-1.25")         # right eye in the "1" columns
        self.assertEqual(csv_rows[0]["Sphere"], "-1.50")

    def test_02_second_poll_is_idempotent_and_cheap(self):
        g = self.graph
        g.add(OB, self.ago(minutes=30), "PO-1 order", attachments=[("po.pdf", b"x")])
        g.add(OB, self.ago(minutes=20), "[COMM] thanks")
        s.poll_mailbox()
        rows, puts, llm = Order.objects.count(), len(self.s3.objects), len(self.llm)
        full_gets = g.count("GET", r"^messages/[^/]+$")

        out = s.poll_mailbox()   # the chaser is still in OPTIC BOT, inside the overlap window

        self.assertEqual(Order.objects.count(), rows)
        self.assertEqual(len(self.s3.objects), puts)
        self.assertEqual(len(self.llm), llm)                         # no re-classification
        self.assertEqual(g.count("GET", r"^messages/[^/]+$"), full_gets)   # no full re-fetch
        self.assertEqual(out["emails_checked"], 0)

    def test_03_email_opened_by_a_person_is_still_processed(self):
        mid = self.graph.add(OB, self.ago(minutes=10), "PO-9 order", attachments=[("po.pdf", b"x")],
                             is_read=True)
        s.poll_mailbox()
        self.assertEqual(self.graph.in_folder(NO), [mid])
        self.assertTrue(Order.objects.filter(message_id=mid, status="AUTO_APPROVED").exists())

    @override_settings(USE_SQS=True)
    def test_04_sqs_mode_ingest_then_worker(self):
        g = self.graph
        a = g.add(OB, self.ago(minutes=10), "PO-10", attachments=[("a.pdf", b"a"), ("b.xlsx", b"b")])
        b = g.add(OB, self.ago(minutes=5), "Top up PO-11", body="-1.00 x 4")

        out = s.poll_mailbox()
        self.assertEqual(out["queued"], 3)                          # 2 attachments + 1 body order
        self.assertEqual(set(Order.objects.values_list("status", flat=True)), {"QUEUED"})
        self.assertEqual([k for k, _ in self.llm].count("extract"), 0)   # no extraction in the poller

        summary = s.drain_sqs_queue()
        self.assertEqual(summary["processed"], 3)
        self.assertEqual(set(Order.objects.values_list("status", flat=True)), {"AUTO_APPROVED"})
        self.assertEqual(self.sqs.in_flight, {})
        # the worker read the document back from S3 under the stable id
        self.assertIn(("extract", "a.pdf"), self.llm)
        self.assertEqual(g.in_folder(NO), sorted([a, b]))

    @override_settings(MAIL_TEST_LIMIT=5)
    def test_05_test_limit_against_a_big_folder(self):
        """1,200 emails waiting; a limit of 5 must touch only 5 of them and
        must not pull the whole folder down."""
        g = self.graph
        for i in range(1200):
            g.add(OB, self.ago(days=400) + timedelta(minutes=i), f"PO-{i} order", body="Rx")

        out = s.poll_mailbox()
        self.assertEqual(out["moved_to_orders"], 5)
        self.assertTrue(out["test_limit_reached"])
        self.assertEqual(self.classify_calls(), 5)
        self.assertEqual(g.count("GET", r"^messages/[^/]+$"), 5)            # 5 full fetches, not 1,200
        self.assertEqual(g.count("GET", rf"^mailFolders/{OB}/messages$"), 1)  # one light page
        self.assertEqual(len(g.in_folder(NO)), 5)

        calls = len(g.calls)
        out = s.poll_mailbox()                                             # limit reached: no Graph at all
        self.assertEqual(len(g.calls), calls)
        self.assertTrue(out["test_limit_reached"])

        with override_settings(MAIL_TEST_LIMIT=12):                        # raise it: continues, no repeats
            s.poll_mailbox()
        self.assertEqual(len(g.in_folder(NO)), 12)
        self.assertEqual(Order.objects.values("message_id").distinct().count(), 12)
        self.assertEqual(self.classify_calls(), 12)

    @override_settings(MAIL_FIRST_RUN_DAYS=1)
    def test_06_first_run_days_window(self):
        g = self.graph
        old = g.add(OB, self.ago(days=3), "PO-1 old order", body="Rx")
        new = g.add(OB, self.ago(hours=5), "PO-2 new order", body="Rx")
        s.poll_mailbox()
        self.assertEqual(g.in_folder(NO), [new])
        self.assertEqual(g.in_folder(OB), [old])                    # older than 1 day: never touched
        self.assertFalse(Order.objects.filter(message_id=old).exists())

        s.poll_mailbox()                                            # and later polls don't reach back either
        self.assertEqual(g.in_folder(OB), [old])

    @override_settings(MAIL_BATCH_SIZE=7)
    def test_07_backlog_drains_completely_across_polls(self):
        g = self.graph
        ids = [g.add(OB, self.ago(days=200) + timedelta(days=i * 3), f"PO-{i}", body="Rx") for i in range(40)]
        for _ in range(6):
            s.poll_mailbox()
        self.assertEqual(g.in_folder(NO), sorted(ids))              # all 40, nothing skipped
        self.assertEqual(self.classify_calls(), 40)                 # each classified exactly once
        self.assertEqual(g.in_folder(OB), [])

    def test_08_mail_arriving_between_polls(self):
        g = self.graph
        g.add(OB, self.ago(minutes=30), "PO-1", body="Rx")
        s.poll_mailbox()
        late = g.add(OB, self.ago(minutes=45), "PO-2 synced late", body="Rx")   # older than the watermark
        s.poll_mailbox()
        self.assertIn(late, g.in_folder(NO))                        # caught by the overlap window

    def test_09_failing_email_is_retried_then_surfaced_for_a_human(self):
        g = self.graph
        bad = g.add(OB, self.ago(hours=2), "PO-1", body="Rx")
        good = g.add(OB, self.ago(hours=1), "PO-2", body="Rx")
        g.fail_move = {bad}

        out = s.poll_mailbox()
        self.assertEqual(out["failed"], 1)
        self.assertIn(good, g.in_folder(NO))                        # the rest of the batch still flows
        self.assertEqual(g.in_folder(OB), [bad])

        g.fail_move = set()                                         # transient: next poll fixes it
        s.poll_mailbox()
        self.assertEqual(g.in_folder(NO), sorted([bad, good]))

    def test_10_poison_email_gives_up_after_window_and_leaves_a_failed_row(self):
        g = self.graph
        bad = g.add(OB, self.ago(hours=2), "PO-1", body="Rx")
        g.fail_get = {bad}
        s.poll_mailbox()
        row = MailWatermark.objects.get()
        row.stuck_since = timezone.now() - timedelta(hours=73)
        row.save()

        out = s.poll_mailbox()
        self.assertTrue(any("gave up" in e for e in out["errors"]))
        failed = Order.objects.get(message_id=bad)
        self.assertEqual((failed.attachment_id, failed.status), ("TRIAGE", "FAILED"))  # visible in the UI

        calls = len(self.llm)
        s.poll_mailbox()                                            # never retried again
        self.assertEqual(len(self.llm), calls)

    @override_settings(MAIL_BATCH_SIZE=1)
    def test_11_failed_ingest_recovered_even_when_watermark_jumps_far(self):
        """A moved email whose S3 upload fails must be retried by the sweep,
        even while a sparse backlog moves the watermark weeks per poll."""
        g = self.graph
        a = g.add(OB, self.ago(days=100), "PO-1", body="Rx")
        b = g.add(OB, self.ago(days=50), "PO-2", body="Rx")
        c = g.add(OB, self.ago(days=10), "PO-3", body="Rx")
        self.s3.fail_prefixes = {a}

        s.poll_mailbox()                    # a moved, ingest fails
        s.poll_mailbox()                    # b handled; sweep retries a, still failing
        self.assertFalse(Order.objects.filter(message_id=a).exists())
        self.assertIsNotNone(MailWatermark.objects.get().sweep_from)

        self.s3.fail_prefixes = set()
        s.poll_mailbox()                    # watermark is now 50 days past a - sweep must still reach it
        self.assertTrue(Order.objects.filter(message_id=a).exists())
        s.poll_mailbox()
        self.assertIsNone(MailWatermark.objects.get().sweep_from)   # cleared once everything is in
        self.assertEqual(g.in_folder(NO), sorted([a, b, c]))

    def test_12_sweep_picks_up_email_dragged_in_by_hand(self):
        s.poll_mailbox()                                            # first run
        manual = self.graph.add(NO, self.ago(minutes=5), "PO-77 forwarded", body="Rx")
        out = s.poll_mailbox()
        self.assertEqual(out["swept"], 1)
        self.assertTrue(Order.objects.filter(message_id=manual).exists())
        self.assertTrue(any(k.startswith(manual) for k in self.s3.keys("in-bucket")))

    def test_13_only_one_poll_at_a_time(self):
        self.graph.add(OB, self.ago(minutes=5), "PO-1", body="Rx")
        self.assertTrue(s.acquire_poll_lease())                     # someone else is polling

        out = s.poll_mailbox()
        self.assertTrue(out["busy"])
        self.assertEqual(self.graph.calls, [])                      # did nothing

        MailWatermark.objects.update(locked_until=timezone.now() - timedelta(minutes=1))  # holder died
        out = s.poll_mailbox()
        self.assertEqual(out["moved_to_orders"], 1)
        self.assertIsNone(MailWatermark.objects.get().locked_until)   # released afterwards

    def test_13b_lease_is_renewed_while_a_long_poll_works(self):
        """A slow poll must keep its lease alive, or a second poller could
        start halfway through it."""
        self.graph.add(OB, self.ago(minutes=5), "PO-1", body="Rx")
        seen = []
        real_move = s.move_email

        def slow_move(*a, **kw):
            # by now the poll has been running "40 minutes": the lease must
            # still be in the future and a rival must be refused
            seen.append(MailWatermark.objects.get().locked_until > timezone.now())
            seen.append(s.acquire_poll_lease())
            return real_move(*a, **kw)

        with mock.patch.object(s, "POLL_LEASE_MINUTES", 30), \
             mock.patch.object(s, "move_email", side_effect=slow_move):
            MailWatermark.objects.get_or_create(name=s.WATERMARK_NAME)
            s.poll_mailbox()
        self.assertEqual(seen, [True, False])

    def test_14_lease_released_even_when_poll_errors(self):
        with override_settings(MAIL_ORDERS_FOLDER="Does Not Exist"):
            with self.assertRaises(s.ConfigError):
                s.poll_mailbox()
        self.assertIsNone(MailWatermark.objects.get().locked_until)

    def test_15_without_immutable_ids_the_problem_is_detected(self):
        """Guards the assumption the design rests on: if Graph ever hands
        back a new id on move, it is logged loudly."""
        self.graph.immutable_ids = False
        self.graph.add(OB, self.ago(minutes=5), "PO-1", body="Rx")
        with self.assertLogs("optic_bot.services", level="WARNING") as logs:
            s.poll_mailbox()
        self.assertTrue(any("Graph id changed on move" in line for line in logs.output))

    def test_16_graph_paging_is_followed(self):
        g = self.graph
        with mock.patch.object(s, "SCAN_PAGE_SIZE", 3), override_settings(MAIL_BATCH_SIZE=50):
            ids = [g.add(OB, self.ago(hours=10) + timedelta(minutes=i), f"PO-{i}", body="Rx")
                   for i in range(8)]
            s.poll_mailbox()
        self.assertEqual(g.in_folder(NO), sorted(ids))
        self.assertEqual(g.count("GET", rf"^mailFolders/{OB}/messages$"), 3)   # 3 + 3 + 2
