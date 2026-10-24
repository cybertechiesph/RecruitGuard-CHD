"""TI-5: the internal schedule calendar.

The calendar is READ-ONLY and only aggregates dates the workflow already records: exam
schedules, interview sessions, vacancy closing dates, document-resubmission deadlines,
completion-requirement deadlines and recorded deliberations. It adds no new data.

SCOPING RULE: every event is built from applications the user can already see in the system,
i.e. get_queue_for_user(user) together with get_closed_cases_for_user(user). Nothing here
widens access: a Secretariat never sees a Level 2 event, and an HRMPSB member sees only the
cases their existing access allows. Vacancy closing dates come from the same scoped
applications, plus the postings an entry manager already manages on Recruitment Entries.

Events never carry applicant names. Batch exams and interviews (same vacancy, same kind,
same date and time) are grouped into one event with an applicant count, which also keeps
the calendar consistent with the anonymised queue.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import calendar as pycalendar

from django.urls import reverse
from django.utils import timezone

from .models import (
    CompletionRecord,
    DeliberationRecord,
    ExamSchedule,
    InterviewSession,
    PositionPosting,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningRecord,
)
from .services import (
    ENTRY_MANAGER_ROLES,
    get_closed_cases_for_user,
    get_manageable_recruitment_entries,
    get_queue_for_user,
)


class EventKind:
    EXAM = "exam"
    INTERVIEW = "interview"
    DELIBERATION = "deliberation"
    CLOSING = "closing"
    RESUBMISSION = "resubmission"
    COMPLETION = "completion"

    LABELS = {
        EXAM: "Examination",
        INTERVIEW: "Interview",
        DELIBERATION: "Deliberation recorded",
        CLOSING: "Application deadline",
        RESUBMISSION: "Resubmission deadline",
        COMPLETION: "Completion deadline",
    }


@dataclass
class ScheduleEvent:
    kind: str
    day: date
    at: datetime | None
    vacancy: PositionPosting
    url: str
    location: str = ""
    count: int = 1
    case_label: str = ""
    application_ids: list = field(default_factory=list)

    @property
    def label(self):
        return EventKind.LABELS[self.kind]

    @property
    def short_title(self):
        """For the month grid, where the kind chip already says what the event is."""
        if self.kind in (EventKind.EXAM, EventKind.INTERVIEW):
            return f"{self.vacancy.title} · {self.count}"
        if self.case_label:
            return f"{self.vacancy.title} · {self.case_label}"
        return self.vacancy.title

    @property
    def title(self):
        if self.kind in (EventKind.EXAM, EventKind.INTERVIEW):
            noun = "applicant" if self.count == 1 else "applicants"
            return f"{self.label} — {self.vacancy.title} ({self.count} {noun})"
        if self.case_label:
            return f"{self.label} — {self.vacancy.title} · {self.case_label}"
        return f"{self.label} — {self.vacancy.title}"


def scoped_application_ids(user):
    """Applications the user can already open: their queue plus their closed cases."""
    ids = set(get_queue_for_user(user).values_list("pk", flat=True))
    ids.update(get_closed_cases_for_user(user).values_list("pk", flat=True))
    return ids


def _case_label(user, application):
    # Mirrors the queue: HRMPSB members see the candidate code, never the case number.
    if user.role == RecruitmentUser.Role.HRMPSB_MEMBER:
        return f"Candidate {application.candidate_code_label}"
    return application.reference_label


def _aware_range(start, end):
    tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(start, time.min), tz),
        timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz),
    )


def get_schedule_events_for_user(user, start, end):
    """All scoped events with a date in [start, end], sorted by day then time."""
    app_ids = scoped_application_ids(user)
    lo, hi = _aware_range(start, end)
    events = []

    def _grouped(queryset, kind, place_attr):
        groups = OrderedDict()
        for item in queryset.select_related("application__position").order_by("scheduled_for", "pk"):
            position = item.application.position
            local = timezone.localtime(item.scheduled_for)
            key = (position.pk, local.replace(second=0, microsecond=0))
            group = groups.get(key)
            if group is None:
                group = groups[key] = ScheduleEvent(
                    kind=kind,
                    day=local.date(),
                    at=local,
                    vacancy=position,
                    url=reverse("vacancy-batch-detail", args=[position.pk]),
                    location=getattr(item, place_attr) or "",
                    count=0,
                )
            group.count += 1
            group.application_ids.append(item.application_id)
        return list(groups.values())

    if app_ids:
        events += _grouped(
            ExamSchedule.objects.filter(application_id__in=app_ids, scheduled_for__gte=lo, scheduled_for__lt=hi),
            EventKind.EXAM,
            "venue",
        )
        events += _grouped(
            InterviewSession.objects.filter(application_id__in=app_ids, scheduled_for__gte=lo, scheduled_for__lt=hi),
            EventKind.INTERVIEW,
            "location",
        )

        for record in (
            ScreeningRecord.objects.filter(
                application_id__in=app_ids,
                resubmission_deadline__gte=start,
                resubmission_deadline__lte=end,
                application__case__case_status=RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
            )
            .select_related("application__position")
            .order_by("resubmission_deadline", "pk")
        ):
            events.append(
                ScheduleEvent(
                    kind=EventKind.RESUBMISSION,
                    day=record.resubmission_deadline,
                    at=None,
                    vacancy=record.application.position,
                    url=reverse("application-detail", args=[record.application_id]),
                    case_label=_case_label(user, record.application),
                    application_ids=[record.application_id],
                )
            )

        for record in (
            CompletionRecord.objects.filter(
                application_id__in=app_ids,
                deadline__gte=start,
                deadline__lte=end,
                completion_date__isnull=True,
                is_voided=False,
            )
            .select_related("application__position")
            .order_by("deadline", "pk")
        ):
            events.append(
                ScheduleEvent(
                    kind=EventKind.COMPLETION,
                    day=record.deadline,
                    at=None,
                    vacancy=record.application.position,
                    url=reverse("application-detail", args=[record.application_id]),
                    case_label=_case_label(user, record.application),
                    application_ids=[record.application_id],
                )
            )

        # One "deliberation recorded" event per vacancy per day, however many records it wrote.
        seen = set()
        for record in (
            DeliberationRecord.objects.filter(
                application_id__in=app_ids, deliberated_at__gte=lo, deliberated_at__lt=hi
            )
            .select_related("application__position")
            .order_by("deliberated_at", "pk")
        ):
            local = timezone.localtime(record.deliberated_at)
            key = (record.application.position_id, local.date())
            if key in seen:
                continue
            seen.add(key)
            events.append(
                ScheduleEvent(
                    kind=EventKind.DELIBERATION,
                    day=local.date(),
                    at=local,
                    vacancy=record.application.position,
                    url=reverse("vacancy-batch-detail", args=[record.application.position_id]),
                )
            )

    position_ids = set(
        PositionPosting.objects.filter(applications__pk__in=app_ids).values_list("pk", flat=True)
    ) if app_ids else set()
    if user.role in ENTRY_MANAGER_ROLES:
        position_ids.update(get_manageable_recruitment_entries(user).values_list("pk", flat=True))
    if position_ids:
        for entry in (
            PositionPosting.objects.filter(pk__in=position_ids, closing_date__gte=start, closing_date__lte=end)
            .select_related("position_reference")
            .order_by("closing_date", "pk")
        ):
            events.append(
                ScheduleEvent(
                    kind=EventKind.CLOSING,
                    day=entry.closing_date,
                    at=None,
                    vacancy=entry,
                    # Only entry managers have a page for the posting itself; for everyone
                    # else the deadline is informational.
                    url=(
                        reverse("recruitment-entry-update", args=[entry.pk])
                        if user.role in ENTRY_MANAGER_ROLES
                        else ""
                    ),
                )
            )

    events.sort(key=lambda e: (e.day, e.at.time() if e.at else time.min, e.kind, e.vacancy.pk))
    return events


def group_events_by_day(events):
    days = OrderedDict()
    for event in events:
        days.setdefault(event.day, []).append(event)
    return days


def parse_month(value, today=None):
    """'YYYY-MM' -> first day of that month; anything else -> the current month."""
    today = today or timezone.localdate()
    try:
        year, month = (int(part) for part in (value or "").split("-"))
        return date(year, month, 1) if 1900 <= year <= 2999 else today.replace(day=1)
    except (TypeError, ValueError):
        return today.replace(day=1)


def build_month_grid(month_start, events):
    """Weeks (Sunday first) of {day, in_month, is_today, events} cells for the month view."""
    cal = pycalendar.Calendar(firstweekday=6)
    by_day = group_events_by_day(events)
    today = timezone.localdate()
    return [
        [
            {
                "day": day,
                "in_month": day.month == month_start.month,
                "is_today": day == today,
                "events": by_day.get(day, []),
            }
            for day in week
        ]
        for week in cal.monthdatescalendar(month_start.year, month_start.month)
    ]
