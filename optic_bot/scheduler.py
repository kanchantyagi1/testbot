"""
Polls Outlook every MAIL_POLL_MINUTES (default 5). Started from apps.py.

On EC2 under gunicorn, run gunicorn with --workers 1 so only one process
runs this job, or set RUN_SCHEDULER=False on the web workers and run one
dedicated scheduler process instead. Two overlapping schedulers would poll
the mailbox twice and race on the same messages.
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings

from .services import ConfigError, poll_mailbox

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
