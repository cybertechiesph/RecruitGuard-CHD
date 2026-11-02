import logging
import re


SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)\b(password|otp|token|secret|authorization|cookie|csrf|turnstile)\b"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"([^\"'\s,;&}\]]+)"
)
HEADER_PATTERN = re.compile(r"(?i)\b(authorization|cookie)\s*:\s*[^\r\n]+")


def redact_sensitive_text(value):
    text = str(value)
    text = HEADER_PATTERN.sub(lambda match: f"{match.group(1)}: [REDACTED]", text)
    return SENSITIVE_KEY_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )


class SensitiveDataFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        return True


def build_logging_config(log_file="", mail_admins=False):
    """F14: console logging always; a rotating log file when DJANGO_LOG_FILE is set, and an
    email to ADMINS on server errors when DJANGO_ADMINS is set. Both off by default, so a
    developer machine and the test suite log exactly as before."""
    handlers = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["redact_sensitive"],
        },
    }
    if log_file:
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": log_file,
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "timestamped",
            "filters": ["redact_sensitive"],
        }
    if mail_admins:
        handlers["mail_admins"] = {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            "filters": ["require_debug_false", "redact_sensitive"],
            "include_html": False,
        }
    base = [name for name in ("console", "file") if name in handlers]
    request_handlers = base + (["mail_admins"] if mail_admins else [])
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "redact_sensitive": {"()": "config.logging.SensitiveDataFilter"},
            "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
        },
        "formatters": {
            "standard": {"format": "%(levelname)s %(name)s %(message)s"},
            "timestamped": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": handlers,
        "loggers": {
            "django.request": {"handlers": request_handlers, "level": "WARNING", "propagate": False},
            "recruitment": {"handlers": base, "level": "INFO", "propagate": False},
        },
        "root": {"handlers": base, "level": "WARNING"},
    }
