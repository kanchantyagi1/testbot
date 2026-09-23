"""
OPTIC BOT data model - exactly two tables.

One Order row = one email ATTACHMENT (not one email - an email can carry
several order files: PDF, Word, Excel...). Dedupe key is
(message_id, attachment_id), enforced by the unique_together below, so
re-reading the same email on the next poll never creates duplicate rows -
and if attachment 2 of 3 fails, attachments 1 and 3 stay saved and only
attachment 2 is retried next time.

STATUS values and what they mean:
    NEW           - row just created, nothing processed yet
    PROCESSING    - sent to the LLM, waiting on the result
    NOT_AN_ORDER  - the file was a T&C / price list / logo, not a PO
                    (kept for audit, never shown to a reviewer)
    NEEDS_REVIEW  - extracted, but below the confidence threshold or the
                    OE code did not match
    AUTO_APPROVED - extracted, every field met the threshold, OE matched
    APPROVED      - a human reviewed and approved it
    REJECTED      - a human reviewed and rejected it
    FAILED        - something threw an exception; see error_message

Export to SAP CSV is NOT a status - an APPROVED/AUTO_APPROVED order can be
exported any number of times (e.g. re-run after a field correction). See
exported_at / export_s3_key below and services.export_order_to_csv().
"""

from django.db import models


class Order(models.Model):
    STATUS_CHOICES = [
        ("NEW", "New"),
        ("PROCESSING", "Processing"),
        ("NOT_AN_ORDER", "Not an order"),
        ("NEEDS_REVIEW", "Needs review"),
        ("AUTO_APPROVED", "Auto approved"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("FAILED", "Failed"),
    ]

    # ---- source email (repeated on each attachment row - deliberate, keeps
    #      the list API a single table read with no join) ----
    message_id = models.CharField(max_length=255, db_index=True)  # Graph message id, NOT unique alone
    sender = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    body_text = models.TextField(blank=True)  # PLAIN TEXT only, never HTML

    # ---- this attachment ----
    attachment_id = models.CharField(max_length=255)  # Graph attachment id
    attachment_name = models.CharField(max_length=255, blank=True)  # e.g. "PO_4471.xlsx"
    file_type = models.CharField(max_length=10, blank=True)  # pdf / docx / xlsx / csv / png ...
    file_size = models.IntegerField(default=0)  # bytes
    s3_key = models.CharField(max_length=500, blank=True)

    # ---- extraction result ----
    # {"po_number": {"value": "PO123", "confidence": 0.93, "edited": false}, ...}
    extracted_data = models.JSONField(default=dict, blank=True)
    min_confidence = models.FloatField(default=0.0, db_index=True)  # lowest field confidence

    # ---- OE code, answered by the pgvector RAG agent (PLAN.md Appendix A) ----
    oe_code = models.CharField(max_length=50, blank=True)  # winning candidate
    oe_confidence = models.FloatField(default=0.0)  # similarity score 0-1
    oe_matched = models.BooleanField(default=False)  # score >= OE_MATCH_THRESHOLD
    # top-k alternatives, shown to the reviewer as a pick-list:
    # [{"oe_code": "OE-IN-0042", "customer_name": "...", "score": 0.91,
    #   "match_type": "vector"}, ...]
    oe_candidates = models.JSONField(default=list, blank=True)

    # ---- workflow ----
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="NEW", db_index=True)
    reviewed_by = models.CharField(max_length=150, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comment = models.TextField(blank=True)
    error_message = models.TextField(blank=True)

    # ---- SAP CSV export (only set once an APPROVED/AUTO_APPROVED order has
    #      been written out to S3_OUTPUT_BUCKET) ----
    exported_at = models.DateTimeField(null=True, blank=True)
    export_s3_key = models.CharField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # This is the dedupe guard: re-reading an email can never create a
        # second row for an attachment that was already processed.
        unique_together = ("message_id", "attachment_id")
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order #{self.pk} [{self.status}] {self.attachment_name}"


class AuditLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="audit_logs")
    # EMAIL_RECEIVED / EXTRACTED / AUTO_APPROVED / FIELD_EDITED / APPROVED /
    # REJECTED / FAILED
    action = models.CharField(max_length=50)
    actor = models.CharField(max_length=150, default="system")
    field_name = models.CharField(max_length=100, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"AuditLog order={self.order_id} action={self.action}"
