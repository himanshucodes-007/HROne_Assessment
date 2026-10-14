# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
Created `{ emp_code: 1 }` (unique) on `employees` to enforce identity uniqueness and prevent duplicate creations, and `{ department: 1, joined_on: 1 }` to back department filtering and date-bounded headcount queries. On `attendance_logs`, created `{ emp_code: 1, date: 1 }` (unique) to guarantee at most one log per employee per day and eliminate check-then-insert punch-in races; `{ date: -1, emp_code: 1 }` to serve paginated attendance listing and range queries with index sort; and `{ date: 1, late_minutes: 1 }` to accelerate monthly leaderboard aggregation. Considered a single-field index on `status` alone, but rejected it because low cardinality yields poor selectivity on 100k rows.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both threads validate input and calculate identical attendance dates (`YYYY-MM-DD`). Both attempt an atomic `insert_one` into MongoDB. The compound unique index on `(emp_code, date)` guarantees only one insertion commits (returning 201). The second request fails with MongoDB `DuplicateKeyError` (E11000), which the endpoint catches and converts into HTTP 409 Conflict without checking beforehand.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
The aggregation uses `$setWindowFields` with standard competition ranking (`$rank: {}`), skipping subsequent ranks on ties (e.g., 1, 2, 2, 4). The filtering stage matches `rank <= limit`. If a tie occurs at rank $L \le \text{limit}$, all tied employees are returned (exceeding `limit` total rows if necessary), sorted by `total_late_minutes` descending and tie-broken by `emp_code` ascending.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
The aggregation pipeline initiates from `employees` rather than `attendance_logs`. It filters active employees whose `joined_on <= end_of_month` and performs a `$lookup` into `attendance_logs` for that month, followed by `$unwind` with `preserveNullAndEmptyArrays: true`. Employees with zero logs remain intact with a null log record, ensuring the subsequent `$group` counts every joined employee in the department headcount.

5. **One thing you would change** if this had to serve 100x the data.
For 10,000,000+ attendance records, computing ad-hoc aggregations (department summaries and moving averages) across entire months on read requests would bottleneck CPU and I/O. I would implement an asynchronous daily aggregation rollup into a precomputed collection (or time-series bucketing) updated at end-of-day/punch-out, allowing analytics endpoints to execute instantaneous point/range reads over daily summary documents instead of scanning raw logs.
