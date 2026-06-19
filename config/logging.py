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
