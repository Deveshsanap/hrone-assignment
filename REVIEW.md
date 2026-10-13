# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always returned `200 OK` even if the DB was offline. | Turn off MongoDB; it still says "ok". | Added `client.admin.command('ping')` and `try/except` to return `503`. |
| 2 | `compute_work_hours()` | Used Python's built-in `round()`, which applies Banker's rounding (rounds to nearest even number). | `round(2.5)` returns `2.0` instead of `3.0`, violating R8. | Replaced with `decimal.Decimal` and `ROUND_HALF_UP`. |
| 3 | `compute_late_minutes()` | Did not use floor division (`//`) for minute calculations. | `int(59.9)` vs true floor could drift on boundaries. | Used `//` to enforce strict floor division per rule R2. |
| 4 | `compute_overtime()` | Missed the rule that overtime under 30 minutes should be 0. | Punching out 10 minutes late yielded 10 overtime minutes instead of 0. | Added `if minutes >= 30 else 0` check per rule R3. |
| 5 | `create_employee()` & `punch_in()` | Used check-then-insert (`find_one` followed by `insert_one`), creating race conditions. | Sending two simultaneous POST requests creates duplicate records. | Created database unique indexes at startup and caught `DuplicateKeyError` to return `409`. |
| 6 | `punch_in()` timestamps | Used `datetime.now()` (local timezone) and did not truncate to seconds. | Saved dates drifted from IST; fractional seconds failed strict matching. | Used `datetime.now(IST)` and wrote a `truncate_to_seconds()` helper per R1. |
| 7 | `list_employees()` | Pagination math `skip = page * page_size` was off by one page. | Page 1 skipped the first 20 records. | Changed math to `skip = (page - 1) * page_size`. |
| 8 | `list_attendance()` | Fetched all records into Python memory to sort them (`list(db.find())`). | Application crashes on 100,000+ records due to OOM. | Removed Python list sorting; used MongoDB's native `.sort().skip().limit()`. |