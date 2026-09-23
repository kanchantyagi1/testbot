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
MAX_ATTACHMENT_MB = float(os.getenv("MAX_ATTACHMENT_MB", "4.5"))
MIN_ATTACHMENT_KB = float(os.getenv("MIN_ATTACHMENT_KB", "10"))
SKIP_INLINE_ATTACHMENTS = env_bool("SKIP_INLINE_ATTACHMENTS", "True")

# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
S3_INPUT_BUCKET = os.getenv("S3_INPUT_BUCKET", "")
S3_OUTPUT_BUCKET = os.getenv("S3_OUTPUT_BUCKET", "")

# ---------------------------------------------------------------------------
# OE master lookup (pgvector RAG agent, built separately by the user)
# ---------------------------------------------------------------------------

OE_RAG_URL = os.getenv("OE_RAG_URL", "")
OE_RAG_API_KEY = os.getenv("OE_RAG_API_KEY", "")
OE_RAG_TIMEOUT = int(os.getenv("OE_RAG_TIMEOUT", "30"))
OE_MATCH_THRESHOLD = float(os.getenv("OE_MATCH_THRESHOLD", "0.82"))
OE_TOP_K = int(os.getenv("OE_TOP_K", "5"))

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "bedrock").strip().lower()
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-8")
LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "")
LLM_GATEWAY_API_KEY = os.getenv("LLM_GATEWAY_API_KEY", "")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
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
# Not used in phase 1 (seam reserved for the SQS worker, see PLAN.md §13)
# ---------------------------------------------------------------------------

USE_SQS = env_bool("USE_SQS", "False")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")

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
