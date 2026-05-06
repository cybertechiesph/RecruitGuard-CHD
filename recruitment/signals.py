from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.backends.signals import connection_created
from django.dispatch import receiver

from .models import AuditLog
from .services import record_system_audit_event


@receiver(connection_created, dispatch_uid="recruitguard_sqlite_connection_pragmas")
def configure_sqlite_connection(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return

    database_name = str(connection.settings_dict.get("NAME", ""))
    with connection.cursor() as cursor:
        cursor.execute(f"PRAGMA busy_timeout = {int(settings.SQLITE_BUSY_TIMEOUT_MS)}")
        cursor.execute("PRAGMA foreign_keys = ON")
        if database_name and database_name != ":memory:" and not database_name.startswith("file:memorydb_"):
            cursor.execute("PRAGMA journal_mode = WAL")


@receiver(user_logged_in, dispatch_uid="recruitguard_internal_login_audit")
def audit_internal_login(sender, request, user, **kwargs):
    if getattr(user, "is_internal_user", False):
        record_system_audit_event(
            actor=user,
            action=AuditLog.Action.INTERNAL_LOGIN,
            description="Internal user logged in.",
            metadata={"user_id": user.id},
        )


@receiver(user_logged_out, dispatch_uid="recruitguard_internal_logout_audit")
def audit_internal_logout(sender, request, user, **kwargs):
    if getattr(user, "is_internal_user", False):
        record_system_audit_event(
            actor=user,
            action=AuditLog.Action.INTERNAL_LOGOUT,
            description="Internal user logged out.",
            metadata={"user_id": user.id},
        )
