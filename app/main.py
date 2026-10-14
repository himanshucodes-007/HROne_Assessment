"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field, field_validator
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime has tzinfo=timezone.utc, truncating microseconds to whole seconds (R1)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime with whole-second precision."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Round a float using half-up rounding (R4, R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("0." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    return (eh_h, eh_m) <= (sh_h, sh_m)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shifts: a punch-in whose IST time is earlier than shift_end belongs to the shift
    that started the previous day.
    """
    punch_ist = punch_in_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        eh_h, eh_m = map(int, shift_end.split(":"))
        if punch_ist.time() < time(eh_h, eh_m, 0):
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 (600 seconds) after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    punch_ist = punch_in_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(att_date_str)
    shift_start_dt = datetime.combine(att_date, time(sh_h, sh_m, 0), tzinfo=IST)
    diff_seconds = (punch_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(diff_seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end is on the next calendar day.
    """
    punch_ist = punch_out_utc.astimezone(IST)
    eh_h, eh_m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(att_date_str)
    if is_overnight_shift(shift_start, shift_end):
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date
    shift_end_dt = datetime.combine(end_date, time(eh_h, eh_m, 0), tzinfo=IST)
    diff_seconds = (punch_ist - shift_end_dt).total_seconds()
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def serialize_history_entry(h: Dict[str, Any]) -> Dict[str, Any]:
    entry = {
        "at": dt_to_epoch_ms(h.get("at")),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": {},
    }
    changes = h.get("changes", {})
    for k, v in changes.items():
        ch_from = v.get("from")
        ch_to = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(ch_from, datetime):
                ch_from = dt_to_epoch_ms(ch_from)
            if isinstance(ch_to, datetime):
                ch_to = dt_to_epoch_ms(ch_to)
        entry["changes"][k] = {"from": ch_from, "to": ch_to}
    return entry


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Lifespan & App Setup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes on startup
    db.employees.create_index([("emp_code", pymongo.ASCENDING)], unique=True)
    db.employees.create_index([("department", pymongo.ASCENDING)])
    db.attendance_logs.create_index([("emp_code", pymongo.ASCENDING), ("date", pymongo.ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)])
    db.attendance_logs.create_index([("status", pymongo.ASCENDING)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info):
        start = info.data.get("shift_start")
        if start and v == start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


# --------------------------------------------------------------------------- #
# Endpoints provided (Fixed)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {e}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created_now = ensure_utc(datetime.now(timezone.utc))
    doc["created_at"] = created_now
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", pymongo.ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_dt(body.punched_at)
    else:
        ts_utc = ensure_utc(datetime.now(timezone.utc))

    att_date = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts_utc, emp["shift_start"], emp["shift_end"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Part B Models
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Part B Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_dt(body.punched_at)
    else:
        ts_utc = ensure_utc(datetime.now(timezone.utc))

    # Find the most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_utc}},
        sort=[("punch_in", pymongo.DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "No punch-in found for employee")

    # If already punched out, 409
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # 422 if punched_at <= punch_in or more than 24 hours after it
    diff_sec = (ts_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(422, "Punch-out time must be after punch-in and within 24 hours")

    work_hrs = compute_work_hours(punch_in_utc, ts_utc)
    is_half = work_hrs < 4.50
    ot_mins = compute_overtime(ts_utc, emp["shift_start"], emp["shift_end"], record["date"])

    # Concurrency safe: only update if punch_out is still None
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": is_half,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not result:
        # Another concurrent request punched out first
        raise HTTPException(409, "Record is already punched out")

    return serialize_attendance_doc(result)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for given date")

    # Status ABSENT or LEAVE clears both punch times: supplying punch_in or punch_out with it is 422
    target_status = body.status if body.status is not None else record["status"]
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out when setting status ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        if final_punch_in.tzinfo is None:
            final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        # punch_in must stay on record's attendance date
        computed_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            if final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_half_day = final_work_hours < 4.50
            final_overtime_minutes = compute_overtime(
                final_punch_out, emp["shift_start"], emp["shift_end"], date
            )
        else:
            final_work_hours = None
            final_half_day = False
            final_overtime_minutes = 0

        final_late_minutes = compute_late_minutes(
            final_punch_in, emp["shift_start"], emp["shift_end"], date
        )

    # Detect changes
    changes = {}
    if target_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": target_status}

    old_pi = record.get("punch_in")
    if old_pi is not None and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    if old_pi != final_punch_in:
        changes["punch_in"] = {"from": old_pi, "to": final_punch_in}

    old_po = record.get("punch_out")
    if old_po is not None and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)
    if old_po != final_punch_out:
        changes["punch_out"] = {"from": old_po, "to": final_punch_out}

    if record.get("work_hours") != final_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": final_work_hours}

    if record.get("late_minutes", 0) != final_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": final_late_minutes}

    if record.get("overtime_minutes", 0) != final_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": record.get("overtime_minutes", 0),
            "to": final_overtime_minutes,
        }

    if record.get("half_day", False) != final_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": final_half_day}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    now_utc = ensure_utc(datetime.now(timezone.utc))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update with history length condition for race-safety
    current_history_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": current_history_len},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not result:
        raise HTTPException(409, "Concurrent modification detected")

    return serialize_attendance_doc(result)


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    d = date.fromisoformat(start_date_str)
    end_d = date.fromisoformat(end_date_str)
    working_days = 0
    while d <= end_d:
        if d.weekday() < 5:  # Mon-Fri
            working_days += 1
        d += timedelta(days=1)
    return working_days


def get_month_dates(month: str):
    import calendar
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"
    return start_date, end_date


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    start_date, end_date = get_month_dates(month)
    joined_on = emp["joined_on"]

    # R7: working days counted from joined_on for mid-month joiners
    if joined_on > end_date:
        effective_start = end_date
        working_days = 0
    else:
        effective_start = max(start_date, joined_on)
        working_days = count_working_days(effective_start, end_date)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late = int(agg["total_late_minutes"])
        total_ot = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    start_date, end_date = get_month_dates(month)
    match_emp = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "logs": 1,
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "emp_present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "emp_late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "emp_total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "emp_leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "emp_on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "all_work_hours": {"$push": "$valid_work_hours"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "flattened_hours": {
                                "$reduce": {
                                    "input": "$all_work_hours",
                                    "initialValue": [],
                                    "in": {"$concatArrays": ["$$value", "$$this"]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$flattened_hours"}, 0]},
                                {"$avg": "$$flattened_hours"},
                                None,
                            ]
                        },
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for doc in results:
        avg_wh = doc.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": float(doc["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count": int(doc["leave_count"]),
            "on_duty_count": int(doc["on_duty_count"]),
        })
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    start_date, end_date = get_month_dates(month)
    match_stage = {
        "date": {"$gte": start_date, "$lte": end_date},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {"rank": {"$lte": limit}}
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for doc in results:
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        })
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, date_from: str, date_to: str):
    start_dt = datetime.fromisoformat(f"{date_from}T00:00:00+00:00")
    end_dt_bound = datetime.fromisoformat(f"{date_to}T00:00:00+00:00") + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date_dt": start_dt,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt_bound],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "e",
                        }
                    },
                    {"$unwind": "$e"},
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$e.department", department]},
                                    {"$eq": ["$date", "$$day"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "att",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$att.present_count"}, 0.0]},
                "late_count": {"$ifNull": [{"$first": "$att.late_count"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if to_date < from_date:
        raise HTTPException(422, "'to' date cannot be before 'from' date")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "Range exceeds 92 days")

    # Check if department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in results:
        rate = doc.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(rate, 4)
        mavg = doc.get("moving_avg_7d")
        if mavg is not None:
            mavg = round_half_up(mavg, 4)

        items.append({
            "date": doc["date"],
            "is_working_day": doc["is_working_day"],
            "headcount": int(doc["headcount"]),
            "present_count": float(doc["present_count"]),
            "late_count": int(doc["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": mavg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C: Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        plan = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        start_date, end_date = get_month_dates(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {
                "$project": {
                    "status": 1,
                    "late_minutes": 1,
                    "overtime_minutes": 1,
                    "half_day": 1,
                    "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        plan = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        plan = db.command(cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        plan = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        if to_date < from_date:
            raise HTTPException(422, "'to' date cannot be before 'from' date")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        plan = db.command(cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    else:
        raise HTTPException(422, f"Unknown endpoint: {endpoint}")

