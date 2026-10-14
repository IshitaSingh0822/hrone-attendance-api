# DECISIONS.md

1. **Indexes.** `employees.emp_code` (unique) stops duplicate employees and serves lookups; `employees(department, emp_code)` serves the filtered, sorted list and the trend's department lookup; `employees.joined_on` serves the summary's headcount match. On `attendance_logs`: `(emp_code, date)` unique gives one record per day and serves PATCH, monthly and trend; `(date desc, emp_code)` serves the list ordering, summary and leaderboard; `(status, date desc, emp_code)` serves status filters; `(emp_code, punch_in desc)` finds the latest punch-in for punch-out. Rejected: copying `department` into every log, because it goes stale when an employee moves.

2. **Punch-in race.** Both requests pass the employee check and both call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError` and returns 409. There is no check-then-insert.

3. **Ties.** The leaderboard ranks with `$rank` first and applies the limit afterwards (`rank <= limit`), so everyone tied at the cutoff is returned, even if that is more than `limit` rows.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s their logs, so someone with no logs still produces a row and is counted in `$group`.

5. **At 100x.** I would stop scanning raw logs for monthly analytics and keep a per-employee, per-month rollup updated on each write.
