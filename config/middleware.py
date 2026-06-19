import hashlib
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse


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
        cache_key = self._cache_key(request, rule_name, window)
        try:
            if cache.add(cache_key, 1, timeout=window_seconds + 2):
                count = 1
            else:
                count = cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, timeout=window_seconds + 2)
            count = 1

        remaining = max(0, limit - count)
        if count > limit:
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

    def _cache_key(self, request, rule_name, window):
        client_ip = self._client_ip(request)
        raw_key = f"{rule_name}:{client_ip}:{window}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return f"recruitguard:rate-limit:{digest}"

    def _client_ip(self, request):
        if getattr(settings, "RATE_LIMIT_TRUST_X_FORWARDED_FOR", False):
            forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
            if forwarded_for:
                return forwarded_for.split(",", 1)[0].strip()
        return request.META.get("REMOTE_ADDR", "") or "unknown"
