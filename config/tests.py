"""
Deployment configuration tests.

Production behaviour depends on environment variables read once at import time, so most of
these tests start a fresh interpreter with production-like values rather than patching settings.
"""

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from config.settings import _database_from_url

BASE_DIR = Path(settings.BASE_DIR)
SECRET = "k" * 20 + "9!x#" * 10  # long, varied, not 'django-insecure-'


def run_manage(*args, **env):
    """Run manage.py in a clean interpreter with exactly these production-style env values."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "DATABASE_URL", "RENDER_"))}
    clean.update({"DJANGO_SKIP_DOTENV": "1", "DJANGO_SECRET_KEY": SECRET, "DJANGO_ALLOWED_HOSTS": "app.example.test", **env})
    return subprocess.run(
        [sys.executable, "manage.py", *args], cwd=BASE_DIR, env=clean, capture_output=True, text=True, timeout=120
    )


class ProductionSettingsTests(SimpleTestCase):
    def dump(self, **env):
        code = (
            "import json, django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
            "from django.conf import settings as s; "
            "print(json.dumps({k: getattr(s, k, None) for k in ['DEBUG','SESSION_COOKIE_SECURE','CSRF_COOKIE_SECURE',"
            "'SECURE_SSL_REDIRECT','SECURE_HSTS_SECONDS','SECURE_PROXY_SSL_HEADER','ALLOWED_HOSTS','CSRF_TRUSTED_ORIGINS']}))"
        )
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "DATABASE_URL", "RENDER_"))}
        clean.update({"DJANGO_SKIP_DOTENV": "1", **env})
        out = subprocess.run([sys.executable, "-c", code], cwd=BASE_DIR, env=clean, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        import json

        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_production_defaults_are_secure(self):
        conf = self.dump(DJANGO_SECRET_KEY=SECRET, DJANGO_ALLOWED_HOSTS="app.example.test")
        self.assertFalse(conf["DEBUG"])  # DEBUG defaults to off
        self.assertTrue(conf["SESSION_COOKIE_SECURE"])
        self.assertTrue(conf["CSRF_COOKIE_SECURE"])
        self.assertTrue(conf["SECURE_SSL_REDIRECT"])
        self.assertGreater(conf["SECURE_HSTS_SECONDS"], 0)
        self.assertIsNone(conf["SECURE_PROXY_SSL_HEADER"])  # proxy header is NOT trusted unless asked
        self.assertEqual(conf["ALLOWED_HOSTS"], ["app.example.test"])

    def test_debug_mode_stays_convenient_for_local_development(self):
        conf = self.dump(DJANGO_DEBUG="True")  # no secret key needed, plain HTTP works
        self.assertTrue(conf["DEBUG"])
        self.assertFalse(conf["SESSION_COOKIE_SECURE"])
        self.assertFalse(conf["CSRF_COOKIE_SECURE"])
        self.assertFalse(conf["SECURE_SSL_REDIRECT"])
        self.assertEqual(conf["SECURE_HSTS_SECONDS"], 0)

    def test_proxy_header_and_origins_come_from_the_environment(self):
        conf = self.dump(
            DJANGO_SECRET_KEY=SECRET, DJANGO_BEHIND_TLS_PROXY="True",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://a.example, https://b.example", RENDER_EXTERNAL_HOSTNAME="svc.onrender.com",
        )
        self.assertEqual(conf["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertEqual(conf["CSRF_TRUSTED_ORIGINS"], ["https://a.example", "https://b.example", "https://svc.onrender.com"])
        self.assertIn("svc.onrender.com", conf["ALLOWED_HOSTS"])

    def test_missing_secret_key_refuses_to_start_in_production(self):
        result = run_manage("check", DJANGO_SECRET_KEY="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY is not set", result.stderr)

    def test_check_deploy_has_only_the_explained_preload_warning(self):
        result = run_manage("check", "--deploy", DJANGO_DEBUG="False")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        warnings = [line for line in result.stdout.splitlines() if "security.W" in line]
        # W021 (HSTS preload) is a deliberate opt-in: preload is hard to undo (see README).
        self.assertEqual([w for w in warnings if "W021" not in w], [], result.stdout)

    def test_check_deploy_is_fully_clean_when_preload_is_opted_into(self):
        result = run_manage("check", "--deploy", DJANGO_HSTS_PRELOAD="True", DJANGO_HSTS_SECONDS="31536000")
        self.assertIn("no issues", result.stdout, result.stdout)

    def test_collectstatic_works_on_a_fresh_checkout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as target:
            code = (
                "import os, django; os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; "
                f"from django.conf import settings; django.setup(); settings.STATIC_ROOT={target!r}; "
                "from django.core.management import call_command; call_command('collectstatic', interactive=False, verbosity=0)"
            )
            clean = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
            clean.update({"DJANGO_SKIP_DOTENV": "1", "DJANGO_SECRET_KEY": SECRET})
            out = subprocess.run([sys.executable, "-c", code], cwd=BASE_DIR, env=clean, capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue((Path(target) / "admin" / "css" / "base.css").exists())


class DatabaseUrlTests(SimpleTestCase):
    def test_postgres_url_is_parsed_including_encoded_characters_and_options(self):
        db = _database_from_url("postgres://app_user:p%40ss%2Fword@db.internal:6543/assess?sslmode=require")
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual((db["NAME"], db["USER"], db["PASSWORD"], db["HOST"], db["PORT"]), ("assess", "app_user", "p@ss/word", "db.internal", "6543"))
        self.assertEqual(db["OPTIONS"], {"sslmode": "require"})
        self.assertTrue(db["CONN_HEALTH_CHECKS"])

    def test_postgresql_scheme_and_defaults(self):
        db = _database_from_url("postgresql://u:p@h/db")
        self.assertEqual((db["HOST"], db["PORT"]), ("h", ""))
        self.assertNotIn("OPTIONS", db)

    def test_non_postgres_urls_are_rejected(self):
        for url in ("mysql://u:p@h/db", "sqlite:///x.db", "not a url"):
            with self.subTest(url=url), self.assertRaises(ImproperlyConfigured):
                _database_from_url(url)


class HttpsBehaviourTests(SimpleTestCase):
    """What the production flags do to real requests (the test client with the flags switched on)."""

    @override_settings(SECURE_SSL_REDIRECT=True, ALLOWED_HOSTS=["testserver"])
    def test_plain_http_is_redirected_to_https(self):
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].startswith("https://"))

    @override_settings(
        SECURE_SSL_REDIRECT=True, SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True, SECURE_HSTS_SECONDS=86400, ALLOWED_HOSTS=["testserver"],
    )
    def test_behind_a_tls_proxy_requests_are_served_with_secure_cookies_and_hsts(self):
        response = self.client.get("/login/", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies["csrftoken"]["secure"])
        self.assertIn("max-age=86400", response["Strict-Transport-Security"])
        self.assertEqual(response["X-Frame-Options"], "DENY")
        # a client cannot make an HTTP request look secure unless the proxy header is trusted
        self.assertEqual(self.client.get("/login/").status_code, 301)

    def test_static_files_are_served_by_the_application_stack(self):
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE)
        self.assertLess(
            settings.MIDDLEWARE.index("django.middleware.security.SecurityMiddleware"),
            settings.MIDDLEWARE.index("whitenoise.middleware.WhiteNoiseMiddleware"),
        )
