# Attendance & Analytics API

A robust FastAPI backend for employee attendance tracking, regularization, and analytics backed by MongoDB.

## Features & Implementation Highlights
- **Full OpenAPI 3.0 Compliance**: Complete implementation of endpoints for employees, attendance logging, regularizations, leaderboard, department summaries, and moving averages.
- **Strict Business Rules (R1 - R10)**:
  - Exact whole-second instant truncation and timezone conversion (IST UTC+05:30).
  - Accurate overnight shift recognition (shifts spanning across midnight).
  - Decimal half-up rounding (`ROUND_HALF_UP`) for work hours and averages.
  - Strict grace period evaluation (> 10 minutes from shift start).
  - Overtime thresholding ($\ge 30$ minutes post shift end).
- **Concurrency & Idempotency**:
  - Atomic operations backed by MongoDB compound unique indexes to eliminate check-then-insert race conditions.
  - Returns HTTP 409 on duplicate punch-in attempts or duplicate employee codes.
- **Aggregation Pipelines**:
  - Optimized MongoDB aggregation pipelines for monthly department summaries, leaderboards with dense/competition rank handling, and 7-day moving averages.

## Prerequisites
- Python 3.10+
- MongoDB 6.0+ (Tested on MongoDB 7.0 / Atlas)

## Setup & Running Locally

1. Create and activate a virtual environment:
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment variables (e.g. `.env`):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

4. Seed initial data (optional):
```bash
python sample_seed.py
```

5. Start the application:
```bash
uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation will be available at `http://localhost:8000/docs`.

## Repository Structure
- `app/main.py`: Complete FastAPI application, models, routes, and business logic.
- `requirements.txt`: Python package requirements.
- `REVIEW.md`: Comprehensive review of starter defects, root causes, symptoms, and fixes.
- `DECISIONS.md`: Design rationale covering indexes, concurrency, ranking, and scalability.
- `README.md`: Setup and project overview.
