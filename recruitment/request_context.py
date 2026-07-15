"""REV-F: per-request context so audit writes can stamp the session tag.

The tag is computed lazily at audit-write time (not when the middleware runs)
because Django rotates the session key during login — an entry written after
``login()`` must carry the NEW session's tag, and the login/logout audit
signals fire mid-request.
"""

import hashlib
from contextvars import ContextVar

_current_request = ContextVar("recruitguard_current_request", default=None)


class AuditRequestContextMiddleware:
    """Expose the active request to the audit service layer via a contextvar."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            _current_request.reset(token)


def current_session_tag():
    """Hashed tag of the active browser session, or "" outside a request.

    Never the raw session key: the tag only needs to be stable within one
    sign-in so audit entries group correctly; it must not be usable to
    hijack the session.
    """
    request = _current_request.get()
    if request is None:
        return ""
    session = getattr(request, "session", None)
    session_key = getattr(session, "session_key", None)
    if not session_key:
        return ""
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
