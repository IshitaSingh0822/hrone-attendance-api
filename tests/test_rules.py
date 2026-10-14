"""Pure-logic tests: no MongoDB needed."""
import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import main

IST = main.IST
DAY_SHIFT = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT_SHIFT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(day, hour, minute, second=0):
    return dt.datetime(2026, 7, day, hour, minute, second, tzinfo=IST)


def test_late_grace_boundary():
    start = ist(6, 9, 30)
    assert main.late_minutes(ist(6, 9, 40, 0), start) == 0
    assert main.late_minutes(ist(6, 9, 40, 1), start) == 10
    assert main.late_minutes(ist(6, 10, 5), start) == 35
    assert main.late_minutes(ist(6, 9, 0), start) == 0


def test_overtime_threshold_and_floor():
    end = ist(6, 18, 30)
    assert main.overtime_minutes(ist(6, 18, 59, 59), end) == 0
    assert main.overtime_minutes(ist(6, 19, 0), end) == 30
    assert main.overtime_minutes(ist(6, 19, 0, 59), end) == 30
    assert main.overtime_minutes(ist(6, 18, 0), end) == 0


def test_work_hours_round_half_up():
    start = ist(6, 9, 30)
    assert main.work_hundredths(start, start + dt.timedelta(hours=9, seconds=18)) == 901
    assert main.work_hundredths(start, start + dt.timedelta(hours=8, minutes=45)) == 875
    assert main.work_hundredths(start, start + dt.timedelta(hours=4, minutes=29, seconds=18)) == 449


def test_half_day_flag():
    bounds = main.shift_bounds(dt.date(2026, 7, 6), DAY_SHIFT)
    short = main.derive("PRESENT", ist(6, 9, 30), ist(6, 13, 59, 18), bounds)
    full = main.derive("PRESENT", ist(6, 9, 30), ist(6, 14, 0, 0), bounds)
    assert short["half_day"] is True and full["half_day"] is False
    assert main.derive("LEAVE", None, None, bounds)["late_minutes"] == 0


def test_overnight_attendance_day():
    assert main.attendance_day(ist(6, 21, 55), NIGHT_SHIFT) == dt.date(2026, 7, 6)
    assert main.attendance_day(ist(7, 5, 30), NIGHT_SHIFT) == dt.date(2026, 7, 6)
    assert main.attendance_day(ist(7, 6, 0), NIGHT_SHIFT) == dt.date(2026, 7, 7)
    assert main.attendance_day(ist(6, 9, 0), DAY_SHIFT) == dt.date(2026, 7, 6)


def test_overnight_derived_values():
    bounds = main.shift_bounds(dt.date(2026, 7, 6), NIGHT_SHIFT)
    out = main.derive("PRESENT", ist(6, 21, 55), ist(7, 6, 40), bounds)
    assert out == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}


def test_half_up_helper():
    assert main.half_up(Decimal("7.125"), 2) == 7.13
    assert main.half_up(Decimal("0.03125"), 4) == 0.0313


def test_working_days_for_mid_month_joiner():
    first, last = dt.date(2026, 7, 1), dt.date(2026, 7, 31)
    assert main.working_days(first, last, dt.date(2026, 7, 1)) == 23
    assert main.working_days(first, last, dt.date(2026, 7, 20)) == 10
    assert main.working_days(first, last, dt.date(2026, 8, 1)) == 0


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app)  # no lifespan, so no database is touched


@pytest.mark.parametrize("value", [1783312500.0, 1783312500, "1783312500000", 1783312500000.0, None, True])
def test_punch_in_rejects_bad_timestamps(client, value):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


def test_employee_validation(client):
    good = {"emp_code": "EMP0099", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-07-01"}
    for patch in [{"emp_code": "E1"}, {"joined_on": "2026-02-30"}, {"email": "nope"}, {"shift_start": "09:30", "shift_end": "09:30"}]:
        assert client.post("/employees", json={**good, **patch}).status_code == 422


def test_query_validation(client):
    assert client.get("/employees?page_size=101").status_code == 422
    assert client.get("/employees?page=0").status_code == 422
    assert client.get("/attendance?status=BOGUS").status_code == 422
    assert client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01").status_code == 422
    assert client.get("/analytics/departments/summary?month=2026-13").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
    assert client.get("/analytics/departments/X/trend?from=2026-07-10&to=2026-07-01").status_code == 422
    assert client.get("/analytics/departments/X/trend?from=2026-01-01&to=2026-06-01").status_code == 422
    assert client.get("/admin/explain/employee_monthly?month=2026-07").status_code == 422
    assert client.get("/admin/explain/nope").status_code == 422
