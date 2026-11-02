import hashlib
import logging
import time

from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.http import HttpResponse
from django.middleware.locale import LocaleMiddleware
from django.utils import translation

from config.client_ip import get_client_ip

logger = logging.getLogger(__name__)

PASSIVE_SESSION_REQUEST_ATTR = "_rg_passive_session_request"


def mark_passive_session_request(request):
    """Flag a request as background traffic (e.g. the notification-bell poll) so it does not
    count as user activity: the session is read as usual but its expiry is not pushed back."""
    setattr(request, PASSIVE_SESSION_REQUEST_ATTR, True)


def _serialize_policy(directives):
    parts = []
    for name, values in directives.items():
        values = tuple(values)
        if values:
            parts.append(f"{name} {' '.join(values)}")
        else:
            parts.append(name)
    return "; ".join(parts)


def _serialize_permissions_policy(directives):
    parts = []
    for feature, allowlist in directives.items():
        allowed = " ".join(allowlist)
        parts.append(f"{feature}=({allowed})")
    return ", ".join(parts)


class ActivityAwareSessionMiddleware(SessionMiddleware):
    """SessionMiddleware that skips the SESSION_SAVE_EVERY_REQUEST refresh for passive requests.

    With SESSION_SAVE_EVERY_REQUEST, every request re-saves the session and so restarts the
    inactivity timeout. The notification bell polls once a minute, which used to keep an
    unattended tab signed in indefinitely. Passive requests that did not change the session
    now leave its expiry alone; anything that modifies the session is still saved normally."""

    def process_response(self, request, response):
        session = getattr(request, "session", None)
        if (
            getattr(request, PASSIVE_SESSION_REQUEST_ATTR, False)
            and session is not None
            and not session.modified
        ):
            return response
        return super().process_response(request, response)


class ApplicantPortalLocaleMiddleware(LocaleMiddleware):
    """Language switching for the applicant portal only.

    On /apply/ pages the language comes from the applicant's choice (the language cookie)
    or their browser, as with Django's LocaleMiddleware. Everywhere else (the internal
    staff portal, admin) English is always used, since only the applicant pages are
    translated."""

    APPLICANT_PREFIX = "/apply/"

    def process_request(self, request):
        if (request.path or "").startswith(self.APPLICANT_PREFIX):
            return super().process_request(request)
        translation.activate(settings.LANGUAGE_CODE)
        request.LANGUAGE_CODE = translation.get_language()
        return None

    def process_response(self, request, response):
        if (request.path or "").startswith(self.APPLICANT_PREFIX):
            return super().process_response(request, response)
        return response


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.csp_header_value = _serialize_policy(settings.CSP_DIRECTIVES)
        self.permissions_policy_value = _serialize_permissions_policy(
            settings.PERMISSIONS_POLICY
        )

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.csp_header_value)
        response.setdefault("Permissions-Policy", self.permissions_policy_value)
        response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rule_name = self._select_rule(request)
        if not getattr(settings, "RATE_LIMIT_ENABLED", True) or rule_name is None:
            return self.get_response(request)

        rule = getattr(settings, "RATE_LIMIT_RULES", {}).get(rule_name)
        if rule is None:
            rule = getattr(settings, "RATE_LIMIT_RULES", {}).get("default", {})

        limit = int(rule.get("limit", 0))
        window_seconds = int(rule.get("window", 60))
        if limit <= 0 or window_seconds <= 0:
            return self.get_response(request)

        now = int(time.time())
        window = now // window_seconds
        client_ip = get_client_ip(request) or "unknown"
        # Each browser gets the rule's limit; the IP as a whole gets a much higher ceiling.
        # Philippine mobile networks put many people behind one shared IP (carrier-grade
        # NAT), so a per-IP-only limit lets applicants on the same network lock each other
        # out on a busy closing day. The ceiling still stops a flood from one address,
        # including one that keeps inventing new cookies.
        browser_count = self._hit(self._cache_key(rule_name, client_ip, self._browser_id(request), window), window_seconds)
        ip_limit = limit * max(1, int(getattr(settings, "RATE_LIMIT_SHARED_IP_FACTOR", 10)))
        ip_count = self._hit(self._cache_key(rule_name, client_ip, "*", window), window_seconds)

        remaining = max(0, limit - browser_count)
        if browser_count > limit or ip_count > ip_limit:
            response = HttpResponse(
                "Too many requests. Please try again shortly.",
                status=429,
                content_type="text/plain",
            )
            response["Retry-After"] = str(max(1, window_seconds - (now % window_seconds)))
            response["X-RateLimit-Limit"] = str(limit)
            response["X-RateLimit-Remaining"] = "0"
            return response

        response = self.get_response(request)
        response.setdefault("X-RateLimit-Limit", str(limit))
        response.setdefault("X-RateLimit-Remaining", str(remaining))
        return response

    def _select_rule(self, request):
        path = request.path or ""
        if path.startswith(getattr(settings, "STATIC_URL", "/static/")):
            return None
        if path.startswith(getattr(settings, "MEDIA_URL", "/media/")):
            return None

        method = request.method.upper()
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            if path.startswith("/internal/login") or "password" in path:
                return "auth"
            if "otp" in path or "mfa" in path:
                return "otp"
            if "upload" in path or "evidence" in path:
                return "upload"
        return "default"

    @staticmethod
    def _hit(cache_key, window_seconds):
        try:
            if cache.add(cache_key, 1, timeout=window_seconds + 2):
                return 1
            return cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, timeout=window_seconds + 2)
            return 1

    @staticmethod
    def _browser_id(request):
        """The session or CSRF cookie identifies one browser behind a shared IP. A request
        with neither (a first visit, or a script) counts as the IP's anonymous browser."""
        cookies = getattr(request, "COOKIES", {}) or {}
        token = cookies.get(settings.SESSION_COOKIE_NAME) or cookies.get(settings.CSRF_COOKIE_NAME) or ""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else "anon"

    @staticmethod
    def _cache_key(rule_name, client_ip, browser_id, window):
        raw_key = f"{rule_name}:{client_ip}:{browser_id}:{window}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return f"recruitguard:rate-limit:{digest}"


class WorkflowConflictMiddleware:
    """A database conflict on an internal form post (two people saving the same step at
    the same moment) becomes a plain message and a fresh page instead of an error page.
    The transaction has already rolled back, so nothing half-saved remains."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.contrib import messages
        from django.db import IntegrityError
        from django.http import HttpResponseRedirect
        from django.utils.http import url_has_allowed_host_and_scheme
        from urllib.parse import urlsplit

        if not isinstance(exception, IntegrityError):
            return None
        if request.method != "POST" or not request.path.startswith("/internal/"):
            return None
        logger.warning("Workflow conflict on %s: %s", request.path, exception)
        messages.warning(
            request,
            "Someone else saved this step at the same moment, so your change was not "
            "applied. The page now shows the latest version.",
        )
        referer = request.META.get("HTTP_REFERER", "")
        if referer and url_has_allowed_host_and_scheme(
            referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            parts = urlsplit(referer)
            target = parts.path + (f"?{parts.query}" if parts.query else "")
        else:
            target = "/internal/"
        return HttpResponseRedirect(target)

