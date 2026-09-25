"""
Django settings for opticbot project (OPTIC BOT).

All secrets and tunables are read from a `.env` file in the project root -
never hardcode a credential here. See `.env.example` for the full key list.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env before anything below reads os.getenv(...)
load_dotenv(BASE_DIR / ".env")


def env_bool(key, default="False"):
    """'True'/'true'/'1' -> True, everything else -> False."""
    return os.getenv(key, default).strip().lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Core Django
# ---------------------------------------------------------------------------

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-dev-only-change-me")
DEBUG = env_bool("DEBUG", "True")

_hosts = os.getenv("ALLOWED_HOSTS", "*")
ALLOWED_HOSTS = ["*"] if _hosts.strip() == "*" else [h.strip() for h in _hosts.split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "optic_bot",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "opticbot.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "opticbot.wsgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# DB_ENGINE=sqlite runs with ZERO credentials (local dev / testing).
# DB_ENGINE=postgres is used on EC2 against RDS. This switch is what lets the
# whole app boot and be verified before any real credentials exist.

if os.getenv("DB_ENGINE", "sqlite").strip().lower() == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", ""),
            "USER": os.getenv("DB_USER", ""),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", ""),
            "PORT": os.getenv("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# REST framework
# ---------------------------------------------------------------------------
# JWT auth is handled by the FRONTEND, not this backend (user decision).
# No auth/permission classes here - reviewer identity arrives as plain data
# (reviewed_by in the request body). Keep this service on a private
# subnet / security group reachable only from the frontend's origin, as the
# architecture diagram shows, since anything that can reach these endpoints
# can act as any reviewer.

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}

# ---------------------------------------------------------------------------
# Prompts folder
# ---------------------------------------------------------------------------

PROMPTS_DIR = BASE_DIR / "prompts"

# ---------------------------------------------------------------------------
# Microsoft Outlook / Graph
# ---------------------------------------------------------------------------

MS_TENANT_ID = os.getenv("MS_TENANT_ID", "")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "")
MAILBOX_USER_EMAIL = os.getenv("MAILBOX_USER_EMAIL", "")
MAIL_TARGET_FOLDER = os.getenv("MAIL_TARGET_FOLDER", "OPTIC BOT")
MAIL_POLL_MINUTES = int(os.getenv("MAIL_POLL_MINUTES", "5"))
MAIL_BATCH_SIZE = int(os.getenv("MAIL_BATCH_SIZE", "20"))
MARK_MAIL_AS_READ = env_bool("MARK_MAIL_AS_READ", "True")
INCLUDE_BODY_AS_CONTEXT = env_bool("INCLUDE_BODY_AS_CONTEXT", "True")
MAX_BODY_CHARS = int(os.getenv("MAX_BODY_CHARS", "4000"))

# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = [
    e.strip().lower()
    for e in os.getenv("ALLOWED_EXTENSIONS", "pdf,docx,doc,xlsx,xls,csv,png,jpg,jpeg").split(",")
    if e.strip()
]
# NO SIZE LIMITS ON ATTACHMENTS. An order file may be 2 KB or 50 MB - it
# is downloaded, stored in S3 and processed either way. Size is never a
# reason to skip an attachment: a 5.5 KB .xls and a 13.35 KB .pdf in the
# sample data are both genuine orders, and a large scanned PDF is just as
# real. Signature logos are excluded by the isInline check instead, and
# anything that slips through is rejected by the prompt's is_order flag.
SKIP_INLINE_ATTACHMENTS = env_bool("SKIP_INLINE_ATTACHMENTS", "True")

# This is NOT a filter - nothing is skipped because of it. It is the
# largest document the LLM provider will accept in one converse call
# (Bedrock's documented per-document limit; verify for your model/region).
# A file above it is still downloaded and stored, then the order is saved
# as FAILED with an explicit message so a human can act on it, rather than
# the attachment being dropped or a cryptic provider error surfacing.
LLM_MAX_DOCUMENT_MB = float(os.getenv("LLM_MAX_DOCUMENT_MB", "4.5"))

# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
S3_INPUT_BUCKET = os.getenv("S3_INPUT_BUCKET", "")
S3_OUTPUT_BUCKET = os.getenv("S3_OUTPUT_BUCKET", "")

# ---------------------------------------------------------------------------
# OE master lookup - DIRECT pgvector query, same Postgres instance as
# DATABASES['default'] above. No separate service, no URL - the backend
# opens its own connection (django.db.connection, the one already
# configured by DB_ENGINE/DB_HOST/etc.) and runs the cosine-similarity SQL
# itself. See PLAN.md Appendix A for the exact table this expects and
# services.py Section C for the query. Requires DB_ENGINE=postgres with
# the pgvector extension; under DB_ENGINE=sqlite this degrades gracefully
# (no pgvector support), same as OE_MATCHING_ENABLED=False.
# ---------------------------------------------------------------------------

OE_MATCHING_ENABLED = env_bool("OE_MATCHING_ENABLED", "True")
# Must match the model/dimension used to populate oe_master.embedding, or
# every score will be meaningless. Titan v2 1024-dim is the default -
# change both together if you embed with something else.
OE_EMBEDDING_MODEL_ID = os.getenv("OE_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
OE_EMBEDDING_DIMENSION = int(os.getenv("OE_EMBEDDING_DIMENSION", "1024"))
OE_MATCH_THRESHOLD = float(os.getenv("OE_MATCH_THRESHOLD", "0.82"))
OE_TOP_K = int(os.getenv("OE_TOP_K", "5"))
# When the top vector score doesn't clear OE_MATCH_THRESHOLD, ask an LLM to
# reason over the candidates instead of going straight to human review.
OE_RERANK_ENABLED = env_bool("OE_RERANK_ENABLED", "True")
OE_RERANK_THRESHOLD = float(os.getenv("OE_RERANK_THRESHOLD", "0.75"))

# ---------------------------------------------------------------------------
# Email triage (order vs communication)
# ---------------------------------------------------------------------------
# Only skips an email when the classifier is CONFIDENT it is not an order.
# Fails open - see services.classify_email().
EMAIL_CLASSIFICATION_ENABLED = env_bool("EMAIL_CLASSIFICATION_ENABLED", "True")
EMAIL_CLASSIFICATION_THRESHOLD = float(os.getenv("EMAIL_CLASSIFICATION_THRESHOLD", "0.80"))

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "bedrock").strip().lower()
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-8")
LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "")
LLM_GATEWAY_API_KEY = os.getenv("LLM_GATEWAY_API_KEY", "")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192"))  # verbose per-eye JSON schema needs headroom for multi-item orders
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))

# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------

CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))
REQUIRE_OE_MATCH = env_bool("REQUIRE_OE_MATCH", "True")

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

RUN_SCHEDULER = env_bool("RUN_SCHEDULER", "False")

# ---------------------------------------------------------------------------
# SQS execution queue (PLAN.md §18)
# ---------------------------------------------------------------------------
# USE_SQS=False  -> the poller extracts inline, no queue involved.
# USE_SQS=True   -> the poller puts the document in S3 and sends one
#                   message per order; a worker job drains the queue.
# Retries and the DLQ are configured ON THE QUEUE in AWS (redrive policy
# with maxReceiveCount), not here - see services.py Section G.

USE_SQS = env_bool("USE_SQS", "False")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
SQS_DLQ_URL = os.getenv("SQS_DLQ_URL", "")  # monitoring only; AWS does the redrive
# Must comfortably exceed how long one extraction takes (LLM calls can run
# 30-120s) or SQS will redeliver a message that is still being worked on.
SQS_VISIBILITY_TIMEOUT = int(os.getenv("SQS_VISIBILITY_TIMEOUT", "300"))
SQS_WAIT_TIME_SECONDS = int(os.getenv("SQS_WAIT_TIME_SECONDS", "20"))  # long polling
SQS_MAX_MESSAGES = int(os.getenv("SQS_MAX_MESSAGES", "10"))  # per receive, AWS max is 10
# Caps how many batches one worker tick drains, so a huge backlog cannot
# make a single scheduled run last forever.
SQS_MAX_BATCHES = int(os.getenv("SQS_MAX_BATCHES", "5"))
SQS_WORKER_MINUTES = float(os.getenv("SQS_WORKER_MINUTES", "1"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
