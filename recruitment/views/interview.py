"""The interview forms of a case: session, ratings, rating sheet, absence and paper ratings.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from ..forms import (
    InterviewFallbackUploadForm,
    InterviewRatingForm,
    CompetencyDefinitionFormSet,
    CompetencyRatingTemplateForm,
    InterviewSessionForm,
    SimpleInterviewRatingForm,
    InterviewAbsenceForm,
)
from ..models import (
    CompetencyDefinition,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentUser,
)
from ..permissions import WorkflowProcessorRequiredMixin
from ..services import (
    get_office_policy,
    interview_fallback_panel_queryset,
    mark_interview_absent,
    set_vacancy_panel_end_users,
    vacancy_panel_member_ids,
    get_interview_session,
    save_interview_rating,
    save_interview_session,
    vacancy_uses_batch_exam,
    upload_interview_fallback_rating,
    user_can_manage_interview_rating,
    create_competency_rating_template,
    get_competency_rating_template,
    get_published_competency_rating_template,
    save_competency_rating_sheet,
    user_can_manage_interview_session,
    user_can_upload_interview_fallback,
    user_can_view_application,
)
from .common import _autosave_response, _format_form_errors, _is_autosave_request


class InterviewSessionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_session(request.user, application):
            raise PermissionDenied
        # RD-4: one door. A pooled vacancy's interview session is scheduled and
        # finalized once for the whole pool in the batch interview console, so
        # this per-case endpoint refuses writes server-side (PT-03) — a forced
        # POST cannot break the pool's lock-step or send a conflicting notice.
        if vacancy_uses_batch_exam(application.position):
            messages.error(
                request,
                "This vacancy's interview is scheduled and finalized for the whole "
                "pool in the batch interview console.",
            )
            return redirect("vacancy-batch-interview", pk=application.position.pk)

        operation = request.POST.get("operation", "save")
        is_autosave = _is_autosave_request(request) and operation != "finalize"
        notify_applicant = operation == "notify_applicant"
        notify_panel = operation == "notify_panel"
        form = InterviewSessionForm(
            request.POST,
            instance=get_interview_session(application),
        )
        if form.is_valid():
            try:
                interview_session = save_interview_session(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=operation == "finalize",
                    notify_applicant=notify_applicant,
                    notify_panel=notify_panel,
                )
            except (ValueError, ValidationError) as exc:
                if is_autosave:
                    return _autosave_response(False)
                messages.error(request, str(exc))
            else:
                if is_autosave:
                    return _autosave_response()
                if interview_session.is_finalized:
                    messages.success(request, "Interview session finalized and locked.")
                elif notify_applicant:
                    messages.success(
                        request, "Interview schedule saved and the applicant was notified."
                    )
                elif notify_panel:
                    messages.success(
                        request, "Interview schedule saved and the HRMPSB panel was notified."
                    )
                else:
                    messages.success(request, "Interview schedule saved.")
        else:
            if is_autosave:
                return _autosave_response(False)
            messages.error(
                request,
                _format_form_errors(
                    form,
                    "Complete the required interview scheduling fields before saving.",
                ),
            )
        return redirect("application-detail", pk=pk)


class InterviewRatingSheetView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    template_name = "recruitment/interview_rating_sheet.html"

    def _load_application(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_session(request.user, application):
            raise PermissionDenied
        return application

    def _context(self, application, template, template_form=None, formset=None):
        entry = application.position
        core_ids = set(get_office_policy().core_panel_members.values_list("pk", flat=True))
        members = RecruitmentUser.objects.filter(
            is_active=True, role=RecruitmentUser.Role.HRMPSB_MEMBER
        ).order_by("last_name", "first_name", "username")
        chosen = set(entry.panel_end_users.values_list("pk", flat=True))
        return {
            "application": application,
            "entry": entry,
            "rating_sheet": template,
            "template_form": template_form,
            "competency_formset": formset,
            # 4a: who sits on this vacancy's panel.
            "core_panel": [member for member in members if member.pk in core_ids],
            "end_user_choices": [
                {"user": member, "checked": member.pk in chosen}
                for member in members
                if member.pk not in core_ids
            ],
            "panel_is_everyone": vacancy_panel_member_ids(entry) is None,
        }

    def get(self, request, pk):
        application = self._load_application(request, pk)
        template = get_competency_rating_template(application.position)
        template_form = formset = None
        if template:
            template_form = CompetencyRatingTemplateForm(instance=template)
            formset = CompetencyDefinitionFormSet(instance=template)
        return render(
            request,
            self.template_name,
            self._context(application, template, template_form, formset),
        )

    def post(self, request, pk):
        application = self._load_application(request, pk)
        entry = application.position
        template = get_competency_rating_template(entry)
        operation = request.POST.get("operation", "save")

        if operation == "save_panel":
            try:
                set_vacancy_panel_end_users(
                    application.position,
                    request.user,
                    request.POST.getlist("panel_end_users"),
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Panel saved for this vacancy.")
            return redirect("interview-rating-sheet", pk=pk)
        if operation == "create":
            if template:
                messages.info(request, "This vacancy already has an interview rating sheet.")
            else:
                try:
                    create_competency_rating_template(entry, request.user)
                    messages.success(
                        request,
                        "Rating sheet created from the standard template. "
                        "Add the Technical competencies below.",
                    )
                except (ValueError, ValidationError) as exc:
                    messages.error(request, str(exc))
            return redirect("interview-rating-sheet", pk=pk)

        if not template:
            messages.error(request, "Create the interview rating sheet first.")
            return redirect("interview-rating-sheet", pk=pk)
        if template.is_locked:
            messages.error(request, "This rating sheet is locked because scoring has started.")
            return redirect("interview-rating-sheet", pk=pk)

        template_form = CompetencyRatingTemplateForm(request.POST, instance=template)
        formset = CompetencyDefinitionFormSet(request.POST, instance=template)
        if template_form.is_valid() and formset.is_valid():
            try:
                save_competency_rating_sheet(
                    template, template_form, formset, publish=operation == "publish"
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                if operation == "publish":
                    messages.success(
                        request,
                        "Rating sheet saved and made available to the HRMPSB panel.",
                    )
                else:
                    messages.success(request, "Rating sheet saved.")
                return redirect("interview-rating-sheet", pk=pk)
        else:
            messages.error(request, "Fix the highlighted problems on the rating sheet.")
        return render(
            request,
            self.template_name,
            self._context(application, template, template_form, formset),
        )


class InterviewRatingSheetPrintView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """REV-E: print-tailored competency rating sheet for the live interview.
    Shows candidate code AND name — the REV-B face-to-face exception: correct
    attribution outranks symbolic blinding in the room."""

    def get(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_view_application(request.user, application):
            raise PermissionDenied
        rating_template = get_published_competency_rating_template(application.position)
        competencies = list(rating_template.competencies.all()) if rating_template else []
        # Mirror the office form (DOH-RO4A-MPERIF): competencies presented in
        # CORE / ORGANIZATIONAL / TECHNICAL sections with a total-score row.
        grouped = []
        for group_value, group_label in CompetencyDefinition.Group.choices:
            members = [c for c in competencies if c.group == group_value]
            if members:
                grouped.append({"label": group_label, "competencies": members})
        total_weight = sum((c.weight or 0) for c in competencies)
        return render(
            request,
            "recruitment/interview_rating_sheet_print.html",
            {
                "application": application,
                "entry": application.position,
                "rating_template": rating_template,
                "competency_groups": grouped,
                "competency_total_weight": total_weight,
                "interview_session": get_interview_session(application),
                "printed_by": request.user,
                "printed_at": timezone.now(),
            },
        )


class InterviewRatingView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_rating(request.user, application):
            raise PermissionDenied

        # RD-4: a pooled COS vacancy's single interview score is recorded in the
        # batch console, so this per-case endpoint refuses it server-side. The
        # Plantilla per-rater competency sheet stays per-case (each rater's own
        # sheet — the console cannot host N raters' parallel entries), and a
        # continuous-COS vacancy keeps its per-case simple score.
        if (
            application.branch == PositionPosting.Branch.COS
            and vacancy_uses_batch_exam(application.position)
        ):
            messages.error(
                request,
                "This vacancy's interview scores are recorded in the batch "
                "interview console, where the whole pool is scored together.",
            )
            return redirect("vacancy-batch-interview", pk=application.position.pk)

        if application.branch == PositionPosting.Branch.COS:
            # COS interviews use a simple end-user / HRMS score, not the sheet (spec §6.2).
            form = SimpleInterviewRatingForm(request.POST)
        else:
            rating_template = get_published_competency_rating_template(application.position)
            if rating_template is None:
                messages.error(
                    request,
                    "Publish the interview rating sheet for this vacancy before recording ratings.",
                )
                return redirect("application-detail", pk=pk)

            form = InterviewRatingForm(
                request.POST,
                application=application,
                actor=request.user,
                template=rating_template,
            )
        if form.is_valid():
            try:
                save_interview_rating(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Interview rating saved.")
        else:
            # F8: name what is missing (often the justification a 75-or-below / above-98
            # rating needs) instead of one generic line.
            problems = [error for field_errors in form.errors.values() for error in field_errors]
            messages.error(request, "The rating wasn't saved: " + " ".join(problems))
        return redirect("application-detail", pk=pk)


class InterviewAbsenceView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """SL-06: record that the candidate did not attend their scheduled interview."""

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_session(request.user, application):
            raise PermissionDenied
        form = InterviewAbsenceForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Record what happened at the scheduled interview.")
            return redirect("application-detail", pk=pk)
        try:
            mark_interview_absent(
                application=application,
                actor=request.user,
                notes=form.cleaned_data["notes"],
            )
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(
                request,
                "Absence recorded. No score was written — file the applicant's letter of "
                "intent if one arrives, then decide whether to reschedule.",
            )
        return redirect("application-detail", pk=pk)


class InterviewFallbackUploadView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_upload_interview_fallback(request.user, application):
            raise PermissionDenied

        form = InterviewFallbackUploadForm(
            request.POST,
            request.FILES,
            panel_members=interview_fallback_panel_queryset(application),
        )
        if form.is_valid():
            try:
                upload_interview_fallback_rating(
                    application=application,
                    actor=request.user,
                    uploaded_file=form.cleaned_data["file"],
                    remarks=form.cleaned_data["remarks"],
                    rated_by=form.cleaned_data["rated_by"],
                    rating_score=form.cleaned_data["rating_score"],
                    justification=form.cleaned_data.get("justification", ""),
                    replace_existing=form.cleaned_data.get("replace_existing", False),
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                member = form.cleaned_data["rated_by"]
                messages.success(
                    request,
                    f"Paper rating sheet saved for {member.get_full_name() or member.username} "
                    f"({form.cleaned_data['rating_score']}). The scan is kept with the case files.",
                )
        else:
            # Say exactly what is wrong (a missing justification, a score over 100, ...)
            # instead of one generic line.
            problems = [
                error
                for field_errors in form.errors.values()
                for error in field_errors
            ]
            messages.error(request, "The rating sheet wasn't saved: " + " ".join(problems))
        return redirect("application-detail", pk=pk)
