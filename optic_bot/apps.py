import os

from django.apps import AppConfig
from django.conf import settings


class OpticBotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "optic_bot"

    def ready(self):
        if not settings.RUN_SCHEDULER:
            return
        # RUN_MAIN check stops the dev-server autoreloader (which spawns two
        # processes) from starting the scheduler twice. In production
        # (DEBUG=False) there is no autoreloader, so always start it there.
        if os.environ.get("RUN_MAIN") == "true" or not settings.DEBUG:
            from . import scheduler
            scheduler.start()
