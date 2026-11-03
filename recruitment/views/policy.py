"""Vacancy assessment weights and the Office Policy page.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from ..forms import (
    OfficePolicyForm,
    VacancyAssessmentWeightsForm,
    VacancyAssessmentWeightsUnlockForm,
)
from ..models import PositionPosting, RecruitmentUser
from ..permissions import EntryManagerRequiredMixin, InternalUserRequiredMixin
from ..services import (
    get_office_policy,
    update_office_policy,
    get_or_create_vacancy_assessment_weights,
    unlock_vacancy_assessment_weights,
    update_vacancy_assessment_weights,
    vacancy_assessment_weights_unlock_block_message,
)


class VacancyAssessmentWeightsView(LoginRequiredMixin, EntryManagerRequiredMixin, View):
    template_name = "recruitment/vacancy_assessment_weights.html"

    def _get_entry(self, pk):
        # RD-5: both branches use the weights now — post-RD-2 the COS exam
        # overall is computed from General/Technical exactly like Plantilla,
        # so COS entries edit their weights here too.
        return get_object_or_404(PositionPosting, pk=pk)

    def _context(self, entry, weights, form, unlock_form=None):
        return {
            "entry": entry,
            "weights": weights,
            "form": form,
            "is_locked": weights.is_locked,
            "unlock_block_message": vacancy_assessment_weights_unlock_block_message(entry),
            "unlock_form": unlock_form or VacancyAssessmentWeightsUnlockForm(),
        }

    def get(self, request, pk):
        entry = self._get_entry(pk)
        weights = get_or_create_vacancy_assessment_weights(entry)
        form = VacancyAssessmentWeightsForm(instance=weights)
        return render(request, self.template_name, self._context(entry, weights, form))

    def _unlock(self, request, entry, weights):
        """SL-00: reopen a locked split for correction, with a mandatory audited reason."""
        unlock_form = VacancyAssessmentWeightsUnlockForm(request.POST)
        if unlock_form.is_valid():
            try:
                unlock_vacancy_assessment_weights(
                    entry,
                    request.user,
                    unlock_form.cleaned_data["reason"],
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(
                    request,
                    "Assessment weights unlocked for correction. The reason is recorded.",
                )
                return redirect("vacancy-assessment-weights", pk=entry.pk)
        else:
            messages.error(request, "Record why the assessment weights are being unlocked.")
        form = VacancyAssessmentWeightsForm(instance=weights)
        return render(
            request,
            self.template_name,
            self._context(entry, weights, form, unlock_form=unlock_form),
        )

    def post(self, request, pk):
        entry = self._get_entry(pk)
        weights = get_or_create_vacancy_assessment_weights(entry)
        if request.POST.get("operation") == "unlock":
            return self._unlock(request, entry, weights)
        if weights.is_locked:
            messages.error(
                request,
                "Assessment weights are locked because scoring has started for this vacancy. "
                "Unlock them for correction if the split was entered wrongly.",
            )
            return redirect("vacancy-assessment-weights", pk=entry.pk)
        form = VacancyAssessmentWeightsForm(request.POST, instance=weights)
        if form.is_valid():
            try:
                update_vacancy_assessment_weights(entry, form, request.user)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Assessment weights updated.")
                return redirect("vacancy-assessment-weights", pk=entry.pk)
        else:
            messages.error(request, "Fix the highlighted problems before saving.")
        return render(request, self.template_name, self._context(entry, weights, form))


class OfficePolicyView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    template_name = "recruitment/office_policy.html"

    def _require_owner(self, request):
        if request.user.role != RecruitmentUser.Role.HRM_CHIEF:
            raise PermissionDenied

    def get(self, request):
        self._require_owner(request)
        policy = get_office_policy()
        return render(
            request,
            self.template_name,
            {"policy": policy, "form": OfficePolicyForm(instance=policy)},
        )

    def post(self, request):
        self._require_owner(request)
        policy = get_office_policy()
        form = OfficePolicyForm(request.POST, instance=policy)
        if form.is_valid():
            try:
                _policy, changes = update_office_policy(form, request.user)
            except (ValueError, ValidationError) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                if changes:
                    changed_labels = ", ".join(
                        form.fields[field].label for field in changes
                    )
                    messages.success(
                        request,
                        f"Office Policy updated: {changed_labels}.",
                    )
                else:
                    messages.info(request, "No Office Policy values changed.")
                return redirect("office-policy")
        else:
            messages.error(request, "Fix the highlighted problems before saving.")
        return render(request, self.template_name, {"policy": policy, "form": form})
