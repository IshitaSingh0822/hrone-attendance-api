"""Employee Attendance & Analytics API.

Run with:  uvicorn app.main:app --reload
Config:    MONGO_URI and MONGO_DB environment variables (a local .env is read too).
"""
import calendar
import datetime as dt
import json
import logging
import os
import re
from collections import Counter
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, NoReturn, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

log = logging.getLogger("attendance")

UTC = dt.timezone.utc
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
HALF_DAY_HUNDREDTHS = 450
MAX_SHIFT = dt.timedelta(hours=24)
MAX_TREND_DAYS = 92

MIN_EPOCH_MS = 100_000_000_000  # 1973; rejects values sent in seconds
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DAY_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

indexes_ready = False


def ensure_indexes() -> None:
    global indexes_ready
    employees, logs = db.employees, db.attendance_logs
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code")
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code")
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        log.exception("could not create indexes at startup, /health will retry")
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error_handler(request: Request, exc: PyMongoError):
    log.error("database error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# ---------------------------------------------------------------- helpers


def unprocessable(loc: tuple, message: str, value=None) -> NoReturn:
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": message, "input": value}])


def parse_day(value: str, name: str, where: str = "query") -> dt.date:
    if not re.fullmatch(DAY_RE, value):
        unprocessable((where, name), "must be a date in YYYY-MM-DD format", value)
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        unprocessable((where, name), "not a real calendar date", value)


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        unprocessable(("query", "month"), "year must be at least 0001", month)
    return dt.date(year, mon, 1), dt.date(year, mon, calendar.monthrange(year, mon)[1])


def require(value, name: str):
    if value is None:
        unprocessable(("query", name), "field required")
    return value


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def to_dt(millis: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(millis // 1000, tz=UTC)


def to_ms(moment: Optional[dt.datetime]) -> Optional[int]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment - EPOCH) // dt.timedelta(milliseconds=1)


def half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def hm(value: str) -> dt.time:
    hour, minute = value.split(":")
    return dt.time(int(hour), int(minute))


# ------------------------------------------------------------ attendance rules


def attendance_day(punched_in: dt.datetime, emp: dict) -> dt.date:
    """R1: the IST calendar date a punch-in belongs to (overnight shifts count toward the day they start)."""
    local = punched_in.astimezone(IST)
    start, end = hm(emp["shift_start"]), hm(emp["shift_end"])
    if end <= start and local.time() < end:
        return local.date() - dt.timedelta(days=1)
    return local.date()


def shift_bounds(day: dt.date, emp: dict) -> tuple[dt.datetime, dt.datetime]:
    start, end = hm(emp["shift_start"]), hm(emp["shift_end"])
    start_at = dt.datetime.combine(day, start, tzinfo=IST)
    end_at = dt.datetime.combine(day, end, tzinfo=IST)
    if end <= start:
        end_at += dt.timedelta(days=1)
    return start_at, end_at


def late_minutes(punched_in: dt.datetime, shift_start: dt.datetime) -> int:
    seconds = int((punched_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def overtime_minutes(punched_out: dt.datetime, shift_end: dt.datetime) -> int:
    seconds = int((punched_out - shift_end).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def work_hundredths(punched_in: dt.datetime, punched_out: dt.datetime) -> int:
    """Worked time in hours x 100, rounded half-up, using integer maths only."""
    seconds = int((punched_out - punched_in).total_seconds())
    return (seconds * 200 + 3600) // 7200


def derive(status: str, punched_in, punched_out, bounds: tuple[dt.datetime, dt.datetime]) -> dict:
    result = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE:
        return result
    punched_in = punched_in.replace(microsecond=0)
    result["late_minutes"] = late_minutes(punched_in, bounds[0])
    if punched_out is not None:
        punched_out = punched_out.replace(microsecond=0)
        hundredths = work_hundredths(punched_in, punched_out)
        result["work_hours"] = hundredths / 100
        result["overtime_minutes"] = overtime_minutes(punched_out, bounds[1])
        result["half_day"] = hundredths < HALF_DAY_HUNDREDTHS
    return result


def check_span(punched_in: dt.datetime, punched_out: dt.datetime) -> None:
    if punched_out <= punched_in:
        unprocessable(("body", "punched_at"), "punch-out must be after punch-in")
    if punched_out - punched_in > MAX_SHIFT:
        unprocessable(("body", "punched_at"), "punch-out is more than 24 hours after punch-in")


# ------------------------------------------------------------------ models


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=DAY_RE)

    @field_validator("joined_on")
    @classmethod
    def real_date(cls, value: str) -> str:
        dt.date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: EpochMs = None  # omitted means now; an explicit null is rejected
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: EpochMs = None


class RegularizeIn(BaseModel):
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------- serialization


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        before, after = change.get("from"), change.get("to")
        if field in ("punch_in", "punch_out"):
            before, after = to_ms(before), to_ms(after)
        changes[field] = {"from": before, "to": after}
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(entry) for entry in doc.get("history") or []],
    }


def find_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


# ------------------------------------------------------------------- health


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


# ---------------------------------------------------------------- employees

Page = Annotated[int, Query(ge=1, le=1_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="employee already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department else {}
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(doc) for doc in cursor],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------- attendance


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = find_employee(body.emp_code)
    punched = to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_day(punched, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **derive(body.status, punched, None, shift_bounds(day, emp)),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="attendance already exists for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = find_employee(body.emp_code)
    punched = to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    check_span(record["punch_in"], punched)

    bounds = shift_bounds(dt.date.fromisoformat(record["date"]), emp)
    derived = derive(record["status"], record["punch_in"], punched, bounds)
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": record["emp_code"],
            "date": record["date"],
            "punch_in": record["punch_in"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_day(date, "date", "path")
    emp = find_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    old_in, old_out = record.get("punch_in"), record.get("punch_out")
    status = body.status or record["status"]
    if status in PRESENCE:
        new_in = to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            unprocessable(("body", "punch_in"), "a presence status needs a punch_in")
        if attendance_day(new_in, emp) != day:
            unprocessable(("body", "punch_in"), "punch_in does not belong to this attendance date")
        if new_out is not None:
            check_span(new_in, new_out)
    else:
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(("body", "status"), f"{status} cannot have punch times")
        new_in = new_out = None

    before = {
        "status": record["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }
    after = {
        "status": status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derive(status, new_in, new_out, shift_bounds(day, emp)),
    }
    changes = {field: {"from": before[field], "to": after[field]} for field in after if before[field] != after[field]}
    if not changes:
        unprocessable(("body",), "the correction does not change anything")

    history = record.get("history") or []
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # compare-and-set on the state we validated against, so a concurrent punch-out
    # or correction cannot be silently overwritten or lose its history entry
    guard = {
        "emp_code": emp_code,
        "date": date,
        "status": before["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(history)]},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": after, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently, retry")
    return record_out(updated)


def attendance_find(emp_code, date_from, date_to, status, page, page_size) -> dict:
    start = parse_day(date_from, "date_from") if date_from else None
    end = parse_day(date_to, "date_to") if date_to else None
    if start and end and start > end:
        unprocessable(("query", "date_from"), "date_from must not be after date_to", date_from)
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = start.isoformat()
        if end:
            query["date"]["$lte"] = end.isoformat()
    if status:
        query["status"] = status
    return {
        "filter": query,
        "projection": {"_id": 0},
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = attendance_find(emp_code, date_from, date_to, status, page, page_size)
    return {
        "items": [record_out(doc) for doc in db.attendance_logs.find(**spec)],
        "total": db.attendance_logs.count_documents(spec["filter"]),
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------- analytics

IS_PRESENT = {"$in": ["$status", PRESENCE]}
WEIGHT_X2 = {"$cond": [IS_PRESENT, {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
IS_WEEKDAY = {
    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]
}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
HOURS_X100 = {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def working_days(first: dt.date, last: dt.date, joined_on: dt.date) -> int:
    day, total = max(first, joined_on), 0
    while day <= last:
        total += day.weekday() < 5
        day += dt.timedelta(days=1)
    return total


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_x2": {"$sum": {"$cond": [IS_WEEKDAY, WEIGHT_X2, 0]}},
                "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MIN},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_RE)]):
    emp = find_employee(emp_code)
    first, last = month_bounds(month)
    row = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)), {})
    working = working_days(first, last, dt.date.fromisoformat(emp["joined_on"]))
    present_x2 = row.get("present_x2", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present_x2 / 2,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(Decimal(present_x2) * 50 / working, 2) if working else None,
    }


def summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    staff = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        staff["department"] = department
    per_employee = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_x2": {"$sum": {"$cond": [IS_WEEKDAY, WEIGHT_X2, 0]}},
                "hours_x100": {"$sum": {"$cond": [HAS_HOURS, HOURS_X100, 0]}},
                "hours_n": count_if(HAS_HOURS),
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MIN},
                "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
            }
        },
    ]
    return [
        {"$match": staff},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": per_employee,
                "as": "m",
            }
        },
        {"$addFields": {"m": {"$ifNull": [{"$first": "$m"}, {"$literal": {}}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_x2": {"$sum": "$m.present_x2"},
                "hours_x100": {"$sum": "$m.hours_x100"},
                "hours_n": {"$sum": "$m.hours_n"},
                "late_count": {"$sum": "$m.late_count"},
                "total_late_minutes": {"$sum": "$m.total_late_minutes"},
                "leave_count": {"$sum": "$m.leave_count"},
                "on_duty_count": {"$sum": "$m.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_RE)], department: Optional[str] = None):
    rows = db.employees.aggregate(summary_pipeline(month, department))
    return {
        "month": month,
        "items": [
            {
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": row["present_x2"] / 2,
                "avg_work_hours": (
                    half_up(Decimal(row["hours_x100"]) / (100 * row["hours_n"]), 2) if row["hours_n"] else None
                ),
                "late_count": row["late_count"],
                "total_late_minutes": row["total_late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            }
            for row in rows
        ],
    }


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE_MIN}, "late_count": {"$sum": IS_LATE}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=MONTH_RE)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department))
    return {"month": month, "items": [{**row, "rank": int(row["rank"])} for row in rows]}


def trend_pipeline(department: str, from_day: str, to_day: str) -> list:
    start = parse_day(from_day, "from")
    end = parse_day(to_day, "to")
    if end < start:
        unprocessable(("query", "to"), "to must not be before from", to_day)
    if (end - start).days + 1 > MAX_TREND_DAYS:
        unprocessable(("query", "to"), f"range is limited to {MAX_TREND_DAYS} days", to_day)

    staff = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not staff:
        raise HTTPException(status_code=404, detail="department not found")
    eligible = [e for e in staff if e["joined_on"] <= end.isoformat()]
    joined = [{"d": day, "n": n} for day, n in Counter(e["joined_on"] for e in eligible).items()]

    lower = dt.datetime.combine(start, dt.time.min, tzinfo=UTC)
    upper = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=UTC)
    headcount = {
        "$reduce": {
            "input": {
                "$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j.d", "$date"]}}
            },
            "initialValue": 0,
            "in": {"$add": ["$$value", "$$this.n"]},
        }
    }
    window = {"documents": [-6, 0]}
    return [
        {
            "$match": {
                "emp_code": {"$in": [e["emp_code"] for e in eligible]},
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
            }
        },
        {"$group": {"_id": "$date", "present_x2": {"$sum": WEIGHT_X2}, "late_count": {"$sum": IS_LATE}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"day": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                "present_x2": {"$ifNull": ["$present_x2", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$addFields": {"headcount": headcount}},
        {
            "$addFields": {
                # rate in 1/10000ths, rounded half-up with exact arithmetic: floor((x*10000 + 0.5))
                "rate_int": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present_x2", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "window_sum": {"$sum": "$rate_int", "window": window},
                    "window_n": {"$sum": {"$cond": [{"$isNumber": "$rate_int"}, 1, 0]}, "window": window},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$present_x2", 2]},
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [{"$isNumber": "$rate_int"}, {"$divide": ["$rate_int", 10000]}, None]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$window_n", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": [2, "$window_sum"]}, "$window_n"]},
                                            {"$multiply": [2, "$window_n"]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: str,
):
    rows = list(db.attendance_logs.aggregate(trend_pipeline(department, from_, to)))
    return {"department": department, "items": rows}


# ------------------------------------------------------------------- admin

ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_RE)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        spec = attendance_find(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": spec["filter"],
            "projection": spec["projection"],
            "sort": dict(spec["sort"]),
            "skip": spec["skip"],
            "limit": spec["limit"],
        }
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            pipeline = monthly_pipeline(require(emp_code, "emp_code"), require(month, "month"))
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = summary_pipeline(require(month, "month"), department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = leaderboard_pipeline(require(month, "month"), limit, department)
        else:
            collection = "attendance_logs"
            pipeline = trend_pipeline(require(department, "department"), require(from_, "from"), require(to, "to"))
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    plan = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(plan)),
    }
