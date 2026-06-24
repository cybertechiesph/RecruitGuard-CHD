import hashlib
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def env_list(name, default=""):
    raw_value = os.getenv(name, default)
    return [item.strip() for item in raw_value.split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", True)

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = hashlib.sha256(f"{BASE_DIR}-recruitguard-debug".encode("utf-8")).hexdigest()
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is False.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
APPLICANT_PORTAL_BASE_URL = (os.getenv("APPLICANT_PORTAL_BASE_URL") or "").strip().rstrip("/")

if not DEBUG:
    local_only_hosts = {"127.0.0.1", "localhost", "[::1]"}
    if set(ALLOWED_HOSTS).issubset(local_only_hosts):
        raise ImproperlyConfigured(
            "DJANGO_ALLOWED_HOSTS must include the deployed host when DJANGO_DEBUG is False."
        )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "recruitment",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "config.middleware.RateLimitMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
            "libraries": {
                "recruitment_ui": "recruitment.templatetags.recruitment_ui",
            },
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

POSTGRES_NAME = (os.getenv("POSTGRES_DB") or "").strip()
POSTGRES_USER = (os.getenv("POSTGRES_USER") or "").strip()
POSTGRES_PASSWORD = (os.getenv("POSTGRES_PASSWORD") or "").strip()
POSTGRES_HOST = (os.getenv("POSTGRES_HOST") or "localhost").strip()
POSTGRES_PORT = (os.getenv("POSTGRES_PORT") or "5432").strip()

if POSTGRES_NAME and POSTGRES_USER and POSTGRES_PASSWORD:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": POSTGRES_NAME,
            "USER": POSTGRES_USER,
            "PASSWORD": POSTGRES_PASSWORD,
            "HOST": POSTGRES_HOST,
            "PORT": POSTGRES_PORT,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "timeout": env_int("SQLITE_TIMEOUT_SECONDS", 30),
            },
        }
    }

SQLITE_BUSY_TIMEOUT_MS = env_int("SQLITE_BUSY_TIMEOUT_MS", 30000)

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

AUTH_USER_MODEL = "recruitment.RecruitmentUser"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Manila")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 10)
DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL",
    EMAIL_HOST_USER or "noreply@recruitguard.local",
)
APPLICATION_OTP_VALIDITY_MINUTES = env_int("APPLICATION_OTP_VALIDITY_MINUTES", 10)
APPLICATION_OTP_RESEND_COOLDOWN_SECONDS = env_int("APPLICATION_OTP_RESEND_COOLDOWN_SECONDS", 60)
APPLICATION_OTP_MAX_ATTEMPTS = env_int("APPLICATION_OTP_MAX_ATTEMPTS", 5)
APPLICATION_OTP_HASH_SECRET = os.getenv("APPLICATION_OTP_HASH_SECRET")
if not APPLICATION_OTP_HASH_SECRET:
    if DEBUG:
        APPLICATION_OTP_HASH_SECRET = SECRET_KEY
    else:
        raise ImproperlyConfigured(
            "APPLICATION_OTP_HASH_SECRET must be set when DJANGO_DEBUG is False."
        )

INTERNAL_MFA_OTP_VALIDITY_MINUTES = env_int("INTERNAL_MFA_OTP_VALIDITY_MINUTES", 5)
INTERNAL_MFA_MAX_ATTEMPTS = env_int("INTERNAL_MFA_MAX_ATTEMPTS", 5)
INTERNAL_MFA_RESEND_COOLDOWN_SECONDS = env_int("INTERNAL_MFA_RESEND_COOLDOWN_SECONDS", 60)
INTERNAL_MFA_OTP_HASH_SECRET = os.getenv("INTERNAL_MFA_OTP_HASH_SECRET")
if not INTERNAL_MFA_OTP_HASH_SECRET:
    if DEBUG:
        INTERNAL_MFA_OTP_HASH_SECRET = SECRET_KEY
    else:
        raise ImproperlyConfigured(
            "INTERNAL_MFA_OTP_HASH_SECRET must be set when DJANGO_DEBUG is False."
        )

EVIDENCE_ENCRYPTION_SECRET = os.getenv("EVIDENCE_ENCRYPTION_SECRET")
if not EVIDENCE_ENCRYPTION_SECRET:
    if DEBUG:
        EVIDENCE_ENCRYPTION_SECRET = SECRET_KEY
    else:
        raise ImproperlyConfigured(
            "EVIDENCE_ENCRYPTION_SECRET must be set when DJANGO_DEBUG is False."
        )

MAX_EVIDENCE_UPLOAD_BYTES = env_int("MAX_EVIDENCE_UPLOAD_BYTES", 5 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int(
    "DATA_UPLOAD_MAX_MEMORY_SIZE",
    MAX_EVIDENCE_UPLOAD_BYTES + (1024 * 1024),
)
CAPTCHA_ENABLED = env_bool("CAPTCHA_ENABLED", True)
CAPTCHA_PROVIDER = (os.getenv("CAPTCHA_PROVIDER") or "local").strip().lower()
RECAPTCHA_TEST_SITE_KEY = "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"
RECAPTCHA_TEST_SECRET_KEY = "6LeIxAcTAAAAAGG-vFI1TnRWxMZNFuojJ4WifJWe"
RECAPTCHA_USE_TEST_KEYS = env_bool("RECAPTCHA_USE_TEST_KEYS", DEBUG)
RECAPTCHA_SITE_KEY = (os.getenv("RECAPTCHA_SITE_KEY") or "").strip()
RECAPTCHA_SECRET_KEY = (os.getenv("RECAPTCHA_SECRET_KEY") or "").strip()
RECAPTCHA_TIMEOUT_SECONDS = env_int("RECAPTCHA_TIMEOUT_SECONDS", 5)
TURNSTILE_TEST_SITE_KEY = "1x00000000000000000000AA"
TURNSTILE_TEST_SECRET_KEY = "1x0000000000000000000000000000000AA"
TURNSTILE_USE_TEST_KEYS = env_bool("TURNSTILE_USE_TEST_KEYS", DEBUG)
TURNSTILE_SITE_KEY = (os.getenv("TURNSTILE_SITE_KEY") or "").strip()
TURNSTILE_VERIFY_URL = (os.getenv("TURNSTILE_VERIFY_URL") or "").strip().rstrip("/")
TURNSTILE_SECRET_KEY = (os.getenv("TURNSTILE_SECRET_KEY") or "").strip()
TURNSTILE_TIMEOUT_SECONDS = env_int("TURNSTILE_TIMEOUT_SECONDS", 5)
TURNSTILE_VERIFY_ALLOWED_HOSTS = env_list(
    "TURNSTILE_VERIFY_ALLOWED_HOSTS",
    "challenges.cloudflare.com,turnstile-siteverify-recruitguard-chd.recruitguard-chd.workers.dev",
)

if RECAPTCHA_USE_TEST_KEYS:
    if not DEBUG:
        raise ImproperlyConfigured(
            "RECAPTCHA_USE_TEST_KEYS cannot be enabled when DJANGO_DEBUG is False."
        )
    RECAPTCHA_SITE_KEY = RECAPTCHA_TEST_SITE_KEY
    RECAPTCHA_SECRET_KEY = RECAPTCHA_TEST_SECRET_KEY

if TURNSTILE_USE_TEST_KEYS:
    if not DEBUG:
        raise ImproperlyConfigured(
            "TURNSTILE_USE_TEST_KEYS cannot be enabled when DJANGO_DEBUG is False."
        )
    TURNSTILE_SITE_KEY = TURNSTILE_TEST_SITE_KEY
    TURNSTILE_SECRET_KEY = TURNSTILE_TEST_SECRET_KEY
    TURNSTILE_VERIFY_URL = ""

if not DEBUG and CAPTCHA_ENABLED and CAPTCHA_PROVIDER == "turnstile":
    if not TURNSTILE_SITE_KEY:
        raise ImproperlyConfigured("TURNSTILE_SITE_KEY must be set for production Turnstile CAPTCHA.")
    if not TURNSTILE_VERIFY_URL and not TURNSTILE_SECRET_KEY:
        raise ImproperlyConfigured(
            "Set TURNSTILE_VERIFY_URL or TURNSTILE_SECRET_KEY for production Turnstile CAPTCHA."
        )

if not DEBUG and CAPTCHA_ENABLED and CAPTCHA_PROVIDER == "recaptcha":
    if not RECAPTCHA_SITE_KEY or not RECAPTCHA_SECRET_KEY:
        raise ImproperlyConfigured(
            "RECAPTCHA_SITE_KEY and RECAPTCHA_SECRET_KEY must be set for production reCAPTCHA."
        )

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_INACTIVITY_TIMEOUT_SECONDS = env_int("SESSION_INACTIVITY_TIMEOUT_SECONDS", 30 * 60)
SESSION_COOKIE_AGE = SESSION_INACTIVITY_TIMEOUT_SECONDS
SESSION_SAVE_EVERY_REQUEST = env_bool("SESSION_SAVE_EVERY_REQUEST", True)
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool("SESSION_EXPIRE_AT_BROWSER_CLOSE", False)
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = os.getenv("CSRF_COOKIE_SAMESITE", "Lax")
SESSION_COOKIE_SECURE = True if not DEBUG else env_bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = True if not DEBUG else env_bool("CSRF_COOKIE_SECURE", False)
SECURE_SSL_REDIRECT = True if not DEBUG else env_bool("SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0 if DEBUG else 31536000)
if not DEBUG:
    SECURE_HSTS_SECONDS = max(SECURE_HSTS_SECONDS, 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", not DEBUG)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

RATE_LIMIT_ENABLED = env_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_WINDOW_SECONDS = env_int("RATE_LIMIT_WINDOW_SECONDS", 60)
RATE_LIMIT_TRUST_X_FORWARDED_FOR = env_bool("RATE_LIMIT_TRUST_X_FORWARDED_FOR", False)
RATE_LIMIT_RULES = {
    "default": {
        "limit": env_int("RATE_LIMIT_DEFAULT_MAX_REQUESTS", 300),
        "window": RATE_LIMIT_WINDOW_SECONDS,
    },
    "auth": {
        "limit": env_int("RATE_LIMIT_AUTH_MAX_REQUESTS", 30),
        "window": RATE_LIMIT_WINDOW_SECONDS,
    },
    "otp": {
        "limit": env_int("RATE_LIMIT_OTP_MAX_REQUESTS", 10),
        "window": RATE_LIMIT_WINDOW_SECONDS,
    },
    "upload": {
        "limit": env_int("RATE_LIMIT_UPLOAD_MAX_REQUESTS", 60),
        "window": RATE_LIMIT_WINDOW_SECONDS,
    },
}

INTERNAL_LOGIN_MAX_ATTEMPTS = env_int("INTERNAL_LOGIN_MAX_ATTEMPTS", 5)
INTERNAL_LOGIN_WINDOW_MINUTES = env_int("INTERNAL_LOGIN_WINDOW_MINUTES", 15)
INTERNAL_LOGIN_LOCKOUT_MINUTES = env_int("INTERNAL_LOGIN_LOCKOUT_MINUTES", 15)
INTERNAL_LOGIN_ALERT_EMAILS = env_list("INTERNAL_LOGIN_ALERT_EMAILS")
INTERNAL_EMAIL_CHANGE_TOKEN_VALIDITY_HOURS = env_int(
    "INTERNAL_EMAIL_CHANGE_TOKEN_VALIDITY_HOURS",
    24,
)
PASSWORD_HISTORY_LIMIT = env_int("PASSWORD_HISTORY_LIMIT", 5)
PASSWORD_RESET_TIMEOUT = env_int("PASSWORD_RESET_TIMEOUT_SECONDS", 3600)
PASSWORD_RESET_EMAIL_COOLDOWN_SECONDS = env_int(
    "PASSWORD_RESET_EMAIL_COOLDOWN_SECONDS",
    300,
)
PASSWORD_RESET_EMAIL_MAX_PER_WINDOW = env_int(
    "PASSWORD_RESET_EMAIL_MAX_PER_WINDOW",
    3,
)
PASSWORD_RESET_EMAIL_WINDOW_SECONDS = env_int(
    "PASSWORD_RESET_EMAIL_WINDOW_SECONDS",
    3600,
)
PASSWORD_RESET_IP_MAX_PER_WINDOW = env_int(
    "PASSWORD_RESET_IP_MAX_PER_WINDOW",
    10,
)
PASSWORD_RESET_IP_WINDOW_SECONDS = env_int(
    "PASSWORD_RESET_IP_WINDOW_SECONDS",
    3600,
)

CLOUDFLARE_TURNSTILE_ORIGIN = "https://challenges.cloudflare.com"
GOOGLE_RECAPTCHA_ORIGIN = "https://www.google.com"
GOOGLE_RECAPTCHA_STATIC_ORIGIN = "https://www.gstatic.com"
GOOGLE_RECAPTCHA_FRAME_ORIGIN = "https://recaptcha.google.com"

CSP_DIRECTIVES = {
    "default-src": ("'self'",),
    "script-src": (
        "'self'",
        "'unsafe-inline'",
        "https://cdn.jsdelivr.net",
        CLOUDFLARE_TURNSTILE_ORIGIN,
        GOOGLE_RECAPTCHA_ORIGIN,
        GOOGLE_RECAPTCHA_STATIC_ORIGIN,
    ),
    "style-src": (
        "'self'",
        "'unsafe-inline'",
        "https://cdn.jsdelivr.net",
        "https://fonts.googleapis.com",
    ),
    "font-src": ("'self'", "https://fonts.gstatic.com", "data:"),
    "img-src": ("'self'", "data:"),
    "connect-src": (
        "'self'",
        CLOUDFLARE_TURNSTILE_ORIGIN,
        GOOGLE_RECAPTCHA_ORIGIN,
    ),
    "frame-src": (
        CLOUDFLARE_TURNSTILE_ORIGIN,
        GOOGLE_RECAPTCHA_ORIGIN,
        GOOGLE_RECAPTCHA_FRAME_ORIGIN,
    ),
    "object-src": ("'none'",),
    "base-uri": ("'self'",),
    "form-action": ("'self'",),
    "frame-ancestors": ("'none'",),
}
if not DEBUG:
    CSP_DIRECTIVES["upgrade-insecure-requests"] = ()

PERMISSIONS_POLICY = {
    "camera": (),
    "geolocation": (),
    "microphone": (),
    "payment": (),
    "usb": (),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_sensitive": {
            "()": "config.logging.SensitiveDataFilter",
        },
    },
    "formatters": {
        "standard": {
            "format": "%(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["redact_sensitive"],
        },
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "recruitment": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
