"""Time rules: the one place a date-time becomes a calendar date, and when an event counts as
held. The office runs on Manila time, so a date-time is turned into a date only through
`local_day`; a bare `.date()` on an aware date-time gives the UTC date, which is the wrong day
between midnight and 8 a.m. (finding F3). A test fails on any `.date()` call outside this
module. The suite-wide `event_not_held_yet` patch targets this module.
"""

from django.utils import timezone


def local_day(when):
    """The Manila calendar date of an aware date-time."""
    return timezone.localtime(when).date()


def event_not_held_yet(when):
    """True while a scheduled exam/interview is still in the future (results for it can't be
    finalized yet). One function so both guards, and the test suite, share the rule."""
    return bool(when and when > timezone.now())


def event_held_by(when, day):
    """True when an event scheduled at `when` had taken place by the date `day`."""
    return local_day(when) <= day


def not_held_yet_message(event, when):
    """Why results can't be finalized yet: the exam/interview is still in the future."""
    local = timezone.localtime(when)
    return (
        f"The {event} is scheduled for {local:%B} {local.day}, {local.year} at "
        f"{local:%I:%M %p}".replace(" at 0", " at ")
        + f". Its results can be finalized once it has taken place; you can save them as a draft until then."
    )
