"""Round F3: the position references every vacancy is built on, maintained by the System
Administrator inside the portal (docs/admin-area-rebuild-plan.md section 4.7). Nothing is
deleted: a reference no longer in use is retired, so old vacancies keep it.
"""

from django.db import transaction
from django.db.models import Count

from ..models import AuditLog, PositionReference
from . import audit


@transaction.atomic
def save_position_reference(form, actor):
    """Save an add/edit form, derive the reference's completeness status from its fields, and
    record the change in the audit log. Returns the saved reference."""
    created = form.instance.pk is None
    reference = form.save(commit=False)
    reference.reference_status = reference.derive_reference_status()
    reference.save()
    changed = list(form.changed_data)
    if created:
        action = AuditLog.Action.POSITION_CREATED
        description = f"Added position reference '{reference.position_title}'."
    else:
        action = AuditLog.Action.POSITION_UPDATED
        if changed == ["is_active"]:
            verb = "Reactivated" if reference.is_active else "Retired"
            description = f"{verb} position reference '{reference.position_title}'."
        else:
            description = f"Updated position reference '{reference.position_title}'."
    audit.record_system_audit_event(
        actor=actor,
        action=action,
        description=description,
        metadata={
            "position_reference_id": reference.pk,
            "position_title": reference.position_title,
            "changed_fields": changed,
            "is_active": reference.is_active,
        },
    )
    return reference


def position_references_with_usage():
    """Every reference with how many vacancies use it, for the list page."""
    return PositionReference.objects.annotate(vacancy_count=Count("recruitment_entries"))
