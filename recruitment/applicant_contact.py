import re


APPLICANT_MOBILE_ERROR_MESSAGE = (
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
