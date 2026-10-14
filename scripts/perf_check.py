"""Load ~100k attendance records into a throwaway DB, then check the explain
endpoints for IXSCAN / COLLSCAN and time the real endpoints.

Run from the project folder:  python -m scripts.perf_check
"""
import datetime as dt
import os
import random
import sys
import time

os.environ["MONGO_DB"] = "attendance_perf"  # never touches your real database

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

EMPLOYEES, DEPARTMENTS, DAYS = 500, 10, 200
FIRST_DAY = dt.date(2026, 1, 5)

EXPLAIN_CASES = [
    ("attendance_list", "page_size=50"),
    ("attendance_list", "emp_code=EMP0001"),
    ("attendance_list", "date_from=2026-03-01&date_to=2026-03-31&status=PRESENT"),
    ("employee_monthly", "emp_code=EMP0001&month=2026-03"),
    ("department_summary", "month=2026-03"),
    ("late_leaderboard", "month=2026-03"),
    ("department_trend", "department=Dept1&from=2026-03-01&to=2026-05-31"),
]
TIMED_CASES = [
    ("GET /attendance", "/attendance?page_size=50"),
    ("monthly", "/analytics/employees/EMP0001/monthly?month=2026-03"),
    ("summary", "/analytics/departments/summary?month=2026-03"),
    ("leaderboard", "/analytics/leaderboard/late?month=2026-03"),
    ("trend (92 days)", "/analytics/departments/Dept1/trend?from=2026-03-01&to=2026-05-31"),
]


def load_data() -> None:
    random.seed(1)
    main.client.drop_database("attendance_perf")
    main.ensure_indexes()
    created = dt.datetime(2026, 1, 1, tzinfo=main.UTC)
    main.db.employees.insert_many([
        {"emp_code": f"EMP{n:04d}", "name": f"Employee {n}", "email": f"e{n}@example.com",
         "department": f"Dept{n % DEPARTMENTS}", "shift_start": "09:30", "shift_end": "18:30",
         "joined_on": "2026-01-01", "created_at": created}
        for n in range(1, EMPLOYEES + 1)
    ])
    batch = []
    for offset in range(DAYS):
        day = FIRST_DAY + dt.timedelta(days=offset)
        for n in range(1, EMPLOYEES + 1):
            status = random.choices(["PRESENT", "WFH", "LEAVE", "ABSENT"], [85, 5, 5, 5])[0]
            worked = status in main.PRESENCE
            late = random.choice([0, 0, 0, 15, 40]) if worked else 0
            start = dt.datetime.combine(day, dt.time(9, 30), tzinfo=main.IST) + dt.timedelta(minutes=late)
            batch.append({
                "emp_code": f"EMP{n:04d}", "date": day.isoformat(), "status": status,
                "punch_in": start if worked else None,
                "punch_out": start + dt.timedelta(hours=8) if worked else None,
                "work_hours": 8.0 if worked else None, "late_minutes": late,
                "overtime_minutes": 0, "half_day": False, "history": [],
            })
        if len(batch) >= 10_000:
            main.db.attendance_logs.insert_many(batch)
            batch = []
    if batch:
        main.db.attendance_logs.insert_many(batch)


def main_check() -> int:
    try:
        main.client.admin.command("ping")
    except Exception as exc:
        print("MongoDB is not reachable:", exc)
        return 2
    print("loading data...")
    load_data()
    print("logs:", main.db.attendance_logs.count_documents({}))
    api = TestClient(main.app)
    failed = False

    print("\nexplain checks")
    for endpoint, query in EXPLAIN_CASES:
        response = api.get(f"/admin/explain/{endpoint}?{query}")
        text = str(response.json().get("explain", "")) if response.status_code == 200 else ""
        ok = response.status_code == 200 and "IXSCAN" in text and "COLLSCAN" not in text
        failed |= not ok
        print(f"  {'OK  ' if ok else 'FAIL'} {endpoint:20s} {query}  (HTTP {response.status_code})")

    print("\ntimings")
    for label, url in TIMED_CASES:
        started = time.perf_counter()
        response = api.get(url)
        millis = (time.perf_counter() - started) * 1000
        print(f"  {label:18s} {millis:8.0f} ms  HTTP {response.status_code}")
        failed |= response.status_code != 200

    print("\nRESULT:", "FAILED" if failed else "all explain checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_check())
