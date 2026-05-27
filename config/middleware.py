from django.conf import settings


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
