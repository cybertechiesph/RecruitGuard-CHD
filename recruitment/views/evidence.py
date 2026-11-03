"""The evidence vault pages and the case export bundle.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import ListView

from ..forms import EvidenceArchiveForm, EvidenceUploadForm
from ..models import EvidenceVaultItem, RecruitmentApplication
from ..permissions import InternalUserRequiredMixin, SystemAdministratorRequiredMixin
from ..services import (
    build_export_bundle,
    decrypt_evidence_bytes,
    evidence_belongs_to_application_context,
    get_evidence_owner_application,
    get_evidence_queryset_for_user,
    record_evidence_access_denied,
    record_export_denied,
    record_evidence_vault_access,
    user_can_manage_evidence_archive,
    upload_evidence_item,
    update_evidence_archive_status,
    user_can_export_application,
    user_can_upload_evidence,
)
from .common import _safe_next_url


class EvidenceUploadView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_upload_evidence(request.user, application):
            raise PermissionDenied
        form = EvidenceUploadForm(request.POST, request.FILES)
        if form.is_valid():
            evidence = upload_evidence_item(
                application=application,
                actor=request.user,
                label=form.cleaned_data["label"],
                uploaded_file=form.cleaned_data["file"],
                artifact_scope=(
                    EvidenceVaultItem.OwnerScope.CASE
                    if hasattr(application, "case")
                    else EvidenceVaultItem.OwnerScope.APPLICATION
                ),
                artifact_type="workflow_evidence",
            )
            messages.success(
                request,
                f"File saved as {evidence.version_label}.",
            )
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect("evidence-vault-list")


class EvidenceDownloadView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request, pk, evidence_pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        evidence = get_object_or_404(
            EvidenceVaultItem.objects.select_related(
                "application",
                "recruitment_case",
                "recruitment_case__application",
                "recruitment_entry",
            ),
            pk=evidence_pk,
        )
        if not evidence_belongs_to_application_context(evidence, application):
            record_evidence_access_denied(
                evidence,
                request.user,
                application=application,
                reason="context_mismatch",
            )
            raise Http404
        try:
            content = decrypt_evidence_bytes(evidence, request.user, application=application)
        except ValueError as exc:
            raise PermissionDenied(str(exc))
        response = HttpResponse(
            content,
            content_type=evidence.content_type or "application/octet-stream",
        )
        # Inline viewing is only honoured for a safe allowlist of static media
        # types (images and PDF). Every other type stays an attachment so an
        # uploaded file cannot execute active content in the RecruitGuard origin.
        serve_inline = (
            request.GET.get("disposition") == "inline" and evidence.is_inline_viewable
        )
        disposition = "inline" if serve_inline else "attachment"
        response["Content-Disposition"] = (
            f'{disposition}; filename="{evidence.original_filename}"'
        )
        response["X-Content-Type-Options"] = "nosniff"
        if serve_inline:
            # Defense in depth: tighten the global policy so the inline file
            # cannot run scripts, submit forms, or be framed even if a file
            # slipped past the allowlist. Same-origin loads stay allowed so the
            # browser can still render the image/PDF.
            response["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'none'; object-src 'none'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
        return response


class EvidenceArchiveToggleView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def post(self, request, pk, evidence_pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_evidence_archive(request.user, application):
            raise PermissionDenied

        evidence = get_object_or_404(
            EvidenceVaultItem.objects.select_related(
                "application",
                "recruitment_case",
                "recruitment_case__application",
                "recruitment_entry",
            ),
            pk=evidence_pk,
        )
        if not evidence_belongs_to_application_context(evidence, application):
            raise Http404
        form = EvidenceArchiveForm(request.POST)
        if form.is_valid():
            try:
                update_evidence_archive_status(
                    evidence=evidence,
                    actor=request.user,
                    action=form.cleaned_data["action"],
                    archive_tag=form.cleaned_data["archive_tag"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                if form.cleaned_data["action"] == "archive":
                    messages.success(request, "File archived with its archive label.")
                else:
                    messages.success(request, "File restored from archive.")
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect(_safe_next_url(request, reverse("evidence-vault-list")))


class EvidenceVaultListView(LoginRequiredMixin, SystemAdministratorRequiredMixin, ListView):
    """Round F3: Secured Files — one toolbar and one row per file. Metadata only: the owning
    case is shown for labelling, the file content is never offered
    (docs/admin-area-rebuild-plan.md section 4.6)."""

    template_name = "recruitment/admin/secured_files.html"
    context_object_name = "evidence_items"
    paginate_by = 50
    ARCHIVAL_CHOICES = ("active", "archived", "all")

    def _filters(self):
        get = self.request.GET
        stage = get.get("stage") or ""
        scope = get.get("artifact_scope") or ""
        archival = get.get("archival_status") or "active"
        return {
            "q": (get.get("q") or "").strip(),
            "stage": stage if stage in EvidenceVaultItem.Stage.values else "",
            "artifact_scope": scope if scope in EvidenceVaultItem.OwnerScope.values else "",
            "archival_status": archival if archival in self.ARCHIVAL_CHOICES else "active",
            # A toggle, not a checkbox: an unticked box in a GET form looks like a first visit.
            "current_version_only": get.get("versions") != "all",
        }

    def get_queryset(self):
        self.search_filters = self._filters()
        filters = self.search_filters
        return get_evidence_queryset_for_user(
            self.request.user,
            search_query=filters["q"],
            stage=filters["stage"],
            artifact_scope=filters["artifact_scope"],
            archival_status=filters["archival_status"],
            current_version_only=filters["current_version_only"],
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        evidence_items = list(context["evidence_items"])
        for evidence in evidence_items:
            # Oversight is metadata-only: resolve the owning case for labelling,
            # but never authorize the admin to download the file content.
            evidence.owner_application = get_evidence_owner_application(evidence)
        filters = self.search_filters
        context.update(
            {
                "evidence_items": evidence_items,
                "filters": filters,
                "result_count": context["paginator"].count if context.get("paginator") else len(evidence_items),
                "stage_chips": EvidenceVaultItem.Stage.choices,
                "scope_chips": EvidenceVaultItem.OwnerScope.choices,
                "is_filtered": bool(
                    filters["q"]
                    or filters["stage"]
                    or filters["artifact_scope"]
                    or filters["archival_status"] != "active"
                    or not filters["current_version_only"]
                ),
            }
        )
        record_evidence_vault_access(
            self.request.user,
            search_query=filters["q"],
            stage=filters["stage"],
            artifact_scope=filters["artifact_scope"],
            archival_status=filters["archival_status"],
            current_version_only=filters["current_version_only"],
        )
        return context


class ExportApplicationBundleView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_export_application(request.user, application):
            record_export_denied(application, request.user, reason="unauthorized")
            raise PermissionDenied
        bundle = build_export_bundle(application, request.user)
        response = HttpResponse(bundle, content_type="application/zip")
        response["Content-Disposition"] = (
            f'attachment; filename="{application.reference_number}-export.zip"'
        )
        return response
