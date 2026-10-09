"""
Django settings for the assessment platform.

Configuration is read from environment variables. For local development,
copy .env.example to .env (which is git-ignored); a tiny loader below reads it
without needing any third-party package.
"""

import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Minimal .env loader: KEY=VALUE lines; real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Real environment variables always win over .env; set DJANGO_SKIP_DOTENV=1 to ignore the file entirely
# (used by the deployment tests so a developer's local .env cannot change their result).
if os.environ.get("DJANGO_SKIP_DOTENV", "").lower() not in {"1", "true", "yes", "on"}:
    _load_dotenv(BASE_DIR / ".env")


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def _env_int(name, default):
    return int(os.environ.get(name, default))


# `manage.py test` runs with DEBUG forced off, so production-only HTTPS redirects must not apply
# to the test client (production behaviour is checked by config/tests.py with real env values).
TESTING = sys.argv[1:2] == ["test"]


# --- Core security settings --------------------------------------------------

DEBUG = _env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        # Ephemeral dev-only key: sessions reset on restart. Never used when DEBUG is off.
        SECRET_KEY = get_random_secret_key()
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is not set. Copy .env.example to .env and set it."
        )

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
# Origins (scheme + host) allowed to submit CSRF-protected POSTs over HTTPS, e.g. https://app.example.com
CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Render sets this to the service's public hostname; trusting it avoids a redeploy to learn the URL.
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_render_host}")


# --- Applications ------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "assessments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serves collected static files (no separate web server needed)
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.NoStoreForAuthenticatedUsersMiddleware",
    "accounts.middleware.ForcePasswordChangeMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

WSGI_APPLICATION = "config.wsgi.application"


# --- Database ----------------------------------------------------------------

def _database_from_url(url):
    """Parse DATABASE_URL (postgres://user:pass@host:5432/name?sslmode=require) without extra packages."""
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must be a postgres:// or postgresql:// URL.")
    config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "CONN_MAX_AGE": _env_int("DJANGO_DB_CONN_MAX_AGE", 60),
        "CONN_HEALTH_CHECKS": True,
    }
    options = dict(parse_qsl(parsed.query))
    if options:
        config["OPTIONS"] = options  # e.g. sslmode=require
    return config


# Production uses PostgreSQL (DATABASE_URL); without it, development falls back to SQLite.
if os.environ.get("DATABASE_URL"):
    DATABASES = {"default": _database_from_url(os.environ["DATABASE_URL"])}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# --- Authentication ----------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:dashboard"  # role-aware router; the login view also redirects by role
LOGOUT_REDIRECT_URL = "accounts:login"


# --- Internationalization ----------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True


# --- Static files ------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"  # `collectstatic` target; served by WhiteNoise, git-ignored
STATICFILES_DIRS = [BASE_DIR / "static"]
# Compressed (no manifest): works without a prior collectstatic, which keeps tests and local runs simple.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- HTTPS and cookie security (production) -------------------------------------
#
# Everything below turns ON when DEBUG is off, so a local `DJANGO_DEBUG=True` run keeps working
# over plain HTTP. Each value can be overridden by an environment variable.

_secure = not DEBUG

SESSION_COOKIE_SECURE = _env_bool("DJANGO_SESSION_COOKIE_SECURE", _secure)
CSRF_COOKIE_SECURE = _env_bool("DJANGO_CSRF_COOKIE_SECURE", _secure)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Redirect HTTP -> HTTPS. Many platforms terminate TLS at a proxy and call the app over HTTP; if
# so, set DJANGO_BEHIND_TLS_PROXY=True (below) or this redirect would loop.
SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", _secure and not TESTING)

# Trust X-Forwarded-Proto ONLY when a reverse proxy you control sets/overwrites it on every request
# (Render, Heroku, Fly and similar do). Never enable it when clients can reach the app directly.
if _env_bool("DJANGO_BEHIND_TLS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HSTS makes browsers refuse plain HTTP for the host. Start short, raise it once HTTPS is verified.
SECURE_HSTS_SECONDS = _env_int("DJANGO_HSTS_SECONDS", 86400 if _secure and not TESTING else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", SECURE_HSTS_SECONDS > 0)
SECURE_HSTS_PRELOAD = _env_bool("DJANGO_HSTS_PRELOAD", False)  # a deliberate, hard-to-undo opt-in

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"


# --- Logging --------------------------------------------------------------------
# With DEBUG off Django would otherwise send 500 tracebacks nowhere (only mail_admins). Log to the
# console so the platform's log viewer shows them.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {"django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False}},
}
