"""
Mailbox polling: watermark, move-to-"01 New Orders", sweep, immutable ids.
Graph and the LLM are mocked; the database is the real (SQLite) test DB.

    python manage.py test optic_bot
"""

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from . import services as s
from .models import MailWatermark, Order


def wm():
    return s.get_watermark_row().received_at


def mail(mail_id, received):
    return {
        "id": mail_id, "subject": f"subject {mail_id}", "body": {"content": "hi", "contentType": "text"},
        "from": {"emailAddress": {"address": "practice@example.com"}},
        "receivedDateTime": received.strftime("%Y-%m-%dT%H:%M:%SZ"), "isRead": False,
    }


def fake_ingest(m, headers):
    """Stands in for process_order_email: leaves an Order row behind."""
    Order.objects.create(message_id=m["id"], attachment_id="BODY", status="NEEDS_REVIEW")
    return {"orders_created": 1}


class PollMailboxTests(TestCase):
    def setUp(self):
        self.t0 = timezone.now().replace(microsecond=0) - timedelta(hours=2)
        self.inbox = []          # what "OPTIC BOT" currently holds
        self.orders_folder = []  # ids in "01 New Orders"
        self.classify_calls = []
        self.verdicts = {}       # mail id -> is_order (default True)
        self.move_fails = set()

        def classify(m, names):
            self.classify_calls.append(m["id"])
            return self.verdicts.get(m["id"], True), 0.9, "reason"

        def move(headers, message_id, dest):
            if message_id in self.move_fails:
                raise RuntimeError("move failed")
            self.orders_folder.append(message_id)
            self.inbox = [x for x in self.inbox if x["id"] != message_id]
            return message_id  # immutable id: unchanged

        patches = {
            "get_graph_token": mock.patch.object(s, "get_graph_token", return_value="t"),
            "get_folder_id": mock.patch.object(s, "get_folder_id", return_value="OB"),
            "get_orders_folder_id": mock.patch.object(s, "get_orders_folder_id", return_value="NO"),
            "fetch": mock.patch.object(s, "fetch_emails_since", side_effect=lambda h, f, since: list(self.inbox)),
            "list_ids": mock.patch.object(s, "list_message_ids_since", side_effect=lambda h, f, since: list(self.orders_folder)),
            "get_message": mock.patch.object(s, "get_message", side_effect=lambda h, i: mail(i, self.t0)),
            "classify": mock.patch.object(s, "classify_email", side_effect=classify),
            "move": mock.patch.object(s, "move_email", side_effect=move),
            "list_attachments": mock.patch.object(s, "list_attachments", return_value=[]),
            "mark_read": mock.patch.object(s, "mark_email_read"),
        }
        self.m = {name: p.start() for name, p in patches.items()}
        self.addCleanup(mock.patch.stopall)
        self.ingest = mock.patch.object(s, "process_order_email", side_effect=fake_ingest).start()

    def test_order_email_is_moved_ingested_and_watermark_advances(self):
        self.inbox = [mail("A", self.t0)]
        out = s.poll_mailbox()

        self.assertEqual(out["emails_checked"], 1)
        self.assertEqual(out["moved_to_orders"], 1)
        self.assertEqual(self.orders_folder, ["A"])
        self.assertTrue(Order.objects.filter(message_id="A").exists())
        self.assertEqual(wm(), self.t0)

    def test_first_run_reads_everything_then_switches_to_the_watermark(self):
        ancient = self.t0 - timedelta(days=400)
        self.inbox = [mail("OLD", ancient)]
        s.poll_mailbox()

        self.assertEqual(self.orders_folder, ["OLD"])         # a 400-day-old email is still read
        self.assertIsNone(self.m["fetch"].call_args_list[0].args[2])   # no lower bound on poll 1
        self.assertIsNone(self.m["list_ids"].call_args_list[0].args[2])  # sweep also unbounded

        self.inbox = [mail("NEW", self.t0)]
        s.poll_mailbox()
        self.assertIsNotNone(self.m["fetch"].call_args_list[1].args[2])  # bounded from poll 2 on
        self.assertIsNotNone(self.m["list_ids"].call_args_list[1].args[2])

    @override_settings(MAIL_TEST_LIMIT=3)
    def test_test_limit_stops_after_n_emails_and_then_stops_fetching(self):
        self.inbox = [mail(f"M{i}", self.t0 + timedelta(minutes=i)) for i in range(10)]

        out = s.poll_mailbox()
        self.assertEqual(out["moved_to_orders"], 3)           # 10 waiting, only 3 allowed
        self.assertTrue(out["test_limit_reached"])
        self.assertEqual(len(self.inbox), 7)

        fetches = self.m["fetch"].call_count
        out = s.poll_mailbox()                                # limit hit: no mailbox call at all
        self.assertTrue(out["test_limit_reached"])
        self.assertEqual(self.m["fetch"].call_count, fetches)
        self.assertEqual(len(self.orders_folder), 3)

    @override_settings(MAIL_TEST_LIMIT=3)
    def test_raising_the_limit_resumes_where_the_test_stopped(self):
        self.inbox = [mail(f"M{i}", self.t0 + timedelta(minutes=i)) for i in range(5)]
        s.poll_mailbox()
        self.assertEqual(self.orders_folder, ["M0", "M1", "M2"])   # oldest first

        with override_settings(MAIL_TEST_LIMIT=0):
            s.poll_mailbox()
        self.assertEqual(self.orders_folder, ["M0", "M1", "M2", "M3", "M4"])
        self.assertEqual(Order.objects.count(), 5)                 # nothing handled twice

    @override_settings(MAIL_FIRST_RUN_DAYS=1)
    def test_poll_passes_the_day_window_to_the_fetch_and_the_sweep(self):
        s.poll_mailbox()
        for call in (self.m["fetch"].call_args_list[0], self.m["list_ids"].call_args_list[0]):
            self.assertAlmostEqual((timezone.now() - call.args[2]).total_seconds(), 86400, delta=5)

    def test_backlog_drains_across_polls_without_skipping_the_middle(self):
        # 30 emails, one a day, oldest 30 days ago; 10 handled per poll
        old = timezone.now().replace(microsecond=0) - timedelta(days=30)
        self.inbox = [mail(f"D{i:02d}", old + timedelta(days=i)) for i in range(30)]
        floors = []

        def fetch(h, f, since):
            floors.append(since)
            return [x for x in self.inbox if since is None or s.parse_datetime(x["receivedDateTime"]) >= since]

        self.m["fetch"].side_effect = fetch
        with override_settings(MAIL_BATCH_SIZE=10):
            for _ in range(3):
                s.poll_mailbox()

        self.assertEqual(len(self.orders_folder), 30)                      # every single one, none skipped
        self.assertEqual(self.orders_folder, [f"D{i:02d}" for i in range(30)])
        self.assertLess(floors[1], old + timedelta(days=10))               # poll 2 continued from the frontier...
        self.assertGreater(floors[1], old)                                 # ...not from the start, not from "now"

    @override_settings(MAIL_RETRY_WINDOW_HOURS=72)
    def test_stuck_email_is_retried_then_given_up_on_after_the_window(self):
        self.inbox = [mail("BAD", self.t0), mail("GOOD", self.t0 + timedelta(minutes=5))]
        self.move_fails = {"BAD"}

        s.poll_mailbox()
        row = s.get_watermark_row()
        self.assertIsNotNone(row.stuck_since)          # clock started
        self.assertIsNone(row.received_at)             # pinned before BAD

        s.poll_mailbox()                               # still within the window: pinned, retried
        self.assertIsNone(s.get_watermark_row().received_at)

        row.stuck_since = timezone.now() - timedelta(hours=73)   # ...73h of failing
        row.save()
        out = s.poll_mailbox()
        self.assertTrue(any("gave up on email BAD" in e for e in out["errors"]))
        row = s.get_watermark_row()
        self.assertEqual(row.received_at, self.t0)     # no longer pinned: the watermark moved past BAD
        self.assertIsNone(row.stuck_since)

    def test_recovery_clears_the_stuck_clock(self):
        self.inbox = [mail("A", self.t0)]
        self.move_fails = {"A"}
        s.poll_mailbox()
        self.assertIsNotNone(s.get_watermark_row().stuck_since)

        self.move_fails = set()
        s.poll_mailbox()
        self.assertIsNone(s.get_watermark_row().stuck_since)
        self.assertEqual(wm(), self.t0)

    def test_overlap_refetch_is_skipped_without_reclassifying(self):
        self.inbox = [mail("N", self.t0)]
        self.verdicts["N"] = False
        s.poll_mailbox()
        # a non-order stays in OPTIC BOT, so the next poll fetches it again
        s.poll_mailbox()
        s.poll_mailbox()

        self.assertEqual(self.classify_calls, ["N"])          # LLM ran exactly once
        self.assertEqual(Order.objects.filter(message_id="N").count(), 1)
        self.assertEqual(self.orders_folder, [])              # never moved

    def test_failed_move_pins_the_watermark_and_is_retried(self):
        a, b = mail("A", self.t0), mail("B", self.t0 + timedelta(minutes=5))
        self.inbox = [a, b]
        self.move_fails = {"A"}
        out = s.poll_mailbox()

        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["moved_to_orders"], 1)           # B still went through
        self.assertIsNone(wm())                  # A failed first: must not advance past it

        self.move_fails = set()
        s.poll_mailbox()
        self.assertEqual(self.orders_folder, ["B", "A"])
        # B already left OPTIC BOT, so A is the newest email the poll can still see
        self.assertEqual(wm(), self.t0)

    def test_sweep_ingests_email_left_without_rows_after_failed_ingest(self):
        self.inbox = [mail("A", self.t0)]
        calls = {"n": 0}

        def flaky(m, headers):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("S3 down")
            return fake_ingest(m, headers)

        self.ingest.side_effect = flaky
        out = s.poll_mailbox()
        self.assertEqual(self.orders_folder, ["A"])           # moved...
        self.assertFalse(Order.objects.filter(message_id="A").exists())  # ...but nothing ingested
        self.assertEqual(out["swept"], 0)                     # not retried in the same poll

        out = s.poll_mailbox()                                # next poll: sweep recovers it
        self.assertEqual(out["swept"], 1)
        self.assertTrue(Order.objects.filter(message_id="A").exists())

    def test_sweep_leaves_emails_that_already_have_rows(self):
        self.orders_folder = ["DONE", "MANUAL"]
        Order.objects.create(message_id="DONE", attachment_id="x", status="FAILED")
        out = s.poll_mailbox()

        self.assertEqual(out["swept"], 1)                     # only the hand-dragged one
        self.assertEqual([c.args[0]["id"] for c in self.ingest.call_args_list], ["MANUAL"])


class GraphContractTests(TestCase):
    def test_every_graph_call_asks_for_immutable_ids(self):
        prefer = s.build_headers("tok")["Prefer"]
        self.assertIn('IdType="ImmutableId"', prefer)
        self.assertIn('outlook.body-content-type="text"', prefer)

    def test_csv_carries_graph_mail_id_as_last_column(self):
        order = Order(message_id="AAMk-immutable", extracted_data={"order": {}, "line_items": []})
        rows = s.build_sap_rows(order)
        self.assertEqual(s.SAP_CSV_COLUMNS[-1], "GraphMailId")
        self.assertEqual(rows[0]["GraphMailId"], "AAMk-immutable")

    def test_fetch_floor(self):
        now = timezone.now()
        self.assertIsNone(s.fetch_floor(None))   # first run, default: whole folder, any age
        recent = now - timedelta(hours=3)
        self.assertEqual(s.fetch_floor(recent), recent - timedelta(minutes=60))

    def test_watermark_has_no_age_cap_so_a_backlog_can_drain(self):
        # after poll 1 of a year-old backlog the watermark is a year old; poll 2 must
        # continue from THERE, not jump to "now minus N hours" and skip the middle
        old = timezone.now() - timedelta(days=365)
        self.assertEqual(s.fetch_floor(old), old - timedelta(minutes=60))
        self.assertEqual(s.sweep_floor(old), old - timedelta(hours=72))

    def test_first_run_days_window(self):
        for days in (1, 7):
            with override_settings(MAIL_FIRST_RUN_DAYS=days):
                for floor in (s.fetch_floor(None), s.sweep_floor(None)):   # main fetch AND sweep
                    self.assertAlmostEqual((timezone.now() - floor).total_seconds(), days * 86400, delta=5)
        # a watermark takes over as soon as one exists, whatever MAIL_FIRST_RUN_DAYS says
        with override_settings(MAIL_FIRST_RUN_DAYS=1):
            old = timezone.now() - timedelta(days=30)
            self.assertEqual(s.fetch_floor(old), old - timedelta(minutes=60))

    def test_watermark_only_moves_forward(self):
        t = timezone.now()
        row = s.get_watermark_row()
        s.advance_watermark(row, t)
        s.advance_watermark(row, t - timedelta(hours=1))
        self.assertEqual(MailWatermark.objects.get().received_at, t)


class LocalStorageProviderTests(TestCase):
    """STORAGE_PROVIDER=local - the AWS-free path for store_document /
    load_document / document_url. Every test uses a throwaway temp
    directory, never the real local_storage/ folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(
            STORAGE_PROVIDER="local", LOCAL_STORAGE_DIR=self.tmp.name,
            S3_INPUT_BUCKET="in-bucket", S3_OUTPUT_BUCKET="out-bucket",
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_store_and_load_roundtrip_on_disk(self):
        key = s.store_document(b"hello world", "msg1/body.txt")
        self.assertEqual(key, "msg1/body.txt")

        on_disk = Path(self.tmp.name) / "in-bucket" / "msg1" / "body.txt"
        self.assertEqual(on_disk.read_bytes(), b"hello world")
        self.assertEqual(s.load_document("msg1/body.txt"), b"hello world")

    def test_same_key_layout_as_s3_mode(self):
        """The whole point: switching STORAGE_PROVIDER must not change any
        key a caller passes in - same (bucket, key) address either way."""
        s.store_document(b"x", "AAMk123=/att0_PO.pdf")
        expected = Path(self.tmp.name) / "in-bucket" / "AAMk123=" / "att0_PO.pdf"
        self.assertTrue(expected.is_file())

    def test_document_url_returns_the_local_path_not_a_url(self):
        s.store_document(b"csv,data", "exports/2026-01-01/order-1-PO.csv", bucket="out-bucket")
        result = s.document_url("exports/2026-01-01/order-1-PO.csv", bucket="out-bucket")
        expected = Path(self.tmp.name) / "out-bucket" / "exports" / "2026-01-01" / "order-1-PO.csv"
        self.assertEqual(result, str(expected))

    def test_path_traversal_is_rejected(self):
        for bad_key in ("../../etc/passwd", "a/../../b"):
            with self.assertRaises(ValueError):
                s.store_document(b"x", bad_key)
        # document_url fails the same way but returns None, like presigned_url does
        self.assertIsNone(s.document_url("../../etc/passwd"))

    def test_document_url_returns_none_for_a_missing_bucket_or_key(self):
        self.assertIsNone(s.document_url(""))
        with override_settings(S3_INPUT_BUCKET="", S3_OUTPUT_BUCKET=""):
            self.assertIsNone(s.document_url("some/key"))

    def test_directory_is_created_on_first_write(self):
        self.assertFalse((Path(self.tmp.name) / "in-bucket").exists())
        s.store_document(b"x", "a/b/c.txt")
        self.assertTrue((Path(self.tmp.name) / "in-bucket" / "a" / "b" / "c.txt").is_file())


class DefaultProviderIsS3Tests(TestCase):
    """Without STORAGE_PROVIDER set, nothing should touch local disk - the
    default must stay "s3", matching production before this feature existed."""

    def test_store_document_goes_through_s3_when_provider_unset(self):
        from django.conf import settings
        self.assertEqual(settings.STORAGE_PROVIDER, "s3")

        with mock.patch.object(s, "upload_to_s3") as upload:
            s.store_document(b"x", "k", bucket="b")
        upload.assert_called_once_with(b"x", "k", bucket="b")

    def test_load_document_goes_through_s3_client_when_provider_unset(self):
        fake_client = mock.Mock()
        fake_client.get_object.return_value = {"Body": mock.Mock(read=lambda: b"bytes")}
        with mock.patch.object(s, "s3_client", return_value=fake_client):
            result = s.load_document("k", bucket="b")
        self.assertEqual(result, b"bytes")
        fake_client.get_object.assert_called_once_with(Bucket="b", Key="k")

    def test_document_url_goes_through_presigned_url_when_provider_unset(self):
        with mock.patch.object(s, "presigned_url", return_value="https://signed.example/x") as presigned:
            result = s.document_url("k", bucket="b")
        self.assertEqual(result, "https://signed.example/x")
        presigned.assert_called_once_with("k", seconds=3600, bucket="b")

def leaf(value, confidence):
    return {"value": value, "confidence": confidence}


class CriticalConfidenceTests(TestCase):
    """The auto-approval gate. Built from the REAL orders in
    actualemail&actualmasteroedata/ - before this, min() over every field
    sent all four to review, every one blocked by metadata."""

    def eye_concepts(self):
        """Every field explicitly labelled in the email body. The cleanest
        order in the sample set - this one MUST auto-approve."""
        return {
            "order": {
                "account_number": leaf("6273615", 0.98),
                "customer_name": leaf("EYE CONCEPTS", 0.97),
                "patient_name": leaf("Marino, Ignazia", 0.96),
                "order_type": leaf("Trial Lens Order", 0.75),   # metadata, must not block
                "order_form_type": leaf("DX", 0.70),            # metadata, must not block
                "placed_by": leaf("Monica Tran", 0.60),         # metadata, must not block
                "trial_only": leaf(True, 0.93),
                "dtp_order": leaf(False, 0.9),
            },
            "line_items": [{
                "product_description": leaf("J&J 1 Day Oasys MAX Multifocal - 30pk", 0.96),
                "pack_size": leaf("30", 0.88),
                "right_eye": {"order_quantity": leaf("1", 0.97), "base_curve": leaf("8.4", 0.97),
                              "sphere": leaf("+3.00", 0.97), "add_power": leaf("MED", 0.9)},
                "left_eye": {"order_quantity": leaf("1", 0.97), "base_curve": leaf("8.4", 0.97),
                             "sphere": leaf("+2.75", 0.97), "add_power": leaf("MED", 0.9)},
            }],
        }

    def test_metadata_alone_never_blocks_a_good_order(self):
        fields = self.eye_concepts()
        self.assertEqual(s.score(fields), 0.60)            # old gate: placed_by sank it
        self.assertGreaterEqual(s.critical_confidence(fields), 0.85)
        self.assertEqual(s.extraction_gaps(fields), [])

    def test_a_bad_prescription_field_still_blocks(self):
        """Vision-X: "+6.00 & +6.50" with no eye labels - real ambiguity,
        and it must still go to a human."""
        fields = self.eye_concepts()
        fields["line_items"][0]["right_eye"]["sphere"] = leaf("+6.00", 0.60)
        self.assertLess(s.critical_confidence(fields), 0.85)

    def test_account_number_and_trial_only_are_critical(self):
        for path, where in (("account_number", "order"), ("trial_only", "order")):
            fields = self.eye_concepts()
            fields[where][path] = leaf(fields[where][path]["value"], 0.50)
            self.assertLess(s.critical_confidence(fields), 0.85, f"{path} should gate")

    def test_dtp_address_is_critical_only_for_dtp_orders(self):
        fields = self.eye_concepts()
        fields["order"]["dtp_address_line1"] = leaf("503/1 Brightwell Lane", 0.55)
        self.assertGreaterEqual(s.critical_confidence(fields), 0.85)  # not a DTP order

        fields["order"]["dtp_order"] = leaf(True, 0.96)
        self.assertLess(s.critical_confidence(fields), 0.85)          # ships to the patient

    def test_incomplete_extraction_cannot_sneak_through(self):
        """One high-scoring field must not look auto-approvable."""
        sparse = {
            "order": {"account_number": leaf("6222377", 0.99)},
            "line_items": [{"product_description": leaf("Acuvue Oasys", 0.99)}],
        }
        self.assertEqual(s.critical_confidence(sparse), 0.99)
        self.assertIn("no eye has both a power and a quantity", s.extraction_gaps(sparse))

    def test_each_completeness_gap_is_reported(self):
        self.assertIn("no account number", s.extraction_gaps({"line_items": []}))
        self.assertIn("no line items", s.extraction_gaps({"order": {}}))
        self.assertEqual(s.extraction_gaps(self.eye_concepts()), [])

    def test_unspecified_eye_stock_line_counts_as_dispensable(self):
        stock = {
            "order": {"account_number": leaf("6235210", 0.98)},
            "line_items": [{
                "product_description": leaf("1-Day Acuvue Oasys 90pk", 0.95),
                "unspecified_eye": {"sphere": leaf("-0.75", 0.95), "order_quantity": leaf("3", 0.95)},
            }],
        }
        self.assertEqual(s.extraction_gaps(stock), [])

    def test_gate_is_wired_into_the_status_decision(self):
        """End to end through _extract_and_score: metadata at 0.60 must
        still come out AUTO_APPROVED."""
        order = Order(message_id="m1", attachment_id="BODY")
        extraction = dict(self.eye_concepts(), is_order=True)
        with mock.patch.object(s, "call_llm", return_value="{}"), \
             mock.patch.object(s, "parse_llm_json", return_value=extraction), \
             mock.patch.object(s, "lookup_oe_code", return_value=("MZM", 0.96, [], True, "ok")), \
             mock.patch.object(s, "load_prompt", return_value="{sender}{subject}{body}"):
            s._extract_and_score(order, file_bytes=None)
        self.assertEqual(order.status, "AUTO_APPROVED")
        self.assertGreaterEqual(order.min_confidence, 0.85)

    def test_incomplete_order_is_held_even_with_an_oe_match(self):
        order = Order(message_id="m2", attachment_id="BODY")
        sparse = {"is_order": True,
                  "order": {"account_number": leaf("6222377", 0.99)},
                  "line_items": [{"product_description": leaf("Acuvue Oasys", 0.99)}]}
        with mock.patch.object(s, "call_llm", return_value="{}"), \
             mock.patch.object(s, "parse_llm_json", return_value=sparse), \
             mock.patch.object(s, "lookup_oe_code", return_value=("MX9", 0.97, [], True, "ok")), \
             mock.patch.object(s, "load_prompt", return_value="{sender}{subject}{body}"):
            s._extract_and_score(order, file_bytes=None)
        self.assertEqual(order.status, "NEEDS_REVIEW")
        self.assertIn("incomplete extraction", order.error_message)
