# REVIEW.md

Defects found in the starter `app/main.py`, and how each was fixed in the submitted version.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` at import | No `tz_aware`, so datetimes come back naive and mixing them with aware ones fails | `TypeError: can't subtract offset-naive and offset-aware datetimes` on punch-out | `tz_aware=True` and UTC everywhere; IST only for business-date logic |
| 2 | `/health` | Always returns ok, never talks to MongoDB | Stop MongoDB: still 200 | Pings MongoDB, returns 503 on failure |
| 3 | `compute_late_minutes` | Truncates to whole minutes, then tests `> 10`, so 09:40:01 gives 10 and is treated as on time | Punch in at 09:40:01 expecting 10 late minutes, get 0 | Compare seconds against 600, then floor to minutes |
| 4 | `compute_late_minutes` | Builds the shift start from the punch-in's own date and timezone, ignoring IST and overnight shifts | Overnight employee punching in after midnight, or any non-IST host | Shift start is built on the R1 attendance date in IST |
| 5 | `compute_work_hours` | Python `round()` is not half-up and floats mis-round ties; seconds are not truncated | 9h 0m 18s should be 9.01 | Integer arithmetic on whole seconds, half-up |
| 6 | `compute_overtime` | No 30-minute minimum, wrong end date for overnight shifts, naive/aware subtraction | 29 minutes past shift end still counted; overnight gives huge or negative values | 30-minute threshold, end on the next day for overnight, aware datetimes |
| 7 | `create_employee` | Check-then-insert race and no unique index | Two parallel creates both return 201 | Unique index on `emp_code`; `DuplicateKeyError` becomes 409 |
| 8 | `EmployeeIn` / `create_employee` | No validation (code pattern, equal shifts, impossible dates); `created_at` is naive local time returned as an ISO string | `EMP1`, `2026-02-30`, equal shifts all accepted | Pydantic patterns and validators; `created_at` stored in UTC, returned as epoch ms |
| 9 | `list_employees` | `skip = page * page_size` drops the first page; `total` ignores the department filter; no sort; unbounded `page_size` | Page 1 skips the first rows; total wrong when filtered | `(page - 1) * page_size`, count with the same filter, sort by `emp_code`, bounds on paging |
| 10 | `punch_in` | Unknown employee gives `None["shift_start"]`, a 500 | POST with a made-up `emp_code` | 404 |
| 11 | `punch_in` | `if body.punched_at` treats 0 as missing; local server time instead of IST for the date; floats, numeric strings and seconds accepted; `status` is any string | Float or seconds-looking timestamps accepted | Strict epoch-ms type with range, `is not None`, IST date per R1 with overnight rule, status restricted to presence values |
| 12 | `punch_in` | Check-then-insert race | Two simultaneous punch-ins both return 201 | Unique `(emp_code, date)` index, 409 on `DuplicateKeyError` |
| 13 | `punch_in` response | Returns `_id` as `id` and datetimes as ISO strings | Response does not match the `AttendanceRecord` schema | One serializer that emits contract fields and epoch ms |
| 14 | `list_attendance` | Loads the whole collection, sorts and paginates in Python, no `emp_code` tiebreak, no 422 for `date_from > date_to` or bad status, leaks `_id`, fails on records missing `history` or `half_day` | Slow on large data; wrong order within a date; missing-field crash | `find` with filter, sort, skip and limit in MongoDB, validation, defaults in the serializer |
| 15 | Startup | No indexes at all | `explain` shows `COLLSCAN` | Indexes created in the lifespan hook (retried from `/health`) |
| 16 | All endpoints | Database errors surface as raw 500s | Stop MongoDB mid-request | Handler returns 503 with a generic message, details only in logs |

## Looked at, not a defect

- Endpoints are plain `def` with synchronous PyMongo: FastAPI runs them in a threadpool, so they do not block the event loop.
- Dates are stored as `YYYY-MM-DD` strings: they compare correctly as strings and keep range queries index-friendly.
- Status codes for the happy paths (201 for creates, 200 for reads) already match the contract.
