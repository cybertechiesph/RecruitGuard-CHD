import ipaddress
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings


CAPTCHA_ANSWER_SESSION_KEY = "captcha_{scope}_answer"
CAPTCHA_PROMPT_SESSION_KEY = "captcha_{scope}_prompt"
TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
RECAPTCHA_SITEVERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"


def captcha_is_enabled():
    return getattr(settings, "CAPTCHA_ENABLED", True)


def captcha_provider():
    return (getattr(settings, "CAPTCHA_PROVIDER", "local") or "local").lower()


def captcha_uses_turnstile():
    return captcha_is_enabled() and captcha_provider() == "turnstile"


def captcha_uses_recaptcha():
    return captcha_is_enabled() and captcha_provider() == "recaptcha"


def _answer_key(scope):
    return CAPTCHA_ANSWER_SESSION_KEY.format(scope=scope)


def _prompt_key(scope):
    return CAPTCHA_PROMPT_SESSION_KEY.format(scope=scope)


def _make_challenge():
    left = secrets.randbelow(8) + 2
    right = secrets.randbelow(8) + 1
    return f"What is {left} + {right}?", str(left + right)


def rotate_captcha_challenge(request, scope):
    if request is None or not hasattr(request, "session"):
        return "Complete the security check."
    prompt, answer = _make_challenge()
    request.session[_prompt_key(scope)] = prompt
    request.session[_answer_key(scope)] = answer
    request.session.modified = True
    return prompt


def get_or_create_captcha_challenge(request, scope):
    if not captcha_is_enabled():
        return ""
    if captcha_uses_turnstile() or captcha_uses_recaptcha():
        return "Complete the security check."
    if request is None or not hasattr(request, "session"):
        return "Complete the security check."
    prompt = request.session.get(_prompt_key(scope))
    answer = request.session.get(_answer_key(scope))
    if prompt and answer:
        return prompt
    return rotate_captcha_challenge(request, scope)


def validate_captcha_answer(request, scope, answer):
    if not captcha_is_enabled():
        return True
    if captcha_uses_turnstile():
        return validate_turnstile_token(request, answer)
    if captcha_uses_recaptcha():
        return validate_recaptcha_token(request, answer)
    expected = ""
    if request is not None and hasattr(request, "session"):
        expected = request.session.get(_answer_key(scope), "")
    supplied = (answer or "").strip()
    is_valid = bool(expected) and secrets.compare_digest(str(expected), supplied)
    rotate_captcha_challenge(request, scope)
    return is_valid


def _request_ip_address(request):
    if request is None:
        return ""
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _captcha_remote_ip(request):
    remote_ip = _request_ip_address(request)
    try:
        address = ipaddress.ip_address(remote_ip)
    except ValueError:
        return ""
    return remote_ip if address.is_global else ""


def _configured_turnstile_allowed_hosts():
    configured_hosts = getattr(settings, "TURNSTILE_VERIFY_ALLOWED_HOSTS", [])
    if isinstance(configured_hosts, str):
        configured_hosts = configured_hosts.split(",")
    return {host.strip().lower().rstrip(".") for host in configured_hosts if host.strip()}


def _validate_turnstile_verify_url(url):
    parsed = urllib.parse.urlsplit((url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Turnstile verification URL must use HTTPS with a valid host.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Turnstile verification URL must not include credentials, query, or fragment.")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname not in _configured_turnstile_allowed_hosts():
        raise ValueError("Turnstile verification URL host is not allowed.")

    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _post_json(url, payload, timeout):
    safe_url = _validate_turnstile_verify_url(url)
    request = urllib.request.Request(
        safe_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # The URL is validated as HTTPS and allowlist-matched before this call.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


def _post_form(url, payload, timeout):
    safe_url = _validate_turnstile_verify_url(url)
    request = urllib.request.Request(
        safe_url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        method="POST",
    )
    # The URL is validated as HTTPS and allowlist-matched before this call.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


def _post_recaptcha_form(payload, timeout):
    request = urllib.request.Request(
        RECAPTCHA_SITEVERIFY_URL,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        method="POST",
    )
    # This endpoint is a fixed HTTPS Google URL, not user-configurable input.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


def validate_turnstile_token(request, token):
    token = (token or "").strip()
    if not token:
        return False

    timeout = getattr(settings, "TURNSTILE_TIMEOUT_SECONDS", 5)
    worker_url = (getattr(settings, "TURNSTILE_VERIFY_URL", "") or "").strip().rstrip("/")
    secret_key = (getattr(settings, "TURNSTILE_SECRET_KEY", "") or "").strip()
    remote_ip = _captcha_remote_ip(request)

    try:
        if secret_key:
            payload = {
                "secret": secret_key,
                "response": token,
            }
            if remote_ip:
                payload["remoteip"] = remote_ip
            response = _post_form(
                TURNSTILE_SITEVERIFY_URL,
                payload,
                timeout,
            )
        elif worker_url:
            payload = {"token": token}
            if remote_ip:
                payload["remoteip"] = remote_ip
            response = _post_json(
                worker_url,
                payload,
                timeout,
            )
        else:
            return False
    except (OSError, TimeoutError, ValueError, urllib.error.URLError):
        return False

    return response.get("success") is True


def validate_recaptcha_token(request, token):
    token = (token or "").strip()
    if not token:
        return False

    secret_key = (getattr(settings, "RECAPTCHA_SECRET_KEY", "") or "").strip()
    if not secret_key:
        return False

    payload = {
        "secret": secret_key,
        "response": token,
    }
    remote_ip = _captcha_remote_ip(request)
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        response = _post_recaptcha_form(
            payload,
            getattr(settings, "RECAPTCHA_TIMEOUT_SECONDS", 5),
        )
    except (OSError, TimeoutError, ValueError, urllib.error.URLError):
        return False

    return response.get("success") is True
