from flask import Flask, render_template, request, jsonify
import sqlite3
import datetime

app = Flask(__name__)
DB_NAME = "payroll_system.db"
STANDARD_START_TIME = datetime.time(9, 0)  # 09:00 AM

# ==========================================
# DATABASE INITIALIZATION
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Employees Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS employees (
        emp_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        department TEXT NOT NULL,
        monthly_salary REAL NOT NULL
    )
    """)

    # Attendance Log Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS attendance_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        status TEXT CHECK(status IN ('Present', 'Absent')),
        check_in_time TEXT,
        arrival_status TEXT CHECK(arrival_status IN ('In Time', 'Late', 'N/A')),
        minutes_late INTEGER DEFAULT 0,
        FOREIGN KEY (emp_id) REFERENCES employees(emp_id),
        UNIQUE(emp_id, date)
    )
    """)

    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# WEB ROUTES & API ENDPOINTS
# ==========================================
@app.route("/")
def index():
    return render_template("index.html")

# 1. Fetch All Employees
@app.route("/api/employees", methods=["GET"])
def get_employees():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM employees ORDER BY name ASC")
    employees = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(employees)

# 2. Add New Employee
@app.route("/api/employees", methods=["POST"])
def add_employee():
    data = request.get_json()
    name = data.get("name", "").strip()
    department = data.get("department", "").strip()
    salary = data.get("monthly_salary", 0)

    if not name or not department or float(salary) <= 0:
        return jsonify({"success": False, "error": "Invalid employee details"}), 400

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO employees (name, department, monthly_salary) VALUES (?, ?, ?)",
            (name, department, float(salary))
        )
        conn.commit()
        emp_id = cursor.lastrowid
        conn.close()
        return jsonify({"success": True, "emp_id": emp_id, "message": "Employee registered successfully"})
    except sqlite3.IntegrityError:
        return jsonify({"success": False, "error": "Employee with this name already exists"}), 400

# 3. Mark Daily Attendance (with Automatic Late Detection)
@app.route("/api/attendance", methods=["POST"])
def mark_attendance():
    data = request.get_json()
    emp_id = data.get("emp_id")
    date_str = data.get("date", datetime.date.today().strftime("%Y-%m-%d"))
    status = data.get("status") # 'Present' or 'Absent'
    check_in_str = data.get("check_in_time", "") # Format: 'HH:MM' (24-hr)

    arrival_status = "N/A"
    minutes_late = 0
    formatted_time = "N/A"

    if status == "Present":
        if not check_in_str:
            return jsonify({"success": False, "error": "Check-in time required for present employees"}), 400

        try:
            # Parse check-in time (HTML5 time input delivers HH:MM 24-hr format)
            parsed_time = datetime.datetime.strptime(check_in_str, "%H:%M").time()
            formatted_time = parsed_time.strftime("%I:%M %p")

            # Calculate time difference relative to 09:00 AM
            ref_start = datetime.datetime.combine(datetime.date.today(), STANDARD_START_TIME)
            actual_checkin = datetime.datetime.combine(datetime.date.today(), parsed_time)
            time_diff = (actual_checkin - ref_start).total_seconds() / 60.0

            if time_diff > 0:
                arrival_status = "Late"
                minutes_late = int(time_diff)
            else:
                arrival_status = "In Time"
                minutes_late = 0
        except ValueError:
            return jsonify({"success": False, "error": "Invalid time format"}), 400
    else:
        status = "Absent"
        formatted_time = "N/A"
        arrival_status = "N/A"
        minutes_late = 0

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO attendance_log (emp_id, date, status, check_in_time, arrival_status, minutes_late)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(emp_id, date) DO UPDATE SET
            status = excluded.status,
            check_in_time = excluded.check_in_time,
            arrival_status = excluded.arrival_status,
            minutes_late = excluded.minutes_late
    """, (emp_id, date_str, status, formatted_time, arrival_status, minutes_late))

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "status": status,
        "arrival_status": arrival_status,
        "minutes_late": minutes_late,
        "check_in_time": formatted_time
    })

# 4. Generate Monthly Report & Deduction Calculations
@app.route("/api/report/<int:emp_id>/<month>", methods=["GET"])
def get_monthly_report(emp_id, month):
    # month format: 'YYYY-MM'
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM employees WHERE emp_id = ?", (emp_id,))
    employee = cursor.fetchone()

    if not employee:
        conn.close()
        return jsonify({"success": False, "error": "Employee not found"}), 404

    cursor.execute("""
        SELECT date, status, check_in_time, arrival_status, minutes_late
        FROM attendance_log
        WHERE emp_id = ? AND strftime('%Y-%m', date) = ?
        ORDER BY date ASC
    """, (emp_id, month))
    
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()

    present_on_time = sum(1 for r in records if r["status"] == "Present" and r["arrival_status"] == "In Time")
    present_late = sum(1 for r in records if r["status"] == "Present" and r["arrival_status"] == "Late")
    absent_count = sum(1 for r in records if r["status"] == "Absent")
    total_minutes_late = sum(r["minutes_late"] for r in records)

    base_salary = float(employee["monthly_salary"])
    per_day_salary = base_salary / 30.0

    # Deduction Rules: Absent = 1.0 day rate, Late = 0.5 day rate
    deduction_absent = round(absent_count * per_day_salary, 2)
    deduction_late = round(present_late * (per_day_salary * 0.5), 2)
    total_deductions = round(deduction_absent + deduction_late, 2)
    net_payable = max(0.0, round(base_salary - total_deductions, 2))

    return jsonify({
        "success": True,
        "employee": dict(employee),
        "month": month,
        "records": records,
        "stats": {
            "total_logged": len(records),
            "present_on_time": present_on_time,
            "present_late": present_late,
            "absent": absent_count,
            "total_minutes_late": total_minutes_late
        },
        "financials": {
            "base_salary": base_salary,
            "per_day_salary": round(per_day_salary, 2),
            "deduction_absent": deduction_absent,
            "deduction_late": deduction_late,
            "total_deductions": total_deductions,
            "net_payable": net_payable
        }
    })

if __name__ == "__main__":
    app.run(debug=True, port=5000)