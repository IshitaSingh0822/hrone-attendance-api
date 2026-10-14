# Employee Attendance & Analytics API

A FastAPI + MongoDB service for employee punch-in/punch-out, manual attendance corrections with an audit history, and aggregation-based analytics. All application code is in `app/main.py`.

## Features
- Employees: create (validated, unique code) and list (paged, filter by department)
- Attendance: punch-in, punch-out, list; IST business dates, overnight shifts, late, overtime, work hours, half day
- Corrections: `PATCH /attendance/{emp_code}/{date}` recomputes derived fields and appends a history entry
- Analytics (MongoDB aggregation): employee monthly summary, department summary, late leaderboard, department daily trend
- `GET /admin/explain/{endpoint}` returns `executionStats` for each main query
- `GET /health` pings MongoDB (503 when unavailable)

## Stack
Python 3.11+, FastAPI, PyMongo (sync driver), MongoDB 6.0+ (tested target: 7.0)

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```
MongoDB, locally with Docker: `docker run -d -p 27017:27017 mongo:7`. An Atlas free tier cluster also works.

## Configuration
| Variable | Meaning | Example |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

Set them in the environment or in a local, uncommitted `.env` file. Never commit real connection strings.

## Run
```bash
python sample_seed.py                      # optional: load the sample data
uvicorn app.main:app --port 8000 --reload
```
Swagger UI: http://localhost:8000/docs

## Tests
```bash
pip install -r requirements-dev.txt
pytest tests/test_rules.py     # pure logic and validation, no database needed
pytest tests                   # also runs integration tests against MongoDB (database `attendance_test`)
```
The integration tests use their own throwaway database and are skipped when MongoDB is unreachable.

## Known limitations
- The trend's per-day headcount is computed from a list of join dates passed into the pipeline, which is fine for departments of thousands but not millions.
- The trend's 7-day average is taken over the published (4-decimal rounded) daily rates.
- PATCH and punch-out use compare-and-set updates: the loser of a concurrent change gets 409 and must retry.
- Tested with `pytest tests` against MongoDB 7.0 (35 tests passing).
