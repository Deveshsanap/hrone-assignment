"""
Employee Attendance & Analytics API
"""
import os
import calendar
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, ConnectionFailure

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Unique constraints to prevent duplicate data/race conditions
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    
    # 2. Performance indexes for list and analytics endpoints
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.employees.create_index("department")
    yield
    client.close()

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# Helpers (Repaired)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds."""
    return dt.replace(microsecond=0)

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """R2: Grace period 10 mins. Floor rounding."""
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() // 60) # // forces floor division
    return minutes if minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: 2 decimals half-up."""
    secs = (punch_out - punch_in).total_seconds()
    val = Decimal(str(secs)) / Decimal("3600")
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: Floor rounding, minimum 30 mins. R1: Overnight shift handling."""
    eh, em = map(int, shift_end.split(":"))
    end_time = datetime.fromisoformat(date_str).replace(hour=eh, minute=em, tzinfo=IST)
    
    # If overnight shift, the shift ends on the next calendar day
    if shift_end <= shift_start:
        end_time += timedelta(days=1)
        
    minutes = int((punch_out - end_time).total_seconds() // 60)
    return minutes if minutes >= 30 else 0
def datetime_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    """Helper to strictly conform to API epoch millisecond requirements."""
    if not isinstance(dt, datetime):
        return dt
    return int(dt.timestamp() * 1000)

def format_attendance_doc(doc: dict) -> dict:
    """Formats MongoDB BSON datetimes into integer epoch milliseconds for the API."""
    if not doc: return doc
    doc.pop("_id", None)
    
    doc["punch_in"] = datetime_to_epoch(doc.get("punch_in"))
    doc["punch_out"] = datetime_to_epoch(doc.get("punch_out"))
    
    for h in doc.get("history", []):
        h["at"] = datetime_to_epoch(h.get("at"))
        for field in ["punch_in", "punch_out"]:
            if field in h.get("changes", {}):
                h["changes"][field]["from"] = datetime_to_epoch(h["changes"][field]["from"])
                h["changes"][field]["to"] = datetime_to_epoch(h["changes"][field]["to"])
    return doc

def get_working_days(month_str: str, joined_on_str: str) -> int:
    """R7: Calculates working days (Mon-Fri) for a given month."""
    y, m = map(int, month_str.split("-"))
    jy, jm, jd = map(int, joined_on_str.split("-"))
    
    start_date = date(y, m, 1)
    if jy == y and jm == m:
        start_date = date(jy, jm, jd) # Joined mid-month
    elif date(jy, jm, jd) > date(y, m, calendar.monthrange(y, m)[1]):
        return 0 # Joined after this month
        
    end_date = date(y, m, calendar.monthrange(y, m)[1])
    
    days = 0
    curr = start_date
    while curr <= end_date:
        if curr.weekday() < 5: # 0-4 represents Monday to Friday
            days += 1
        curr += timedelta(days=1)
    return days

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"
class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
class RegularizeRequest(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
# --------------------------------------------------------------------------- #
# Endpoints provided (Repaired)
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command('ping')
        return {"status": "ok"}
    except ConnectionFailure:
        raise HTTPException(status_code=503, detail="Database unavailable")

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return doc

@app.get("/employees", tags=["employees"])
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    
    # Do not sort in memory. Use MongoDB's native .sort() and .skip().limit()
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at:
        ts = truncate_to_seconds(datetime.fromtimestamp(body.punched_at / 1000, tz=IST))
    else:
        ts = truncate_to_seconds(datetime.now(IST))
        
    # R1: Overnight shift logic for the attendance date
    d = ts.date()
    if emp["shift_end"] <= emp["shift_start"]:
        shift_end_time = ts.replace(hour=int(emp["shift_end"].split(":")[0]), 
                                    minute=int(emp["shift_end"].split(":")[1]))
        if ts < shift_end_time:
            d -= timedelta(days=1)
            
    date_str = d.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
        
    doc.pop("_id", None)
    return doc

@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

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
    total = db.attendance_logs.count_documents(q)
    
    # Use database sorting to prevent memory crashes on 100k docs
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    return {"items": list(cursor), "total": total, "page": page, "page_size": page_size}
@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    # 1. Verify employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # 2. Parse the timestamp
    if body.punched_at:
        ts = truncate_to_seconds(datetime.fromtimestamp(body.punched_at / 1000, tz=IST))
    else:
        ts = truncate_to_seconds(datetime.now(IST))

    # 3. Find the most recent record where punch_in <= punch_out time
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)]
    )

    if not record:
        raise HTTPException(404, "No matching punch-in found")
        
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    # 4. Validations according to openapi.yaml
    if ts <= record["punch_in"]:
        raise HTTPException(422, "punch_out must be after punch_in")
        
    if (ts - record["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # 5. Business logic calculations
    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hours < 4.50

    # 6. Atomic Update (Prevents race conditions)
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None}, 
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day
        }},
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        # If null, another thread snuck in and updated it just before us
        raise HTTPException(409, "Record is already punched out")

    return format_attendance_doc(updated_doc)
    # Note: For punch_in it will be: return format_attendance_doc(doc)

@app.patch("/attendance/{emp_code}/{date}", status_code=200, tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    # 1. Look up employee and current record
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Record not found")

    # 2. Determine new raw values
    new_status = body.status if body.status else record["status"]
    
    new_in = record["punch_in"]
    if body.punch_in is not None:
        new_in = truncate_to_seconds(datetime.fromtimestamp(body.punch_in / 1000, tz=IST))
        
    new_out = record["punch_out"]
    if body.punch_out is not None:
        new_out = truncate_to_seconds(datetime.fromtimestamp(body.punch_out / 1000, tz=IST))

    # 3. Rule Validations
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch times for ABSENT/LEAVE")
        new_in = None
        new_out = None
    else:
        if not new_in:
            raise HTTPException(422, "Presence status requires a punch_in")

    if new_in:
        # R1: Ensure punch_in stays on the correct attendance date
        shift_d = new_in.date()
        if emp["shift_end"] <= emp["shift_start"]:
            shift_end_time = new_in.replace(hour=int(emp["shift_end"].split(":")[0]), 
                                            minute=int(emp["shift_end"].split(":")[1]))
            if new_in < shift_end_time:
                shift_d -= timedelta(days=1)
        if shift_d.isoformat() != date:
            raise HTTPException(422, "punch_in does not match the record's attendance date")

    if new_in and new_out:
        if new_out <= new_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_out - new_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out cannot be more than 24h after punch_in")

    # 4. Recalculate Derived Fields
    new_work_hours = compute_work_hours(new_in, new_out) if new_in and new_out else None
    new_late = compute_late_minutes(new_in, emp["shift_start"]) if new_in else 0
    new_overtime = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date) if new_out else 0
    new_half = (new_work_hours < 4.50) if new_work_hours is not None else False

    if new_status in ["ABSENT", "LEAVE"]:
        new_late = 0
        new_overtime = 0
        new_half = False

    # 5. Build the changes dictionary (only record what actually changed)
    proposed_state = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half
    }
    
    changes = {}
    for key, val in proposed_state.items():
        old_val = record.get(key)
        # Handle default legacy fields that might be missing
        if key == "half_day" and old_val is None: old_val = False
        if key in ["late_minutes", "overtime_minutes"] and old_val is None: old_val = 0
            
        if old_val != val:
            changes[key] = {"from": old_val, "to": val}

    if not changes:
        raise HTTPException(422, "No fields changed")

    # 6. Build the history entry
    now = truncate_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    # 7. Atomic update (Optimistic Locking using history array size)
    history_len = len(record.get("history", []))
    
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"], 
            # Ensure no one else has pushed a history item while we were computing!
            f"history.{history_len}": {"$exists": False} 
        },
        {
            "$set": proposed_state,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "Concurrent modification detected, please retry")

    return format_attendance_doc(updated_doc)
    # Note: For punch_in it will be: return format_attendance_doc(doc)

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str):
    # 1. Check employee
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # 2. Get the working days denominator
    working_days = get_working_days(month, emp["joined_on"])

    # 3. MongoDB Aggregation Pipeline
    pipeline = [
        # Stage 1: Filter to just this employee and this month (e.g., '2026-07-...')
        {"$match": {
            "emp_code": emp_code,
            "date": {"$regex": f"^{month}"}
        }},
        # Stage 2: Determine if the day is a weekday (2-6 in MongoDB dayOfWeek where 1=Sun)
        {"$addFields": {
            "is_weekday": {
                "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]
            }
        }},
        # Stage 3: Roll up the sums
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                        {"$cond": ["$half_day", 0.5, 1]},
                        0
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
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    
    # Setup default values in case they have 0 logs
    stats = result[0] if result else {
        "present_days": 0, "leave_days": 0, "late_count": 0, 
        "total_late_minutes": 0, "total_overtime_minutes": 0
    }

    # 4. R8: Calculate percentage with half-up rounding
    attendance_pct = None
    if working_days > 0:
        val = Decimal(str(stats["present_days"])) / Decimal(str(working_days)) * 100
        attendance_pct = float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(stats["present_days"]),
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }
@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: str, department: Optional[str] = None):
    # 1. Base filter for employees
    emp_match = {}
    if department:
        emp_match["department"] = department
        
    # 2. Calculate the end of the month for the Headcount rule (R9)
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    period_end = f"{month}-{last_day:02d}"

    # 3. The Aggregation Pipeline (A SQL-like LEFT JOIN + GROUP BY)
    pipeline = [
        # Stage 1: Filter employees by department (if provided)
        {"$match": emp_match},
        
        # Stage 2: R9 - Only include employees who joined on or before the end of this month
        {"$match": {"joined_on": {"$lte": period_end}}},
        
        # Stage 3: LEFT JOIN attendance_logs
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "let": {"month_regex": f"^{month}"},
            "pipeline": [
                # Only grab logs for the requested month
                {"$match": {
                    "$expr": {"$regexMatch": {"input": "$date", "regex": "$$month_regex"}}
                }},
                # Pre-calculate weekday and presence flags
                {"$addFields": {
                    "is_weekday": {
                        "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]
                    },
                    "is_present": {
                        "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                    }
                }}
            ],
            "as": "logs"
        }},
        
        # Stage 4: Flatten the array so we can process individual logs, 
        # but KEEP employees with empty logs (preserveNullAndEmptyArrays = LEFT JOIN behavior)
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        
        # Stage 5: Group by Department and roll up the math
        {"$group": {
            "_id": "$department",
            "unique_emps": {"$addToSet": "$emp_code"}, # Used for accurate headcount
            
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$logs.is_present", "$logs.is_weekday"]},
                        {"$cond": ["$logs.half_day", 0.5, 1]},
                        0
                    ]
                }
            },
            
            # Collect data for average work hours (only present records with a work_hours value)
            "work_hours_sum": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$logs.is_present", {"$ne": ["$logs.work_hours", None]}]},
                        "$logs.work_hours",
                        0
                    ]
                }
            },
            "work_hours_count": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$logs.is_present", {"$ne": ["$logs.work_hours", None]}]},
                        1, 
                        0
                    ]
                }
            },
            
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        
        # Stage 6: Sort alphabetically by department name
        {"$sort": {"_id": 1}}
    ]

    result = list(db.employees.aggregate(pipeline))
    
    # 4. Final Python formatting and Rounding
    items = []
    for doc in result:
        headcount = len(doc["unique_emps"])
        if headcount == 0:
            continue # API contract states to omit departments with 0 headcount
            
        avg_wh = None
        if doc["work_hours_count"] > 0:
            val = Decimal(str(doc["work_hours_sum"])) / Decimal(str(doc["work_hours_count"]))
            avg_wh = float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            
        items.append({
            "department": doc["_id"],
            "headcount": headcount,
            "present_days": float(doc["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": doc["late_count"],
            "total_late_minutes": doc["total_late_minutes"],
            "leave_count": doc["leave_count"],
            "on_duty_count": doc["on_duty_count"]
        })

    return {"month": month, "items": items}
@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    pipeline = [
        # Stage 1: Filter to logs in this month where the employee was actually late
        {"$match": {
            "date": {"$regex": f"^{month}"},
            "late_minutes": {"$gt": 0}
        }},
        
        # Stage 2: Group by employee to sum up their tardiness
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        
        # Stage 3: JOIN with employees collection to get names and departments
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp_info"
        }},
        
        # Stage 4: Flatten the array (ignore logs if the employee was deleted/doesn't exist)
        {"$unwind": "$emp_info"}
    ]

    # Stage 5: Apply department filter if requested
    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline.extend([
     # Stage 6: The Window Function (SQL RANK() equivalent)
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {
                    "$rank": {}
                }
            }
        }}, 
        
        # Stage 7: Filter by rank AFTER the ranking is calculated
        {"$match": {"rank": {"$lte": limit}}},
        
        # Stage 8: Sort for final output
        {"$sort": {"rank": 1, "_id": 1}},
        
        # Stage 9: Format the keys to match the API contract exactly
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp_info.name",
            "department": "$emp_info.department",
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}
@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str, 
    from_date: str = Query(alias="from"), 
    to_date: str = Query(alias="to")
):
    # 1. Validations
    if from_date > to_date:
        raise HTTPException(422, "from cannot be after to")
        
    f_dt = datetime.fromisoformat(from_date)
    t_dt = datetime.fromisoformat(to_date)
    if (t_dt - f_dt).days > 92:
        raise HTTPException(422, "Range exceeds 92 days")
        
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")

    # 2. Aggregation Pipeline
    pipeline = [
        # Stage 1: Anchor on ONE employee from this department just to have a document to densify
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "dummy": {"$literal": 1}}},
        
        # Stage 2: $densify creates a new document for EVERY single day in our range
        {"$densify": {
            "field": "date",
            "range": {
                "step": 1,
                "unit": "day",
                # Bounds are [inclusive, exclusive), so we add 1 day to the end
                "bounds": [f_dt, t_dt + timedelta(days=1)]
            }
        }},
        {"$match": {"date": {"$ne": None}}},
        # Stage 3: Format dates and check for Mon-Fri
        {"$addFields": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]}
        }},
        
        # Stage 4: Lookup Headcount (employees joined on or before this specific day)
        {"$lookup": {
            "from": "employees",
            "let": {"curr_date": "$date_str"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$curr_date"]}
                        ]
                    }
                }}
            ],
            "as": "active_emps"
        }},
        {"$addFields": {"headcount": {"$size": "$active_emps"}}},
        
        # Stage 5: Lookup Attendance Logs for this department on this specific day
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"curr_date": "$date_str"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$curr_date"]}}},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp_info"
                }},
                {"$unwind": "$emp_info"},
                {"$match": {"emp_info.department": department}}
            ],
            "as": "logs"
        }},
        
        # Stage 6: Roll up Present Count and Late Count
        {"$addFields": {
            "present_count": {
                "$sum": {
                    "$map": {
                        "input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}}},
                        "as": "p",
                        "in": {"$cond": ["$$p.half_day", 0.5, 1]}
                    }
                }
            },
            "late_count": {
                "$size": {
                    "$filter": {"input": "$logs", "as": "l", "cond": {"$gt": ["$$l.late_minutes", 0]}}
                }
            }
        }},
        
        # Stage 7: Calculate raw Attendance Rate
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        
        # Stage 8: Window Function to calculate the 7-day moving average
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    # Look at current document (0) and up to 6 previous documents (-6)
                    "window": {"documents": [-6, 0]}
                }
            }
        }}
    ]

    result = list(db.employees.aggregate(pipeline))
    
    # 3. Final Python Half-Up Rounding for 4 Decimals
    items = []
    for doc in result:
        rate = None
        if doc.get("attendance_rate") is not None:
            rate = float(Decimal(str(doc["attendance_rate"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
            
        m_avg = None
        if doc.get("moving_avg_7d") is not None:
            m_avg = float(Decimal(str(doc["moving_avg_7d"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
            
        items.append({
            "date": doc["date_str"],
            "is_working_day": doc["is_working_day"],
            "headcount": doc["headcount"],
            "present_count": float(doc["present_count"]),
            "late_count": doc["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": m_avg
        })

    return {"department": department, "items": items}
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        
        # Run explain on the find query
        explanation = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).explain()
        collection = "attendance_logs"
        
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}}
        ]
        # Run explain on the aggregate query
        explanation = db.command('aggregate', 'attendance_logs', pipeline=pipeline, explain=True)
        collection = "attendance_logs"
        
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        emp_match = {}
        if department:
            emp_match["department"] = department
        pipeline = [{"$match": emp_match}]
        explanation = db.command('aggregate', 'employees', pipeline=pipeline, explain=True)
        collection = "employees"
        
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}}
        ]
        explanation = db.command('aggregate', 'attendance_logs', pipeline=pipeline, explain=True)
        collection = "attendance_logs"
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to required")
        pipeline = [
            {"$match": {"department": department}}
        ]
        explanation = db.command('aggregate', 'employees', pipeline=pipeline, explain=True)
        collection = "employees"
        
    else:
        raise HTTPException(422, "Unknown endpoint")

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explanation
    }
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
