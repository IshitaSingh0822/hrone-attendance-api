"""Integration tests: need a running MongoDB 6.0+ (MONGO_URI, default localhost)."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from app import main

IST = main.IST


def ms(day, hour, minute, second=0, month=7):
    return int(dt.datetime(2026, month, day, hour, minute, second, tzinfo=IST).timestamp() * 1000)


def emp(code, dept, joined="2026-07-01", **extra):
    return {"emp_code": code, "name": code, "email": f"{code}@x.io", "department": dept,
            "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
            "created_at": dt.datetime(2026, 7, 1, tzinfo=main.UTC), **extra}


def log(code, day, status="PRESENT", late=0, hours=None, half=False, ot=0):
    return {"emp_code": code, "date": f"2026-07-{day:02d}", "status": status,
            "punch_in": None if status in ("ABSENT", "LEAVE") else dt.datetime(2026, 7, day, 4, 0, tzinfo=main.UTC),
            "punch_out": None, "work_hours": hours, "late_minutes": late, "overtime_minutes": ot,
            "half_day": half, "history": []}


@pytest.fixture(scope="session")
def mongo_up():
    try:
        main.client.admin.command("ping")
    except PyMongoError:
        pytest.skip("MongoDB is not reachable", allow_module_level=False)


@pytest.fixture()
def api(mongo_up):
    main.db.employees.drop()
    main.db.attendance_logs.drop()
    main.ensure_indexes()
    return TestClient(main.app)


@pytest.fixture()
def seeded(api):
    main.db.employees.insert_many([
        emp("EMP0001", "Alpha"), emp("EMP0002", "Alpha"), emp("EMP0003", "Alpha", joined="2026-07-20"),
        emp("EMP0004", "Alpha"), emp("EMP0005", "Beta"),
    ])
    main.db.attendance_logs.insert_many([
        log("EMP0001", 6, late=20, hours=9.0), log("EMP0001", 7, late=20, hours=3.5, half=True),
        log("EMP0002", 6, late=20, hours=8.0), log("EMP0002", 8, status="LEAVE"),
        log("EMP0002", 11, late=20, hours=8.0, ot=40), log("EMP0005", 6, late=50, hours=9.0),
    ])
    return api


def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_duplicate_employee_is_409(api):
    body = {"emp_code": "EMP0010", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-07-01"}
    assert api.post("/employees", json=body).status_code == 201
    assert api.post("/employees", json=body).status_code == 409


def test_concurrent_punch_in_creates_one_record(api):
    main.db.employees.insert_one(emp("EMP0001", "Alpha"))
    payload = {"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)}

    def hit(_):
        return TestClient(main.app).post("/attendance/punch-in", json=payload).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(hit, range(8)))
    assert codes == [201] + [409] * 7


def test_overnight_flow(api):
    main.db.employees.insert_one(emp("EMP0001", "Alpha", shift_start="22:00", shift_end="06:00"))
    made = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(6, 21, 55)})
    assert made.status_code == 201 and made.json()["date"] == "2026-07-06"
    done = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(7, 6, 40)}).json()
    assert (done["work_hours"], done["overtime_minutes"], done["late_minutes"]) == (8.75, 40, 0)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(7, 6, 50)}).status_code == 409


def test_punch_errors(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    main.db.employees.insert_one(emp("EMP0001", "Alpha"))
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(6, 18, 0)}).status_code == 404
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(8, 9, 30)}).status_code == 422


def test_regularize_records_history(api):
    main.db.employees.insert_one(emp("EMP0001", "Alpha"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(6, 10, 5)})
    body = {"punch_in": ms(6, 9, 30), "reason": "forgot to punch", "regularized_by": "hr"}
    fixed = api.patch("/attendance/EMP0001/2026-07-06", json=body).json()
    entry = fixed["history"][0]
    assert fixed["late_minutes"] == 0 and entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert entry["changes"]["punch_in"] == {"from": ms(6, 10, 5), "to": ms(6, 9, 30)}
    assert api.patch("/attendance/EMP0001/2026-07-06", json=body).status_code == 422  # no change
    assert api.patch("/attendance/EMP0001/2026-07-09", json=body).status_code == 404
    bad = {"status": "LEAVE", "punch_in": ms(6, 9, 30), "reason": "nope nope", "regularized_by": "hr"}
    assert api.patch("/attendance/EMP0001/2026-07-06", json=bad).status_code == 422


def test_attendance_list_order_and_total(seeded):
    data = seeded.get("/attendance?page_size=3").json()
    assert data["total"] == 6
    assert [(r["date"], r["emp_code"]) for r in data["items"]] == [
        ("2026-07-11", "EMP0002"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001")]


def test_employee_monthly(seeded):
    two = seeded.get("/analytics/employees/EMP0002/monthly?month=2026-07").json()
    assert two == {"emp_code": "EMP0002", "month": "2026-07", "working_days": 23, "present_days": 1.0,
                   "leave_days": 1, "late_count": 2, "total_late_minutes": 40,
                   "total_overtime_minutes": 40, "attendance_pct": 4.35}
    one = seeded.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
    assert (one["present_days"], one["attendance_pct"]) == (1.5, 6.52)
    assert seeded.get("/analytics/employees/EMP0003/monthly?month=2026-07").json()["working_days"] == 10
    assert seeded.get("/analytics/employees/NOPE/monthly?month=2026-07").status_code == 404


def test_department_summary(seeded):
    items = seeded.get("/analytics/departments/summary?month=2026-07").json()["items"]
    assert items[0] == {"department": "Alpha", "headcount": 4, "present_days": 2.5, "avg_work_hours": 7.13,
                        "late_count": 4, "total_late_minutes": 80, "leave_count": 1, "on_duty_count": 0}
    assert (items[1]["department"], items[1]["headcount"], items[1]["avg_work_hours"]) == ("Beta", 1, 9.0)


def test_leaderboard_ties_extend_past_limit(seeded):
    items = seeded.get("/analytics/leaderboard/late?month=2026-07&limit=2").json()["items"]
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in items] == [
        (1, "EMP0005", 50), (2, "EMP0001", 40), (2, "EMP0002", 40)]
    only_alpha = seeded.get("/analytics/leaderboard/late?month=2026-07&department=Alpha").json()["items"]
    assert [r["rank"] for r in only_alpha] == [1, 1]


def test_trend_fills_gaps_and_moving_average(seeded):
    rows = seeded.get("/analytics/departments/Alpha/trend?from=2026-07-06&to=2026-07-12").json()["items"]
    assert [r["date"] for r in rows] == [f"2026-07-{d:02d}" for d in range(6, 13)]
    assert [r["attendance_rate"] for r in rows] == [0.6667, 0.1667, 0.0, 0.0, 0.0, None, None]
    assert [r["moving_avg_7d"] for r in rows] == [0.6667, 0.4167, 0.2778, 0.2084, 0.1667, 0.1667, 0.1667]
    assert rows[5]["present_count"] == 1.0 and rows[5]["is_working_day"] is False and rows[0]["headcount"] == 3
    assert seeded.get("/analytics/departments/Nope/trend?from=2026-07-06&to=2026-07-12").status_code == 404


def test_trend_empty_range_still_has_every_day(seeded):
    rows = seeded.get("/analytics/departments/Alpha/trend?from=2026-09-01&to=2026-09-05").json()["items"]
    assert len(rows) == 5 and all(r["present_count"] == 0 for r in rows)


@pytest.mark.parametrize("path", [
    "attendance_list", "attendance_list?emp_code=EMP0001", "attendance_list?date_from=2026-07-01&date_to=2026-07-31",
    "employee_monthly?emp_code=EMP0001&month=2026-07", "department_summary?month=2026-07",
    "late_leaderboard?month=2026-07", "department_trend?department=Alpha&from=2026-07-06&to=2026-07-12",
])
def test_explain_uses_indexes(seeded, path):
    body = seeded.get(f"/admin/explain/{path}").json()
    text = str(body["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text
