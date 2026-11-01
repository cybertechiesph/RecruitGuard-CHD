import re

from django.utils.translation import gettext_lazy as _

APPLICANT_MOBILE_ERROR_MESSAGE = _(
    "Enter a Philippine mobile number, like 0917 123 4567."
)

_APPLICANT_MOBILE_SEPARATORS_RE = re.compile(r"[ ()-]")
_APPLICANT_LOCAL_MOBILE_RE = re.compile(r"09\d{9}")
_APPLICANT_INTERNATIONAL_MOBILE_RE = re.compile(r"(?:\+63|63)9\d{9}")


def normalize_philippine_mobile_number(value):
    compact_value = _APPLICANT_MOBILE_SEPARATORS_RE.sub("", (value or "").strip())
    if _APPLICANT_LOCAL_MOBILE_RE.fullmatch(compact_value):
        return f"+63{compact_value[1:]}"
    if _APPLICANT_INTERNATIONAL_MOBILE_RE.fullmatch(compact_value):
        return compact_value if compact_value.startswith("+") else f"+{compact_value}"
    raise ValueError(APPLICANT_MOBILE_ERROR_MESSAGE)


def display_philippine_mobile_number(value):
    """+639171234567 -> "0917 123 4567", the way applicants write it (and the hint shows).
    Anything that is not a stored PH mobile number is returned unchanged."""
    value = (value or "").strip()
    if len(value) == 13 and value.startswith("+639") and value[1:].isdigit():
        local = "0" + value[3:]
        return f"{local[:4]} {local[4:7]} {local[7:]}"
    return value
