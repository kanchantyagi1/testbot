"""
Background jobs. Started from apps.py.

    poll_outlook   - every MAIL_POLL_MINUTES (default 5). Always runs.
    drain_sqs      - every SQS_WORKER_MINUTES (default 1). ONLY when
                     USE_SQS=True; this is the "AI processing service"
                     worker from the architecture diagram.

With USE_SQS=False there is one job and it does everything inline.
With USE_SQS=True the poller only ingests (S3 + queue) and this worker
does the extraction, so the two can be scaled and restarted separately.

On EC2 under gunicorn, run gunicorn with --workers 1 so only one process
runs these jobs, or set RUN_SCHEDULER=False on the web workers and run one
dedicated scheduler process instead. Two overlapping schedulers would poll
the mailbox twice and race on the same messages.

The SQS worker is safe to run in more than one process (that is the point
of a queue) - SQS only hands a message to one consumer at a time, and
process_queued_message() skips orders already in a terminal state. The
MAILBOX poller is not; keep that to a single process.
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings

from .services import ConfigError, drain_sqs_queue, poll_mailbox

logger = logging.getLogger(__name__)

_scheduler = None


def run_poll():
    """Thin wrapper so a missing .env value just logs instead of crashing
    the background thread."""
    try:
        summary = poll_mailbox()
        logger.info("Scheduled poll finished: %s", summary)
    except ConfigError as e:
        logger.warning("Scheduled poll skipped - %s", e)
    except Exception:
        logger.exception("Scheduled poll failed unexpectedly")


def run_sqs_worker():
    """Drain the execution queue. Never raises out of the thread - a
    failure here must not kill the scheduler, and individual message
    failures are already left on the queue for SQS to retry/DLQ."""
    try:
        summary = drain_sqs_queue()
        if summary["received"]:
            logger.info("SQS worker finished: %s", summary)
    except ConfigError as e:
        logger.warning("SQS worker skipped - %s", e)
    except Exception:
        logger.exception("SQS worker failed unexpectedly")


def start():
    global _scheduler
    if _scheduler is not None:
        return  # already running - never start a second one

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        run_poll,
        "interval",
        minutes=settings.MAIL_POLL_MINUTES,
        id="poll_outlook",
        max_instances=1,       # never overlap two polls
        replace_existing=True,
        coalesce=True,         # missed runs fire once, not N times
        # UTC, not naive local time: the scheduler's timezone is UTC, so a
        # naive datetime.now() on a machine ahead of UTC (e.g. IST, UTC+5:30)
        # would be read as "5:30 from now", not "now".
        next_run_time=datetime.now(timezone.utc),  # also run once immediately on boot
    )
    _scheduler.start()
    logger.info(
        "Scheduler started - polling Outlook every %s minutes", settings.MAIL_POLL_MINUTES
    )

    if settings.USE_SQS:
        _scheduler.add_job(
            run_sqs_worker,
            "interval",
            minutes=settings.SQS_WORKER_MINUTES,
            id="drain_sqs",
            max_instances=1,   # one drain at a time in THIS process
            replace_existing=True,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc),
        )
        logger.info(
            "SQS worker started - draining the execution queue every %s minute(s)",
            settings.SQS_WORKER_MINUTES,
        )
