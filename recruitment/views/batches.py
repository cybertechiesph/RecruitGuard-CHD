"""Vacancy batches: the batch console, a vacancy's batch page, the batch exam and interview,
the COS deliberation pick, the rating sheet and the batch handoff.
"""

from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from ..schedule_services import get_schedule_events_for_user, group_events_by_day
from ..forms import ExamScheduleForm, InterviewSessionForm
from ..models import (
    AttendanceException,
    ExamRecord,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentUser,
)
from ..permissions import WorkflowProcessorRequiredMixin
from ..services import (
    close_cos_selection_round_short,
    vacancy_batch_pool_block_message,
    get_attendance_exceptions,
    get_cos_vacancy_deliberation,
    mark_interview_absent,
    get_latest_cos_selection_round,
    get_open_cos_selection_round,
    open_cos_selection_round,
    user_can_open_cos_selection_round,
    user_can_close_cos_selection_round_short,
    vacancy_uses_cos_selection_rounds,
    get_followed_applications,
    get_loaned_out_applications,
    hand_off_vacancy_batch,
    recall_vacancy_batch,
    user_can_send_vacancy_batch,
    cos_eligible_pool,
    cos_deliberation_pool,
    save_cos_vacancy_deliberation,
    user_can_manage_cos_deliberation,
    get_exam_record,
    get_notified_exam_sitting,
    get_exam_records,
    get_interview_rating_for_user,
    get_interview_ratings,
    get_interview_session,
    get_screening_record,
    get_closed_cases_for_user,
    get_queue_for_user,
    vacancy_uses_batch_exam,
    get_published_competency_rating_template,
    user_can_manage_interview_session,
    vacancy_exam_pool,
    save_vacancy_exam_schedule,
    save_vacancy_exam_scores,
    vacancy_interview_pool,
    save_vacancy_interview_schedule,
    save_vacancy_cos_interview_ratings,
    finalize_vacancy_interview_batch,
    vacancy_screening_batch_status,
    vacancy_screening_batch_block_message,
    user_can_view_application,
)
from .common import _format_form_errors


class VacancyBatchConsoleView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, TemplateView):
    """Read-only vacancy-grouped view of the user's own queue. Groups the same
    FRS-scoped applications get_queue_for_user returns (so the Level-1 -> Secretariat /
    Level-2 -> HRM Chief routing and the support-role/override carve-outs are preserved)
    by vacancy, with a per-vacancy "awaiting your action" count for triage."""

    template_name = "recruitment/vacancy_batches.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        groups = {}
        for application in get_queue_for_user(self.request.user):
            group = groups.setdefault(
                application.position_id,
                {"entry": application.position, "applications": []},
            )
            group["applications"].append(application)
        vacancies = sorted(
            groups.values(),
            key=lambda g: (g["entry"].get_branch_display(), (g["entry"].title or "").lower()),
        )
        for group in vacancies:
            group["count"] = len(group["applications"])
            group["following"] = False
        # C2: Level 2 vacancies the HRM Chief follows while others hold the cases.
        followed = {}
        for application in get_followed_applications(self.request.user):
            if application.position_id in groups:
                continue
            group = followed.setdefault(
                application.position_id,
                {"entry": application.position, "applications": [], "following": True},
            )
            group["applications"].append(application)
        for group in followed.values():
            group["count"] = len(group["applications"])
        context["vacancies"] = vacancies + sorted(
            followed.values(), key=lambda g: (g["entry"].title or "").lower()
        )
        context["total_applications"] = sum(group["count"] for group in vacancies)
        # TI-5: the next seven days from the schedule calendar, on the landing page.
        today = timezone.localdate()
        context["coming_up"] = group_events_by_day(
            get_schedule_events_for_user(self.request.user, today, today + timedelta(days=6))
        )
        return context


class VacancyBatchDetailView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, TemplateView):
    """The applicants of a single vacancy that are in the current user's scoped queue,
    each linking to the existing per-case detail. 404s when the user has no scoped
    applications for the vacancy AND no closed-case access to it, so a Secretariat never
    sees a Level-2 batch; a user whose own cases simply closed gets a note instead."""

    template_name = "recruitment/vacancy_batch_detail.html"

    def get(self, request, *args, **kwargs):
        pk = self.kwargs["pk"]
        if get_queue_for_user(request.user).filter(position_id=pk).exists():
            return super().get(request, *args, **kwargs)
        if get_followed_applications(request.user).filter(position_id=pk).exists():
            # C2: the HRM Chief follows a Level 2 vacancy through the CAR and decision.
            return super().get(request, *args, **kwargs)
        if get_loaned_out_applications(request.user).filter(position_id=pk).exists():
            # C4: the office that lent the batch follows it and can recall it.
            return super().get(request, *args, **kwargs)
        # Nothing queued here, for one of two very different reasons: the vacancy's cases are
        # all finished (this user worked them through to closure), or the vacancy was never
        # theirs (FRS scoping — a Secretariat must not see a Level-2 batch). Only the first
        # earns a friendly landing; the second keeps the 404 so a stale or guessed URL can
        # never confirm that another role's batch exists. get_closed_cases_for_user mirrors
        # the closed-access model exactly, so this cannot widen what the user may see.
        if get_closed_cases_for_user(request.user).filter(position_id=pk).exists():
            messages.info(
                request,
                "Every case you handled for this vacancy is closed, so its batch page is no "
                "longer active. You can still look them up under Closed cases.",
            )
            return redirect("vacancy-batches")
        raise Http404("No batch is available for this vacancy.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pk = self.kwargs["pk"]
        applications = [
            application
            for application in get_queue_for_user(self.request.user)
            if application.position_id == pk
        ]
        context["following_only"] = False
        loaned = list(get_loaned_out_applications(self.request.user).filter(position_id=pk))
        context["loaned_out"] = loaned
        if loaned:
            context["loaned_to_label"] = (
                "HRM Chief" if self.request.user.role == RecruitmentUser.Role.SECRETARIAT else "Secretariat"
            )
        context["on_loan_only"] = False
        if not applications and loaned:
            applications = loaned
            context["on_loan_only"] = True
        if not applications:
            applications = list(get_followed_applications(self.request.user).filter(position_id=pk))
            context["following_only"] = bool(applications)
        if not applications:
            # Defensive net — get() already resolved the empty-queue case above.
            raise Http404("No batch is available for this vacancy.")
        entry = applications[0].position
        context["entry"] = entry
        context["applications"] = applications
        context["can_send_batch"] = user_can_send_vacancy_batch(self.request.user, entry)
        context["send_target_label"] = (
            "HRM Chief" if self.request.user.role == RecruitmentUser.Role.SECRETARIAT else "Secretariat"
        )
        context["screening_batch"] = vacancy_screening_batch_status(entry)
        # SL-09: the batch waits for a final applicant pool — say when it unlocks rather
        # than offering a link that bounces straight back.
        context["batch_pool_block_message"] = vacancy_batch_pool_block_message(entry)
        context["exam_batch_ready"] = any(
            user_can_view_application(self.request.user, application)
            for application in vacancy_exam_pool(entry)
        )
        context["interview_batch_ready"] = any(
            user_can_view_application(self.request.user, application)
            for application in vacancy_interview_pool(entry)
        )
        context["cos_deliberation_ready"] = entry.branch == PositionPosting.Branch.COS and any(
            user_can_view_application(self.request.user, application)
            for application in cos_deliberation_pool(entry)
        )
        return context


def _batch_pool_empty_redirect(request, entry, message):
    """A batch exam/interview console whose scoped pool is empty used to ``raise Http404``,
    which surfaced as a bare "Page Not Found" right after finalizing: the finalized cases
    leave that stage's pool, so a refresh, Back-button, or re-run of the same URL hit an
    empty console. Send the reviewer back to the batch overview with a friendly note instead
    of crashing.

    An empty scoped pool has two causes, and only one is friendly: (a) the stage finished and
    its cases moved on, or (b) the user has no business on this vacancy's batch at all (wrong
    role — FRS scoping). Redirect only when the user still holds batch access for the vacancy
    (the same queue check ``VacancyBatchDetailView`` enforces); otherwise keep the 404 so a
    non-reviewing role can never probe another role's batch."""
    if not get_queue_for_user(request.user).filter(position=entry).exists():
        raise Http404("No batch is available for this vacancy.")
    messages.info(request, message)
    return redirect("vacancy-batch-detail", pk=entry.pk)


class VacancyBatchExamView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """Vacancy-level batch exam: one schedule + one score grid + one finalize over the
    qualified pool, looping the per-case exam services. FRS-scoped (only the reviewing role for
    the vacancy sees its pool); the schedule/score actions refuse until the screening batch is
    ready (Phase-4 gate)."""

    template_name = "recruitment/vacancy_batch_exam.html"

    def _scoped_pool(self, request, entry):
        return [
            application
            for application in vacancy_exam_pool(entry)
            if user_can_view_application(request.user, application)
        ]

    def _context(self, request, entry, pool, schedule_form=None):
        candidates = []
        for application in pool:
            exam_record = get_exam_record(application)
            # At a re-validation stage (HRM Chief re-checking the Secretariat's exam), the
            # earlier pass's finalized scores are offered for copying — never auto-accepted.
            prior_record = next(
                (
                    record
                    for record in reversed(list(get_exam_records(application)))
                    if record.is_finalized and record.review_stage != application.case.current_stage
                ),
                None,
            )
            candidates.append(
                {
                    "application": application,
                    "case_id": application.case.id,
                    "exam_record": exam_record,
                    "exam_status": exam_record.exam_status if exam_record else "",
                    "general_score": exam_record.general_score if exam_record else None,
                    "technical_score": exam_record.technical_score if exam_record else None,
                    "schedule": get_notified_exam_sitting(application),
                    "prior_record": prior_record,
                }
            )
        weights = entry.assessment_weights_or_default
        candidate_count = len(candidates)
        schedule_sent = bool(candidates) and all(c["schedule"] for c in candidates)
        # What every candidate was told (the batch sits once): shown at the top of section 1
        # so nobody re-sends a schedule that already went out.
        current_sitting = candidates[0]["schedule"] if schedule_sent else None
        sitting_from_earlier_stage = bool(
            current_sitting
            and pool
            and current_sitting.review_stage != pool[0].case.current_stage
        )
        finalize_exam_body = (
            f"This locks the recorded exam scores for all {candidate_count} "
            f"candidate{'' if candidate_count == 1 else 's'} and moves the whole pool to the "
            "next review step. Scores can no longer be edited afterwards."
        )
        # Judgment item A: candidates marked Absent/Waived lock without advancing — say so
        # in the finalize confirmation.
        absent_waived_count = sum(
            1
            for c in candidates
            if c["exam_record"]
            and c["exam_record"].exam_status
            in {ExamRecord.ExamStatus.ABSENT, ExamRecord.ExamStatus.WAIVED}
        )
        finalize_exam_summary_items = []
        if absent_waived_count:
            finalize_exam_summary_items.append(
                f"{absent_waived_count} candidate{'' if absent_waived_count == 1 else 's'} "
                "marked Absent/Waived will not proceed"
            )
        first_exam_record = candidates[0]["exam_record"] if candidates else None
        withdrawal_requests = [
            c["application"]
            for c in candidates
            if c["application"].withdrawal_requested_at
            and c["application"].status != RecruitmentApplication.Status.WITHDRAWN
        ]
        return {
            "finalize_exam_summary_items": finalize_exam_summary_items,
            "withdrawal_requests": withdrawal_requests,
            "entry": entry,
            "candidates": candidates,
            "candidate_count": candidate_count,
            "schedule_sent": schedule_sent,
            "current_sitting": current_sitting,
            "has_prior_scores": any(c["prior_record"] for c in candidates),
            "sitting_from_earlier_stage": sitting_from_earlier_stage,
            "finalize_exam_body": finalize_exam_body,
            "is_plantilla": entry.branch == PositionPosting.Branch.PLANTILLA,
            "screening_batch": vacancy_screening_batch_status(entry),
            "weights": weights,
            "schedule_form": schedule_form
            or ExamScheduleForm(
                initial=(
                    {
                        "scheduled_for": timezone.localtime(current_sitting.scheduled_for).strftime("%Y-%m-%dT%H:%M"),
                        "venue": current_sitting.venue,
                        "instructions": current_sitting.instructions,
                        "notice_delivery": current_sitting.notice_delivery,
                    }
                    if current_sitting
                    else None
                )
            ),
            "batch_valid_from": (
                first_exam_record.valid_from if first_exam_record else None
            ),
            "batch_valid_until": (
                first_exam_record.valid_until if first_exam_record else None
            ),
        }

    def get(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        # RD-5: one door in BOTH directions. A continuous-intake vacancy is
        # examined per case, so its console is refused outright — otherwise the
        # console URL reopens the two-door bug RD-2 closed, just mirrored.
        if not vacancy_uses_batch_exam(entry):
            messages.error(
                request,
                "This vacancy's exam is recorded per case — open the candidate's "
                "case to schedule or score it.",
            )
            return redirect("vacancy-batch-detail", pk=entry.pk)
        pool = self._scoped_pool(request, entry)
        if not pool:
            return _batch_pool_empty_redirect(
                request,
                entry,
                "No candidates are at the exam step for this vacancy right now — they may "
                "already have been examined and moved to the interview stage, or none are "
                "ready yet.",
            )
        # Batch gate (Phase-4): while any applicant is still pending screening or awaiting
        # resubmission, the exam screen must not be exposed at all — not even read-only. Send
        # the reviewer back to the batch detail so a forced URL cannot preview the exam stage.
        if not vacancy_screening_batch_status(entry).is_ready:
            messages.error(request, vacancy_screening_batch_block_message(entry))
            return redirect("vacancy-batch-detail", pk=entry.pk)
        # SL-09: and the batch itself cannot open until the pool it covers is final —
        # otherwise a timely later applicant misses the sitting they were entitled to.
        pool_block = vacancy_batch_pool_block_message(entry)
        if pool_block:
            messages.error(request, pool_block)
            return redirect("vacancy-batch-detail", pk=entry.pk)
        return render(request, self.template_name, self._context(request, entry, pool))

    def post(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        # RD-5: same refusal as GET — a forced POST must not write through the
        # console for a per-case (continuous) vacancy.
        if not vacancy_uses_batch_exam(entry):
            messages.error(
                request,
                "This vacancy's exam is recorded per case — open the candidate's "
                "case to schedule or score it.",
            )
            return redirect("vacancy-batch-detail", pk=entry.pk)
        pool = self._scoped_pool(request, entry)
        if not pool:
            return _batch_pool_empty_redirect(
                request,
                entry,
                "No candidates are at the exam step for this vacancy right now — they may "
                "already have been examined and moved to the interview stage, or none are "
                "ready yet.",
            )
        # Same batch gate as GET: refuse every exam operation (schedule/score/finalize) while
        # the screening batch is on hold. The exam services re-check this too, but blocking here
        # keeps a forced POST from re-rendering the read-only exam screen on a form error.
        if not vacancy_screening_batch_status(entry).is_ready:
            messages.error(request, vacancy_screening_batch_block_message(entry))
            return redirect("vacancy-batch-detail", pk=entry.pk)
        # SL-09: and the batch itself cannot open until the pool it covers is final —
        # otherwise a timely later applicant misses the sitting they were entitled to.
        pool_block = vacancy_batch_pool_block_message(entry)
        if pool_block:
            messages.error(request, pool_block)
            return redirect("vacancy-batch-detail", pk=entry.pk)
        operation = request.POST.get("operation", "")

        if operation == "save_schedule":
            form = ExamScheduleForm(request.POST)
            if not form.is_valid():
                messages.error(
                    request,
                    _format_form_errors(form, "Complete the exam schedule fields before saving."),
                )
                return render(
                    request, self.template_name, self._context(request, entry, pool, schedule_form=form)
                )
            try:
                save_vacancy_exam_schedule(entry, request.user, form.cleaned_data)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Exam schedule sent to every candidate in the batch.")
            return redirect("vacancy-batch-exam", pk=entry.pk)

        if operation in {"save_scores", "finalize"}:
            finalize = operation == "finalize"
            rows = {}
            for application in pool:
                case_id = application.case.id
                general = (request.POST.get(f"general_score_{case_id}", "") or "").strip()
                technical = (request.POST.get(f"technical_score_{case_id}", "") or "").strip()
                row = {}
                if general:
                    row["general_score"] = general
                if technical:
                    row["technical_score"] = technical
                # RD-2: Absent / Waived is chosen here — the per-case exam screen
                # is read-only for a pooled vacancy, so this is the only door. That
                # makes it the only door for the remarks an absence/waiver REQUIRES,
                # too — without this field a batch holding one absent candidate could
                # never be finalized (E2E finding).
                outcome = (request.POST.get(f"outcome_{case_id}", "") or "").strip()
                if outcome:
                    row["exam_status"] = outcome
                notes = (request.POST.get(f"notes_{case_id}", "") or "").strip()
                if notes:
                    row["exam_notes"] = notes
                if row:
                    rows[case_id] = row
            # The pool sat one exam, so one validity window covers the batch.
            valid_from_raw = (request.POST.get("valid_from", "") or "").strip()
            valid_until_raw = (request.POST.get("valid_until", "") or "").strip()
            try:
                valid_from = date.fromisoformat(valid_from_raw) if valid_from_raw else None
                valid_until = date.fromisoformat(valid_until_raw) if valid_until_raw else None
            except ValueError:
                messages.error(request, "Enter valid dates for the exam validity window.")
                return redirect("vacancy-batch-exam", pk=entry.pk)
            clear_validity_window = bool(
                request.POST.get("clear_validity_window")
            )
            # Capture the pre-finalize handler role so the toast can tell "routed to a new
            # office" apart from "same office, next step". The pool moves in lockstep, so one
            # representative role is enough.
            handler_before_finalize = pool[0].current_handler_role if pool else ""
            try:
                saved = save_vacancy_exam_scores(
                    entry,
                    request.user,
                    rows,
                    finalize=finalize,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    clear_validity_window=clear_validity_window,
                )
            except (ValueError, ValidationError) as exc:
                # A ValidationError's str() is a raw Python dict — join its messages so the
                # reviewer reads "Provide remarks explaining the waiver or absence." rather
                # than "{'exam_notes': [...]}" (E2E finding).
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
                return redirect("vacancy-batch-exam", pk=entry.pk)
            if finalize:
                # An Absent candidate did NOT advance — they hold at the exam awaiting the
                # letter-of-intent decision (SL-06), and a Waived one closed terminally.
                # Counting them as "advanced" told the reviewer something false (E2E finding).
                held = [
                    application
                    for application in saved
                    if (request.POST.get(f"outcome_{application.case.id}", "") or "").strip()
                    in {ExamRecord.ExamStatus.ABSENT, ExamRecord.ExamStatus.WAIVED}
                ]
                count = len(saved) - len(held)
                held_note = (
                    f" {len(held)} candidate{'' if len(held) == 1 else 's'} did not sit the "
                    "exam and awaits the absence decision instead."
                    if held
                    else ""
                )
                # save_exam_record mutates each routed application in place, so a changed
                # handler role means the pool advanced to a new office. When it is unchanged
                # (e.g. a COS batch the HRM Chief finalizes at their own review stage — the
                # case only moves from the exam section to the interview section), don't claim
                # a handoff that didn't happen.
                advanced = [a for a in saved if a not in held]
                next_role = advanced[0].current_handler_role if advanced else ""
                if next_role and next_role != handler_before_finalize:
                    next_role_label = RecruitmentUser.Role(next_role).label
                    messages.success(
                        request,
                        f"Exam batch finalized — {count} "
                        f"candidate{'' if count == 1 else 's'} advanced to "
                        f"{next_role_label} review.{held_note}",
                    )
                else:
                    messages.success(
                        request,
                        f"Exam batch finalized — {count} "
                        f"candidate{'' if count == 1 else 's'} moved to the next review "
                        f"step.{held_note}",
                    )
                # Continue straight to the interview console when this user is the one who
                # runs the next step (Plantilla: the Secretariat / HRM Chief support the
                # interview scheduling; COS interview belongs to the HRM Chief). Checked against
                # the freshly-routed pool, which now sits in the interview section. If they are
                # not the interview handler, fall back to the batch overview. Either way, a
                # forced-empty interview console now shows a friendly note (see
                # _batch_pool_empty_redirect) rather than a 404.
                if saved and user_can_manage_interview_session(request.user, saved[0]):
                    return redirect("vacancy-batch-interview", pk=entry.pk)
                # Round B P3: the same landing rule as the interview finalize, so both
                # branches agree: this vacancy's batch page while the user still holds or
                # follows cases in it, otherwise the batches list.
                if (
                    get_queue_for_user(request.user).filter(position_id=entry.pk).exists()
                    or get_followed_applications(request.user).filter(position_id=entry.pk).exists()
                ):
                    return redirect("vacancy-batch-detail", pk=entry.pk)
                return redirect("vacancy-batches")
            messages.success(request, "Exam scores saved.")
            return redirect("vacancy-batch-exam", pk=entry.pk)

        messages.error(request, "This action is not available.")
        return redirect("vacancy-batch-exam", pk=entry.pk)


class VacancyBatchInterviewView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """Vacancy-level batch interview: one schedule over the whole pool, then a finalize.
    COS records a single end-user score per candidate inline (spec §6.2); Plantilla scores
    each candidate's competency sheet on the per-case detail and finalizes the pool together.
    Loops the per-case interview services so FRS routing + lock-step hold."""

    template_name = "recruitment/vacancy_batch_interview.html"

    def _scoped_pool(self, request, entry):
        return [
            application
            for application in vacancy_interview_pool(
                entry,
                include_recorded_absences=(
                    entry.branch == PositionPosting.Branch.PLANTILLA
                ),
            )
            if user_can_view_application(request.user, application)
        ]

    def _context(self, request, entry, pool, schedule_form=None):
        is_plantilla = entry.branch == PositionPosting.Branch.PLANTILLA
        candidates = []
        for application in pool:
            rating = get_interview_rating_for_user(application, request.user)
            session = get_interview_session(application)
            rating_count = get_interview_ratings(application).count()
            if session:
                rating_count += session.fallback_ratings.count()
            absence_exception = next(
                (
                    exception
                    for exception in get_attendance_exceptions(application)
                    if exception.assessment_stage == AttendanceException.Stage.INTERVIEW
                    and session
                    and exception.review_stage == session.review_stage
                ),
                None,
            )
            candidates.append(
                {
                    "application": application,
                    "case_id": application.case.id,
                    "session": session,
                    "rating": rating,
                    "rating_count": rating_count,
                    "absence_exception": absence_exception,
                    "can_mark_absent": bool(
                        is_plantilla
                        and session
                        and session.scheduled_for
                        and not session.is_finalized
                        and rating_count == 0
                        and user_can_manage_interview_session(request.user, application)
                    ),
                }
            )
        # WF-08: the HRMPSB panel can't rate a Plantilla pool until the rating sheet is
        # published — surface that prerequisite on the console instead of letting the
        # panel discover it on their case pages.
        rating_sheet_published = True
        rating_sheet_url = ""
        if is_plantilla:
            rating_sheet_published = get_published_competency_rating_template(entry) is not None
            if not rating_sheet_published and pool:
                rating_sheet_url = reverse("vacancy-rating-sheet", kwargs={"pk": entry.pk})
        withdrawal_requests = [
            c["application"]
            for c in candidates
            if c["application"].withdrawal_requested_at
            and c["application"].status != RecruitmentApplication.Status.WITHDRAWN
        ]
        # Only the role that runs the interview sends its schedule (the Secretariat for
        # Level 1, the HRM Chief for Level 2 or COS); everyone else sees what was sent.
        can_schedule = bool(pool) and all(
            user_can_manage_interview_session(request.user, application) for application in pool
        )
        sessions = [c["session"] for c in candidates]
        current_interview = (
            sessions[0]
            if sessions and all(s and s.scheduled_for for s in sessions)
            else None
        )
        return {
            "withdrawal_requests": withdrawal_requests,
            "can_schedule": can_schedule,
            "current_interview": current_interview,
            "entry": entry,
            "candidates": candidates,
            "is_plantilla": is_plantilla,
            "schedule_form": schedule_form
            or InterviewSessionForm(
                initial=(
                    {
                        "scheduled_for": timezone.localtime(current_interview.scheduled_for).strftime("%Y-%m-%dT%H:%M"),
                        "location": current_interview.location,
                        "session_notes": current_interview.session_notes,
                    }
                    if current_interview
                    else None
                )
            ),
            "rating_sheet_published": rating_sheet_published,
            "rating_sheet_url": rating_sheet_url,
        }

    def get(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        # RD-5: a continuous-intake vacancy is interviewed per case — its
        # console is refused so the two-door bug can't reopen in reverse.
        if not vacancy_uses_batch_exam(entry):
            messages.error(
                request,
                "This vacancy's interview is recorded per case — open the "
                "candidate's case to schedule or score it.",
            )
            return redirect("vacancy-batch-detail", pk=entry.pk)
        pool = self._scoped_pool(request, entry)
        if not pool:
            return _batch_pool_empty_redirect(
                request,
                entry,
                "No candidates are at the interview step for this vacancy right now — they "
                "may already have been interviewed and moved to the next stage, or none are "
                "ready yet.",
            )
        # SL-09: the interview batch is the same single cohort as the exam batch, so it
        # waits for the same final pool.
        pool_block = vacancy_batch_pool_block_message(entry)
        if pool_block:
            messages.error(request, pool_block)
            return redirect("vacancy-batch-detail", pk=entry.pk)
        return render(request, self.template_name, self._context(request, entry, pool))

    def post(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        # RD-5: same refusal as GET (server-side, PT-03).
        if not vacancy_uses_batch_exam(entry):
            messages.error(
                request,
                "This vacancy's interview is recorded per case — open the "
                "candidate's case to schedule or score it.",
            )
            return redirect("vacancy-batch-detail", pk=entry.pk)
        pool = self._scoped_pool(request, entry)
        if not pool:
            return _batch_pool_empty_redirect(
                request,
                entry,
                "No candidates are at the interview step for this vacancy right now — they "
                "may already have been interviewed and moved to the next stage, or none are "
                "ready yet.",
            )
        # SL-09: the interview batch is the same single cohort as the exam batch, so it
        # waits for the same final pool.
        pool_block = vacancy_batch_pool_block_message(entry)
        if pool_block:
            messages.error(request, pool_block)
            return redirect("vacancy-batch-detail", pk=entry.pk)
        operation = request.POST.get("operation", "")

        if operation == "mark_absent" and entry.branch == PositionPosting.Branch.PLANTILLA:
            case_id = (request.POST.get("case_id", "") or "").strip()
            target = next(
                (application for application in pool if str(application.case.id) == case_id),
                None,
            )
            if target is None:
                messages.error(request, "This candidate is not in the current interview batch.")
                return redirect("vacancy-batch-interview", pk=entry.pk)
            try:
                mark_interview_absent(
                    target,
                    request.user,
                    notes=(request.POST.get("notes", "") or "").strip(),
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    "Interview absence recorded. The candidate is waiting for the letter-of-intent decision.",
                )
            return redirect("vacancy-batch-interview", pk=entry.pk)

        if operation == "save_schedule":
            form = InterviewSessionForm(request.POST)
            if not form.is_valid():
                messages.error(
                    request,
                    _format_form_errors(form, "Complete the interview schedule fields before saving."),
                )
                return render(
                    request, self.template_name, self._context(request, entry, pool, schedule_form=form)
                )
            try:
                save_vacancy_interview_schedule(entry, request.user, form.cleaned_data)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Interview schedule sent to every candidate in the batch.")
            return redirect("vacancy-batch-interview", pk=entry.pk)

        if operation in {"save_scores", "finalize"} and entry.branch == PositionPosting.Branch.COS:
            finalize = operation == "finalize"
            rows = {}
            for application in pool:
                case_id = application.case.id
                score = (request.POST.get(f"rating_score_{case_id}", "") or "").strip()
                notes = (request.POST.get(f"rating_notes_{case_id}", "") or "").strip()
                justification = (request.POST.get(f"justification_{case_id}", "") or "").strip()
                row = {}
                if score:
                    row["rating_score"] = score
                if notes:
                    row["rating_notes"] = notes
                if justification:
                    row["justification"] = justification
                # SL-06: the console is the pooled vacancy's only interview door, so the
                # no-show must be recordable HERE. The per-case absence form is hidden for
                # pooled vacancies on the strength of exactly this control — without it,
                # interview absence had no door at all (live E2E finding). The service
                # already accepted the row key; the console just never offered it.
                outcome = (request.POST.get(f"int_outcome_{case_id}", "") or "").strip()
                if outcome:
                    row["attendance_outcome"] = outcome
                if row:
                    rows[case_id] = row
            try:
                save_vacancy_cos_interview_ratings(entry, request.user, rows, finalize=finalize)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
                return redirect("vacancy-batch-interview", pk=entry.pk)
            if finalize:
                messages.success(request, "Interview batch finalized — the pool moved on together.")
                # Same forward as the exam console: for COS the next step is the deliberation
                # pick, run by the same HRM Chief who just finalized the interview. Plantilla
                # deliberately keeps the overview — its next step is the CAR, which belongs to
                # the Secretariat, so there is no single next screen for this finalizer.
                if user_can_manage_cos_deliberation(request.user, entry):
                    return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
                return redirect("vacancy-batches")
            messages.success(request, "Interview scores saved.")
            return redirect("vacancy-batch-interview", pk=entry.pk)

        if operation == "finalize" and entry.branch == PositionPosting.Branch.PLANTILLA:
            # WF-16(c): optional light record of the board's first (pre-CAR) deliberation.
            note = ""
            delib_date = (request.POST.get("first_deliberation_date", "") or "").strip()
            delib_members = (request.POST.get("first_deliberation_members", "") or "").strip()
            if delib_date or delib_members:
                note = (
                    f"First deliberation held on {delib_date}" if delib_date else "First deliberation held"
                )
                if delib_members:
                    note += f", members present {delib_members}"
                note += "."
            try:
                finalize_vacancy_interview_batch(
                    entry, request.user, first_deliberation_note=note
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
                return redirect("vacancy-batch-interview", pk=entry.pk)
            # Land on this vacancy's batch page, which lists what comes next, while this user
            # still has cases there; otherwise the batches list.
            messages.success(
                request,
                f"Interview batch finalized for every candidate. {_interview_finalize_next_step(entry, request.user)}",
            )
            if (
                get_queue_for_user(request.user).filter(position_id=entry.pk).exists()
                or get_followed_applications(request.user).filter(position_id=entry.pk).exists()
            ):
                return redirect("vacancy-batch-detail", pk=entry.pk)
            return redirect("vacancy-batches")

        messages.error(request, "This action is not available.")
        return redirect("vacancy-batch-interview", pk=entry.pk)


def _interview_finalize_next_step(entry, user):
    """What happens after the interview batch, said to the person who finalized it (C2)."""
    if entry.branch != PositionPosting.Branch.PLANTILLA:
        return "Next: the HRM Chief's deliberation and selection."
    if user.role == RecruitmentUser.Role.HRM_CHIEF:
        return (
            "Next: the Secretariat prepares the Comparative Assessment Report. You can follow "
            "it from this vacancy's page."
        )
    return "Next: prepare the Comparative Assessment Report from any candidate's case."


class VacancyBatchCosDeliberationView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """The COS decision: one per-vacancy deliberation pick (spec §6.3). The HRM Chief records
    the chosen applicant + notes for the whole pool.

    SL-01: finalizing the pick IS the COS selection — it records the chosen applicant as
    selected, routes them to completion, and closes the rest of the deliberated pool. There
    is no second per-case decision screen that could name someone else."""

    template_name = "recruitment/vacancy_batch_cos_deliberation.html"

    def _scoped_pool(self, request, entry):
        return [
            application
            for application in cos_deliberation_pool(entry)
            if user_can_view_application(request.user, application)
        ]

    def _context(self, request, entry, pool):
        pick = get_cos_vacancy_deliberation(entry)
        # SL-14 (COS): never present a rescinded pick as the working state — the template
        # pre-checks its candidate's radio and shows its notes, which would nudge the
        # re-decision toward the person the office just voided.
        if pick is not None and pick.is_rescinded:
            pick = None
        selection_round = get_open_cos_selection_round(entry)
        latest_round = selection_round or get_latest_cos_selection_round(entry)
        # WF-09: the pick asks the chief to name "the most qualified applicant" — show the
        # finalized comparative data (exam, interview, screening) next to each radio so the
        # decision doesn't rely on recall. Strongest interview scores first; choice stays free.
        candidates = []
        for application in pool:
            exam_record = get_exam_record(application)
            interview_rating = get_interview_ratings(application).first()
            screening_record = get_screening_record(application)
            candidates.append(
                {
                    "application": application,
                    "case_id": application.case.id,
                    "exam_score": exam_record.exam_score if exam_record else None,
                    "interview_score": (
                        interview_rating.rating_score if interview_rating else None
                    ),
                    "screening_outcome": (
                        screening_record.qualification_outcome if screening_record else ""
                    ),
                }
            )
        candidates.sort(
            key=lambda c: (c["interview_score"] is None, -(c["interview_score"] or 0))
        )
        # SL-02: a continuous COS vacancy hires in need-based rounds, so the console has to
        # say which round is open, how many slots it fills, and who is waiting in the pool.
        eligible_pool = cos_eligible_pool(entry)
        return {
            "entry": entry,
            "candidates": candidates,
            "candidate_count": len(candidates),
            "pick": pick,
            "can_manage": user_can_manage_cos_deliberation(request.user, entry),
            "uses_selection_rounds": vacancy_uses_cos_selection_rounds(entry),
            "selection_round": selection_round,
            "latest_round": latest_round,
            "round_picks": (
                # SL-14 (COS): the round's LIVE picks — a rescinded one is not a filled slot.
                list(latest_round.picks.filter(
                    is_finalized=True, is_rescinded=False
                ).select_related("chosen_case", "chosen_case__application"))
                if latest_round
                else []
            ),
            "can_open_round": user_can_open_cos_selection_round(request.user, entry),
            "can_close_round_short": user_can_close_cos_selection_round_short(
                request.user, entry
            ),
            "eligible_pool_count": len(eligible_pool),
        }

    def get(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        if entry.branch != PositionPosting.Branch.COS:
            raise Http404("The COS deliberation pick is only for COS vacancies.")
        pool = self._scoped_pool(request, entry)
        pick = get_cos_vacancy_deliberation(entry)
        if (
            not pool
            and not (pick and pick.is_finalized)
            and not user_can_open_cos_selection_round(request.user, entry)
            and get_latest_cos_selection_round(entry) is None
        ):
            raise Http404("No COS deliberation is available for this vacancy.")
        return render(request, self.template_name, self._context(request, entry, pool))

    def post(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        operation = request.POST.get("operation", "")
        if operation == "open_round":
            # SL-02: the office decides when it is hiring; opening the round freezes the
            # candidates it will consider.
            if not user_can_open_cos_selection_round(request.user, entry):
                raise PermissionDenied
            try:
                selection_round = open_cos_selection_round(
                    entry,
                    request.user,
                    slot_count=request.POST.get("slot_count", "1"),
                    notes=request.POST.get("round_notes", ""),
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(
                    request,
                    f"Selection round {selection_round.round_number} opened with "
                    f"{selection_round.members.count()} candidate(s) and "
                    f"{selection_round.slot_count} slot(s).",
                )
            return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
        if operation == "close_round_short":
            try:
                selection_round = close_cos_selection_round_short(
                    entry,
                    request.user,
                    request.POST.get("close_short_reason", ""),
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(
                    request,
                    f"Selection round {selection_round.round_number} closed with "
                    f"{selection_round.picks.filter(is_finalized=True, is_rescinded=False).count()} "
                    f"of {selection_round.slot_count} slots filled.",
                )
            return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
        if not user_can_manage_cos_deliberation(request.user, entry):
            raise PermissionDenied
        finalize = operation == "finalize"
        if operation not in {"save", "finalize"}:
            messages.error(request, "This action is not available.")
            return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
        try:
            save_cos_vacancy_deliberation(
                entry,
                request.user,
                request.POST.get("chosen_case"),
                request.POST.get("notes", ""),
                finalize=finalize,
            )
        except (ValueError, ValidationError) as exc:
            messages.error(request, str(exc))
            return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
        if finalize:
            # SL-01: finalizing IS the selection — say so, because it is the last
            # reversible moment. SL-02: what happens to the rest depends on how the
            # vacancy hires, so do not promise the wrong one.
            if vacancy_uses_cos_selection_rounds(entry):
                messages.success(
                    request,
                    "COS deliberation pick finalized. The chosen applicant is recorded as "
                    "selected and routed to completion; the candidates this round did not "
                    "pick stay in the pool for the next round.",
                )
            else:
                messages.success(
                    request,
                    "COS deliberation pick finalized. The chosen applicant is recorded as "
                    "selected and routed to completion; the rest of the deliberated pool "
                    "is closed as not selected and notified.",
                )
            return redirect("vacancy-batches")
        messages.success(request, "COS deliberation pick saved.")
        return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)


class VacancyRatingSheetView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """P3: the rating sheet belongs to the vacancy. This address opens it through any case of
    the vacancy the user may manage (the builder edits the per-position template)."""

    def get(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        for application in RecruitmentApplication.objects.filter(position=entry).select_related("case"):
            if getattr(application, "case", None) and user_can_manage_interview_session(request.user, application):
                return redirect("interview-rating-sheet", pk=application.pk)
        raise Http404("No rating sheet you can open for this vacancy.")


class VacancyBatchHandoffView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """C4: lend a vacancy's batch to the other office, or recall it."""

    def post(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        operation = request.POST.get("operation", "")
        remarks = request.POST.get("remarks", "")
        try:
            if operation == "send":
                moved = hand_off_vacancy_batch(entry, request.user, remarks)
                target = "HRM Chief" if request.user.role == RecruitmentUser.Role.SECRETARIAT else "Secretariat"
                messages.success(
                    request,
                    f"Sent {len(moved)} case{'' if len(moved) == 1 else 's'} to the {target}. "
                    "You can still follow the batch here and recall it.",
                )
            elif operation == "recall":
                moved = recall_vacancy_batch(entry, request.user, remarks)
                messages.success(
                    request,
                    f"Recalled {len(moved)} case{'' if len(moved) == 1 else 's'}. The batch is back with you.",
                )
            else:
                messages.error(request, "This action is not available.")
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        if (
            get_queue_for_user(request.user).filter(position_id=pk).exists()
            or get_loaned_out_applications(request.user).filter(position_id=pk).exists()
            or get_followed_applications(request.user).filter(position_id=pk).exists()
        ):
            return redirect("vacancy-batch-detail", pk=pk)
        return redirect("vacancy-batches")
