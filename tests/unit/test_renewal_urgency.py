"""
PA Renewal urgency tests
=========================
compute_urgency is pure date arithmetic against "today" — no AI, no I/O.
Deterministic, so it's tested directly rather than through the API.
"""

from datetime import datetime, timedelta, timezone

from fillmypdf.models.renewal import compute_urgency


def _date(days_from_now: int) -> str:
    d = datetime.now(timezone.utc).date() + timedelta(days=days_from_now)
    return d.isoformat()


class TestComputeUrgency:
    def test_no_date_is_no_date(self):
        assert compute_urgency(None, "active") == "no_date"

    def test_past_date_is_overdue(self):
        assert compute_urgency(_date(-1), "active") == "overdue"

    def test_far_past_date_is_overdue(self):
        assert compute_urgency(_date(-400), "active") == "overdue"

    def test_today_is_due_soon(self):
        # Day 0 counts as due_soon, not overdue — nothing has lapsed yet.
        assert compute_urgency(_date(0), "active") == "due_soon"

    def test_within_30_days_is_due_soon(self):
        assert compute_urgency(_date(30), "active") == "due_soon"

    def test_31_days_is_upcoming(self):
        assert compute_urgency(_date(31), "active") == "upcoming"

    def test_90_days_is_upcoming(self):
        assert compute_urgency(_date(90), "active") == "upcoming"

    def test_91_days_is_later(self):
        assert compute_urgency(_date(91), "active") == "later"

    def test_non_active_status_never_alerts_even_if_overdue(self):
        # A renewed/cancelled record must never show up in the alert count,
        # regardless of what its stored renewal_due date says.
        assert compute_urgency(_date(-30), "renewed") == "no_date"
        assert compute_urgency(_date(-30), "cancelled") == "no_date"

    def test_malformed_date_string_is_no_date(self):
        assert compute_urgency("not-a-date", "active") == "no_date"

    def test_empty_string_is_no_date(self):
        assert compute_urgency("", "active") == "no_date"
