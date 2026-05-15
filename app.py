import face_recognition
import numpy as np
import os
import sqlite3
import cv2
import io
import datetime
import json
import gc
import traceback
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_cors import CORS
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.secret_key = "face_track_secret_123"

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# Global variables para sa cache
KNOWN_ENCODINGS = []
KNOWN_NAMES = []
KNOWN_IDS = []

# Enable CORS
CORS(app)

@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# ---------------- CONFIG ----------------
DB_PATH = 'attendance.db'
DATASET_PATH = os.path.join("dataset", "students")
PROFILE_PATH = os.path.join("static", "imageprofile")

# Ensure folders exist
for path in [DATASET_PATH, PROFILE_PATH]:
    os.makedirs(path, exist_ok=True)

# ---------------- DECORATORS ----------------

def role_required(*roles):
    """
    Decorator for API endpoints.
    - No session  → 401 JSON
    - Wrong role  → 403 JSON
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                return jsonify({"success": False, "message": "Authentication required"}), 401
            if roles and session.get('role') not in roles:
                return jsonify({"success": False, "message": "Access denied"}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def login_required(f):
    """
    Decorator for API endpoints that only need any valid session (any role).
    - No session → 401 JSON
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return jsonify({"success": False, "message": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function


def redirect_by_role():
    """Redirect to correct home page based on role after login."""
    role = session.get('role')
    if role in ('student', 'faculty', 'admin'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login_page'))


# ---------------- DATABASE ----------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()

    conn.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            name TEXT NOT NULL,
            student_number TEXT UNIQUE NOT NULL,
            course TEXT,
            year TEXT,
            image_profile TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    try:
        conn.execute('ALTER TABLE attendance ADD COLUMN status TEXT;')
    except sqlite3.OperationalError:
        pass

    conn.execute('''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_number TEXT,
            name TEXT,
            time_in TEXT,
            time_out TEXT,
            date TEXT,
            status TEXT
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            type TEXT,
            location TEXT,
            target_course TEXT,
            target_year TEXT,
            date TEXT,
            recurring_days TEXT,
            start_time TEXT,
            end_time TEXT
        )
    ''')

    # ---------------- SEED DEFAULT ADMIN ----------------
    default_admin_username = 'admin'
    default_admin_password = 'admin123'

    existing_admin = conn.execute(
        "SELECT id FROM users WHERE username = ?", (default_admin_username,)
    ).fetchone()

    if not existing_admin:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (default_admin_username, generate_password_hash(default_admin_password), 'admin')
        )
        print(f"[INIT] Default admin account created → username: '{default_admin_username}' / password: '{default_admin_password}'")
    else:
        print(f"[INIT] Default admin account already exists — skipping seed.")

    conn.commit()
    conn.close()


# ---------------- HELPER: Get active schedule for now ----------------
def get_active_schedule(conn):
    """
    Returns the active schedule dict for the current day and time, or None.
    Checks both one-time (date-based) and recurring (recurring_days) schedules.
    A schedule is considered active if the current time falls within:
        (start_time - 60 minutes)  →  end_time
    """
    now = datetime.datetime.now()
    today_date = now.strftime('%Y-%m-%d')
    current_day = now.strftime('%A')          # e.g. "Monday"
    current_minutes = now.hour * 60 + now.minute

    schedules = conn.execute("SELECT * FROM schedules").fetchall()

    for sched in schedules:
        start_time = sched['start_time']
        end_time   = sched['end_time']

        if not start_time or not end_time:
            continue

        try:
            sh, sm = map(int, start_time.split(':'))
            eh, em = map(int, end_time.split(':'))
        except ValueError:
            continue

        sched_start    = sh * 60 + sm
        sched_end      = eh * 60 + em
        window_start   = sched_start - 60   # allow scanning 60 min before class

        # Is the current time within the scanning window?
        if not (window_start <= current_minutes <= sched_end):
            continue

        # One-time schedule: check exact date
        if sched['date'] and sched['date'] == today_date:
            return dict(sched)

        # Recurring schedule: check day-of-week
        recurring_raw = sched['recurring_days']
        if recurring_raw:
            try:
                recurring_days = json.loads(recurring_raw)
                if current_day in recurring_days:
                    return dict(sched)
            except Exception:
                pass

    return None


# ---------------- AUTH ROUTES ----------------
@app.route("/")
def login_page():
    if 'user' in session:
        return redirect_by_role()
    return render_template("login.html")

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    selected_role = data.get('role')

    if not username or not password or not selected_role:
        return jsonify({"success": False, "message": "Missing credentials or role"}), 400

    conn = get_db_connection()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()

    if user and check_password_hash(user['password_hash'], password) and user['role'] == selected_role:
        session.clear()
        session['user'] = username
        session['user_id'] = user['id']
        session['role'] = selected_role

        redirect_url = url_for('dashboard')
        return jsonify({"success": True, "redirect": redirect_url})
    else:
        return jsonify({"success": False, "message": "Invalid username, password, or role mismatch"})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@app.route("/register")
def register_page():
    return render_template("register.html")

# ---------------- PAGES ----------------

@app.route("/create-account")
def create_account_page():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    if session.get('role') not in ('faculty', 'admin'):
        return redirect(url_for('scanner_page'))
    return render_template("createaccount.html")

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    return render_template('dashboard.html')

@app.route("/scanner")
def scanner_page():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    # FIX: Block students from accessing scanner page server-side.
    # Previously only the frontend JS guard handled this redirect.
    if session.get('role') == 'student':
        return redirect(url_for('dashboard'))
    return render_template("scanner.html")

@app.route("/logs")
def logs_page():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    if session.get('role') not in ('faculty', 'admin'):
        return redirect(url_for('scanner_page'))
    return render_template("attendance_log.html")

@app.route("/students")
def student_records():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    if session.get('role') not in ('faculty', 'admin'):
        return redirect(url_for('scanner_page'))
    return render_template("students.html")

@app.route("/reports")
def reports_page():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    if session.get('role') not in ('faculty', 'admin'):
        return redirect(url_for('scanner_page'))
    return render_template("reports.html")

@app.route("/schedule")
def schedule_page():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    if session.get('role') not in ('faculty', 'admin'):
        return redirect(url_for('scanner_page'))
    return render_template("schedule.html")

@app.route("/student-portal")
def student_portal():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    if session.get('role') != 'student':
        return redirect(url_for('student_records'))
    return render_template("student-portal.html")

@app.route('/settings')
def account_settings():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    return render_template('settings.html')

# ---------------- STATIC PROFILE IMAGE ----------------
@app.route('/static/imageprofile/<filename>')
def serve_profile_image(filename):
    profile_path = os.path.join(PROFILE_PATH, filename)
    if os.path.exists(profile_path):
        return send_file(profile_path, mimetype='image/jpeg')
    else:
        from flask import Response
        svg_placeholder = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
            <rect fill="#e0e0e0" width="100" height="100"/>
            <circle cx="50" cy="35" r="20" fill="#999"/>
            <path d="M 30 60 Q 50 45 70 60 L 70 100 L 30 100 Z" fill="#999"/>
        </svg>'''
        return Response(svg_placeholder, mimetype='image/svg+xml'), 404

# ---------------- LOAD KNOWN FACES ----------------
def load_known_faces():
    known_encodings = []
    known_names = []
    known_ids = []

    conn = get_db_connection()
    students = conn.execute("SELECT name, student_number FROM students").fetchall()
    conn.close()

    for student in students:
        folder_name = secure_filename(student['name'].replace(" ", "_"))
        student_dir = os.path.join(DATASET_PATH, folder_name)

        if os.path.exists(student_dir):
            for filename in os.listdir(student_dir):
                if filename.lower().endswith((".jpg", ".png", ".jpeg")):
                    img_path = os.path.join(student_dir, filename)
                    try:
                        face_img = face_recognition.load_image_file(img_path)
                        encodings = face_recognition.face_encodings(face_img)
                        if encodings:
                            known_encodings.append(encodings[0])
                            known_names.append(student['name'])
                            known_ids.append(student['student_number'])
                    except Exception as e:
                        print(f"Error encoding {img_path}: {e}")

    return known_encodings, known_names, known_ids

# ---------------- SCANNER ----------------
@app.route('/scan', methods=['POST'])
@login_required
def scan_face():
    global KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS

    if not KNOWN_ENCODINGS:
        KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS = load_known_faces()

    if 'image' not in request.files:
        return jsonify({"status": "unknown", "message": "No image uploaded"}), 400

    bgr_frame         = None
    rgb_frame         = None
    small_frame       = None
    face_locations    = None
    face_encodings_list = None

    try:
        file           = request.files['image']
        in_memory_file = io.BytesIO(file.read())
        file_bytes     = np.frombuffer(in_memory_file.getvalue(), dtype=np.uint8)
        bgr_frame      = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)

        if bgr_frame is None:
            return jsonify({"status": "unknown", "message": "Invalid image format"}), 200

        rgb_frame   = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        small_frame = cv2.resize(rgb_frame, (0, 0), fx=0.25, fy=0.25)

        face_locations      = face_recognition.face_locations(small_frame, model="hog")
        face_encodings_list = face_recognition.face_encodings(small_frame, face_locations)

        if not face_encodings_list:
            return jsonify({"status": "unknown", "message": "No face detected"}), 200

        if not KNOWN_ENCODINGS:
            return jsonify({"status": "unknown", "message": "No students registered in database"}), 200

        for face_encoding in face_encodings_list:
            face_distances    = face_recognition.face_distance(KNOWN_ENCODINGS, face_encoding)
            best_match_index  = np.argmin(face_distances)
            best_distance     = face_distances[best_match_index]

            if best_distance < 0.45:
                name           = KNOWN_NAMES[best_match_index]
                student_number = KNOWN_IDS[best_match_index]

                now           = datetime.datetime.now()
                date          = now.strftime('%Y-%m-%d')
                time_str      = now.strftime('%H:%M')
                full_time_str = now.strftime('%H:%M:%S')

                conn = get_db_connection()
                try:
                    student = conn.execute(
                        "SELECT * FROM students WHERE student_number=?", (student_number,)
                    ).fetchone()

                    if student is None:
                        return jsonify({
                            "status": "unknown",
                            "message": "Student not found in database. Restart server to reload face cache."
                        })

                    course_str = student['course'] if student and student['course'] else "N/A"
                    year_str   = student['year']   if student and student['year']   else "N/A"
                    image_url  = student['image_profile'] if student else None

                    # ── SCHEDULE CHECK ───────────────────────────────────────────
                    active_schedule = get_active_schedule(conn)

                    if not active_schedule:
                        return jsonify({
                            "status": "no_schedule",
                            "name": name,
                            "student_number": student_number,
                            "course": course_str,
                            "year":   year_str,
                            "image":  image_url,
                            "message": "No active class schedule at this time."
                        })

                    # ── DETERMINE STATUS: Present or Late ────────────────────────
                    sh, sm = map(int, active_schedule['start_time'].split(':'))
                    sched_start_minutes = sh * 60 + sm
                    current_minutes     = now.hour * 60 + now.minute
                    late_threshold      = sched_start_minutes + 10   # 10-min grace period

                    status = "Late" if current_minutes > late_threshold else "Present"

                    # ── RECORD ATTENDANCE ────────────────────────────────────────
                    existing = conn.execute(
                        "SELECT * FROM attendance WHERE student_number=? AND date=?",
                        (student_number, date)
                    ).fetchone()

                    if not existing:
                        # First scan of the day → Time In
                        conn.execute(
                            "INSERT INTO attendance (student_number, name, time_in, date, status) VALUES (?, ?, ?, ?, ?)",
                            (student_number, name, full_time_str, date, status)
                        )
                        conn.commit()

                        # FIX: Include course and year in socket emit so the
                        # dashboard live table can populate those columns correctly.
                        socketio.emit('attendance_update', {
                            "name":           name,
                            "student_number": student_number,
                            "time_in":        time_str,
                            "time_out":       None,
                            "status":         status,
                            "date":           date,
                            "course":         course_str,
                            "year":           year_str,
                            "message":        "Time In recorded"
                        }, to='/')

                        result_message = "Time In recorded"
                        scan_type      = "time_in"

                    else:
                        # Subsequent scan → Time Out (with 30-second cooldown)
                        time_in_str = existing['time_in']
                        try:
                            time_in_obj = datetime.datetime.strptime(time_in_str, '%H:%M:%S')
                        except ValueError:
                            time_in_obj = datetime.datetime.strptime(time_in_str, '%H:%M')

                        time_in_full   = datetime.datetime.combine(now.date(), time_in_obj.time())
                        time_diff_secs = (now - time_in_full).total_seconds()
                        time_diff_mins = time_diff_secs / 60

                        print(f"DEBUG: time_in={time_in_str}, diff_secs={time_diff_secs}, diff_mins={time_diff_mins}")

                        if time_diff_mins >= 0.5:
                            conn.execute(
                                "UPDATE attendance SET time_out=? WHERE student_number=? AND date=?",
                                (full_time_str, student_number, date)
                            )
                            conn.commit()

                            # FIX: Include course and year in time-out emit as well.
                            socketio.emit('attendance_update', {
                                "name":           name,
                                "student_number": student_number,
                                "time_in":        existing['time_in'],
                                "time_out":       time_str,
                                "status":         status,
                                "date":           date,
                                "course":         course_str,
                                "year":           year_str,
                                "message":        "Time Out recorded"
                            }, to='/')

                            result_message = "Time Out recorded"
                            scan_type      = "time_out"
                        else:
                            secs_remaining = max(0, int((0.5 - time_diff_mins) * 60))
                            result_message = f"Time Out available in {secs_remaining} seconds"
                            scan_type      = "time_in"

                finally:
                    conn.close()

                return jsonify({
                    "status":            "recognized",
                    "name":              name,
                    "student_number":    student_number,
                    "course":            course_str,
                    "year":              year_str,
                    "image":             image_url,
                    "attendance_status": status,
                    "scan_type":         scan_type,
                    "message":           result_message
                })

        return jsonify({"status": "unknown", "message": "Face not recognized or not in database"}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 200

    finally:
        if bgr_frame         is not None: del bgr_frame
        if rgb_frame         is not None: del rgb_frame
        if small_frame       is not None: del small_frame
        if face_locations    is not None: del face_locations
        if face_encodings_list is not None: del face_encodings_list
        gc.collect()

# ---------------- REGISTER STUDENT ----------------
@app.route('/api/register_student', methods=['POST'])
@login_required
def register_student():
    global KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS

    try:
        full_name = request.form.get('full_name')
        course    = request.form.get('course')
        year      = request.form.get('year')

        if session.get('role') == 'student':
            student_number = session['user']
            user_id        = session.get('user_id')
        else:
            return jsonify({"message": "Only students can register their own profile"}), 403

        profile_image   = request.files.get('profile_image')
        training_images = request.files.getlist('training_images')

        if not full_name:
            return jsonify({"message": "Missing full name"}), 400

        if len(training_images) == 0:
            return jsonify({"message": "Please upload at least one training image"}), 400

        folder_name = secure_filename(full_name.replace(" ", "_"))

        os.makedirs(PROFILE_PATH, exist_ok=True)
        image_profile_path = None
        if profile_image:
            profile_image.save(os.path.join(PROFILE_PATH, f"{folder_name}.jpg"))
            image_profile_path = f"/static/imageprofile/{folder_name}.jpg"

        dataset_folder = os.path.join(DATASET_PATH, folder_name)
        os.makedirs(dataset_folder, exist_ok=True)

        valid_images = 0
        for i, img in enumerate(training_images):
            path  = os.path.join(dataset_folder, f"{i+1}.jpg")
            img.save(path)
            image = face_recognition.load_image_file(path)
            encodings = face_recognition.face_encodings(image)
            if encodings:
                valid_images += 1
            else:
                os.remove(path)

        if valid_images == 0:
            return jsonify({"message": "No valid face images in training set"}), 400

        conn = get_db_connection()
        try:
            existing = conn.execute(
                "SELECT id FROM students WHERE student_number = ?", (student_number,)
            ).fetchone()

            print(f"[DEBUG] register_student: student_number={student_number}, user_id={user_id}, existing={existing}")
            print(f"[DEBUG] register_student: full_name={full_name}, course={course}, year={year}")
            print(f"[DEBUG] register_student: image_profile_path={image_profile_path}")

            if existing:
                print(f"[DEBUG] register_student: Updating existing record id={existing['id']}")
                if image_profile_path:
                    cursor = conn.execute("""
                        UPDATE students SET name=?, course=?, year=?, image_profile=?
                        WHERE student_number=?
                    """, (full_name, course, year, image_profile_path, student_number))
                else:
                    cursor = conn.execute("""
                        UPDATE students SET name=?, course=?, year=?
                        WHERE student_number=?
                    """, (full_name, course, year, student_number))
                print(f"[DEBUG] register_student: UPDATE rowcount={cursor.rowcount}")
            else:
                print(f"[WARN] register_student: No student record found for {student_number}, inserting. user_id={user_id}")
                cursor = conn.execute("""
                    INSERT INTO students (user_id, name, student_number, course, year, image_profile)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, full_name, student_number, course, year, image_profile_path))
                print(f"[DEBUG] register_student: INSERT lastrowid={cursor.lastrowid}")

            conn.commit()
            print(f"[DEBUG] register_student: commit successful")

            verify = conn.execute(
                "SELECT * FROM students WHERE student_number = ?", (student_number,)
            ).fetchone()
            print(f"[DEBUG] register_student: verify after commit = {dict(verify) if verify else None}")

        except Exception as db_err:
            print(f"[ERROR] register_student DB error: {db_err}")
            traceback.print_exc()
            raise
        finally:
            conn.close()

        KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS = load_known_faces()

        return jsonify({"message": "Student profile updated successfully"})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"message": str(e)}), 500

# ---------------- UPDATE STUDENT ----------------
@app.route('/api/update_student', methods=['POST'])
@login_required
def update_student():
    global KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS

    try:
        full_name      = request.form.get('full_name')
        student_number = request.form.get('student_number')
        course         = request.form.get('course')
        year           = request.form.get('year')

        # Students can only update their own record
        if session.get('role') == 'student':
            student_number = session['user']

        # FIX: Faculty should not directly update student records via this endpoint.
        # Faculty manage students through the student records page which uses
        # dedicated admin-level routes. This prevents privilege escalation.
        if session.get('role') == 'faculty':
            return jsonify({"message": "Faculty cannot update student records directly via this endpoint"}), 403

        profile_image   = request.files.get('profile_image')
        training_images = request.files.getlist('training_images')

        if not full_name or not student_number:
            return jsonify({"message": "Missing data"}), 400

        conn = get_db_connection()
        try:
            conn.execute("""
                UPDATE students SET name=?, course=?, year=? WHERE student_number=?
            """, (full_name, course, year, student_number))

            if profile_image:
                folder_name = secure_filename(full_name.replace(" ", "_"))
                os.makedirs(PROFILE_PATH, exist_ok=True)
                profile_image.save(os.path.join(PROFILE_PATH, f"{folder_name}.jpg"))
                conn.execute(
                    "UPDATE students SET image_profile=? WHERE student_number=?",
                    (f"/static/imageprofile/{folder_name}.jpg", student_number)
                )

            if training_images and len(training_images) > 0:
                folder_name    = secure_filename(full_name.replace(" ", "_"))
                dataset_folder = os.path.join(DATASET_PATH, folder_name)
                os.makedirs(dataset_folder, exist_ok=True)

                for file in os.listdir(dataset_folder):
                    os.remove(os.path.join(dataset_folder, file))

                valid_images = 0
                for i, img in enumerate(training_images):
                    path  = os.path.join(dataset_folder, f"{i+1}.jpg")
                    img.save(path)
                    image = face_recognition.load_image_file(path)
                    encodings = face_recognition.face_encodings(image)
                    if encodings:
                        valid_images += 1
                    else:
                        os.remove(path)

                if valid_images == 0:
                    conn.close()
                    return jsonify({"message": "No valid face images in training set"}), 400

            conn.commit()
        finally:
            conn.close()

        KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS = load_known_faces()
        return jsonify({"message": "Student updated successfully"})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"message": str(e)}), 500

# ---------------- DELETE STUDENT — Admin only ----------------
@app.route('/api/delete_student/<student_id>', methods=['DELETE'])
@role_required('admin')
def delete_student(student_id):
    global KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS

    try:
        conn = get_db_connection()
        student = conn.execute(
            "SELECT name FROM students WHERE student_number = ?", (student_id,)
        ).fetchone()

        if not student:
            conn.close()
            return jsonify({"message": "Student not found"}), 404

        student_name = student['name']
        folder_name  = secure_filename(student_name.replace(" ", "_"))

        conn.execute("DELETE FROM students WHERE student_number = ?", (student_id,))
        conn.commit()
        conn.close()

        profile_path = os.path.join(PROFILE_PATH, f"{folder_name}.jpg")
        if os.path.exists(profile_path):
            try:
                os.remove(profile_path)
            except Exception as e:
                print(f"Warning: could not delete profile image: {e}")

        dataset_folder = os.path.join(DATASET_PATH, folder_name)
        if os.path.exists(dataset_folder):
            try:
                for file in os.listdir(dataset_folder):
                    os.remove(os.path.join(dataset_folder, file))
                os.rmdir(dataset_folder)
            except Exception as e:
                print(f"Warning: could not delete dataset folder: {e}")

        KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS = load_known_faces()

        socketio.emit('student_deleted', {
            "student_number": student_id,
            "name":           student_name
        }, to='/')

        return jsonify({"message": "Student deleted successfully"}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"message": str(e)}), 500

# ---------------- DELETE LOG — Admin only ----------------
@app.route('/delete_log/<int:id>', methods=['DELETE'])
@role_required('admin')
def delete_log(id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM attendance WHERE id = ?", (id,))
        conn.commit()
        return jsonify({"success": True, "message": "Record deleted successfully"}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()

# ---------------- DELETE SCHEDULE — Admin only ----------------
@app.route('/delete_schedule/<int:id>', methods=['POST', 'DELETE'])
@role_required('admin')
def delete_schedule(id):
    conn = get_db_connection()
    conn.execute("DELETE FROM schedules WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ---------------- SCHEDULES ----------------
@app.route('/api/schedules', methods=['GET', 'POST'])
@login_required
def handle_schedules():
    conn    = get_db_connection()
    cursor  = conn.cursor()

    if request.method == 'POST':
        if session.get('role') not in ('faculty', 'admin'):
            conn.close()
            return jsonify({"success": False, "message": "Access denied"}), 403

        data          = request.get_json() or {}
        name          = data.get('name', 'N/A')
        stype         = data.get('type', 'Class')
        location      = data.get('location', 'N/A')
        target_course = data.get('target_course', 'BSCpE')
        target_year   = data.get('target_year', '1')
        date          = data.get('date', 'N/A')
        start_time    = data.get('start_time', '08:00')
        end_time      = data.get('end_time', '10:00')
        recurring_days = json.dumps(data.get('recurring_days', []))

        try:
            cursor.execute('''
                INSERT INTO schedules (name, type, location, target_course, target_year, date, start_time, end_time, recurring_days)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (name, stype, location, target_course, target_year, date, start_time, end_time, recurring_days))
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': 'Schedule saved successfully.'})
        except Exception as e:
            conn.close()
            traceback.print_exc()
            return jsonify({'success': False, 'message': str(e)}), 500

    elif request.method == 'GET':
        cursor.execute('SELECT * FROM schedules ORDER BY id DESC')
        rows      = cursor.fetchall()
        schedules = []
        for row in rows:
            schedules.append({
                'id':            row['id'],
                'name':          row['name'],
                'type':          row['type'],
                'location':      row['location'],
                'target_course': row['target_course'],
                'target_year':   row['target_year'],
                'date':          row['date'],
                'start_time':    row['start_time'],
                'end_time':      row['end_time'],
                'recurring_days': json.loads(row['recurring_days']) if row['recurring_days'] else []
            })
        conn.close()
        return jsonify(schedules)

# ---------------- DASHBOARD STATS — Faculty and Admin only ----------------
@app.route('/api/dashboard_stats')
@role_required('faculty', 'admin')
def dashboard_stats():
    conn   = get_db_connection()
    cursor = conn.cursor()

    total_enrolled  = cursor.execute('SELECT COUNT(*) FROM students').fetchone()[0]
    present_today   = cursor.execute("SELECT COUNT(DISTINCT student_number) FROM attendance WHERE date = date('now')").fetchone()[0]
    total_records   = cursor.execute("SELECT COUNT(*) FROM attendance WHERE date = date('now')").fetchone()[0]

    recent_logs_raw = cursor.execute("""
        SELECT a.student_number, a.name, a.time_in, a.time_out, a.status, s.course, s.year
        FROM attendance a
        LEFT JOIN students s ON a.student_number = s.student_number
        ORDER BY a.id DESC LIMIT 10
    """).fetchall()

    rate = round((present_today / total_enrolled * 100), 1) if total_enrolled > 0 else 0

    logs_data = []
    for row in recent_logs_raw:
        row_dict = dict(row)
        if not row_dict.get('time_in'):
            row_dict['time_in'] = '--:--'
        logs_data.append(row_dict)

    conn.close()

    return jsonify({
        "total_enrolled":       total_enrolled,
        "present_today":        present_today,
        "total_records_today":  total_records,
        "attendance_rate":      rate,
        "recent_logs":          logs_data
    })

# ---------------- GET STUDENTS ----------------
@app.route('/api/get_students')
@login_required
def get_students():
    if session.get('role') == 'student':
        try:
            conn     = get_db_connection()
            username = session['user']
            print(f"[DEBUG] get_students: Looking for student_number={username}")

            student = conn.execute(
                "SELECT * FROM students WHERE student_number=?", (username,)
            ).fetchone()
            conn.close()

            if student:
                name = student['name'] or ''
                print(f"[DEBUG] get_students: Found record name={name!r}, course={student['course']}, year={student['year']}")

                not_yet_registered = (
                    name == username and
                    student['course'] is None and
                    student['year']   is None
                )

                if not_yet_registered:
                    print(f"[DEBUG] get_students: Account exists but profile not yet completed")
                    return jsonify([])

                return jsonify([{
                    "name":          name if name else username,
                    "student_number": student["student_number"],
                    "course":        student["course"] or "N/A",
                    "year":          student["year"]   or "N/A",
                    "display_photo": student["image_profile"]
                }])
            else:
                print(f"[DEBUG] get_students: No student record found for student_number={username}")
                return jsonify([])
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # Faculty and Admin see all
    try:
        conn     = get_db_connection()
        students = conn.execute("SELECT * FROM students").fetchall()
        conn.close()
        return jsonify([
            {
                "name":           s["name"],
                "student_number": s["student_number"],
                "course":         s["course"] or "N/A",
                "year":           s["year"]   or "N/A",
                "display_photo":  s["image_profile"]
            }
            for s in students
        ])
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ---------------- ATTENDANCE LOGS ----------------
@app.route('/api/attendance_logs', methods=['GET'])
@login_required
def get_attendance_logs():
    conn = get_db_connection()

    if session.get('role') == 'student':
        logs = conn.execute(
            '''SELECT a.id, a.student_number, a.name, a.time_in, a.time_out,
                      a.date, a.status,
                      s.course AS course, s.year AS year
               FROM attendance a
               LEFT JOIN students s ON a.student_number = s.student_number
               WHERE a.student_number=?
               ORDER BY a.date DESC, a.time_in DESC''',
            (session['user'],)
        ).fetchall()
    else:
        logs = conn.execute(
            '''SELECT a.id, a.student_number, a.name, a.time_in, a.time_out,
                      a.date, a.status,
                      s.course AS course, s.year AS year
               FROM attendance a
               LEFT JOIN students s ON a.student_number = s.student_number
               ORDER BY a.date DESC, a.time_in DESC'''
        ).fetchall()

    conn.close()
    return jsonify([dict(log) for log in logs])

# ---------------- RECENT LOGS ----------------
@app.route('/api/get_recent_logs', methods=['GET'])
@login_required
def get_recent_logs():
    conn = None
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, student_number, name, time_in, time_out, date, status
            FROM attendance
            ORDER BY date DESC, time_in DESC
            LIMIT 10
        """)

        rows = cursor.fetchall()
        logs = []
        for row in rows:
            time_in_str    = row["time_in"]
            formatted_time = "--:--"

            if time_in_str and time_in_str != "--:--":
                try:
                    if len(time_in_str.split(':')) == 3:
                        t_obj = datetime.datetime.strptime(time_in_str, '%H:%M:%S')
                    else:
                        t_obj = datetime.datetime.strptime(time_in_str, '%H:%M')
                    formatted_time = t_obj.strftime('%I:%M %p')
                except ValueError:
                    formatted_time = time_in_str

            logs.append({
                "id":             row["id"],
                "student_number": row["student_number"],
                "name":           row["name"],
                "time_in":        formatted_time,
                "time_out":       row["time_out"],
                "date":           row["date"],
                "status":         row["status"]
            })

        return jsonify({"status": "success", "logs": logs}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Internal Server Error while retrieving logs."}), 500

    finally:
        if conn:
            conn.close()

# ---------------- MARK ATTENDANCE ----------------
@app.route('/api/mark-attendance', methods=['POST'])
@login_required
def mark_attendance():
    data              = request.get_json()
    student_number    = data.get('student_number')
    name              = data.get('name')
    attendance_status = data.get('attendance_status', 'ON-TIME')

    now            = datetime.datetime.now()
    date           = now.strftime('%Y-%m-%d')
    time_str       = now.strftime('%H:%M:%S')
    formatted_time = now.strftime('%I:%M %p')

    conn     = get_db_connection()
    existing = conn.execute(
        "SELECT * FROM attendance WHERE student_number=? AND date=?",
        (student_number, date)
    ).fetchone()

    if existing:
        conn.close()
        return jsonify({"success": False, "message": "Already marked today"})

    conn.execute(
        "INSERT INTO attendance (student_number, name, time_in, date, status) VALUES (?, ?, ?, ?, ?)",
        (student_number, name, time_str, date, attendance_status)
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True, "time_in": formatted_time})

# ---------------- MARK ABSENTS — Faculty/Admin only ----------------
@app.route('/api/mark_absents', methods=['POST'])
@role_required('faculty', 'admin')
def mark_absents():
    """
    For a given schedule_id, insert an Absent record for every student
    in the schedule's target_course + target_year who has no attendance
    entry for today.  Call this after the schedule's end_time has passed.
    """
    data        = request.get_json()
    schedule_id = data.get('schedule_id')

    if not schedule_id:
        return jsonify({"success": False, "message": "schedule_id is required"}), 400

    conn = get_db_connection()
    try:
        sched = conn.execute(
            "SELECT * FROM schedules WHERE id=?", (schedule_id,)
        ).fetchone()

        if not sched:
            return jsonify({"success": False, "message": "Schedule not found"}), 404

        today    = datetime.datetime.now().strftime('%Y-%m-%d')
        students = conn.execute(
            "SELECT student_number, name FROM students WHERE course=? AND year=?",
            (sched['target_course'], sched['target_year'])
        ).fetchall()

        marked = 0
        for student in students:
            existing = conn.execute(
                "SELECT id FROM attendance WHERE student_number=? AND date=?",
                (student['student_number'], today)
            ).fetchone()

            if not existing:
                conn.execute(
                    "INSERT INTO attendance (student_number, name, time_in, date, status) VALUES (?, ?, ?, ?, ?)",
                    (student['student_number'], student['name'], None, today, 'Absent')
                )
                marked += 1

        conn.commit()
        return jsonify({"success": True, "marked_absent": marked})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()

# ---------------- REPORT DATA — Faculty and Admin only ----------------
@app.route("/api/report-data")
@role_required('faculty', 'admin')
def report_data():
    conn   = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT a.name, a.student_number, s.course, s.year, a.status, a.date, a.time_in
        FROM attendance a
        LEFT JOIN students s ON a.student_number = s.student_number
        ORDER BY a.date DESC
    """)

    rows         = [dict(r) for r in cursor.fetchall()]
    all_students = cursor.execute("SELECT DISTINCT student_number, name FROM students").fetchall()
    all_dates    = [row['date'] for row in cursor.execute(
        "SELECT DISTINCT date FROM attendance ORDER BY date DESC"
    ).fetchall()]

    attendance_dict = {}
    for record in rows:
        key = (record['student_number'], record['date'])
        attendance_dict[key] = record

    for student in all_students:
        for date in all_dates:
            key = (student['student_number'], date)
            if key not in attendance_dict:
                student_data = cursor.execute(
                    "SELECT course, year FROM students WHERE student_number = ?",
                    (student['student_number'],)
                ).fetchone()
                rows.append({
                    'name':           student['name'],
                    'student_number': student['student_number'],
                    'course':         student_data['course'] if student_data else 'N/A',
                    'year':           student_data['year']   if student_data else 'N/A',
                    'status':         'Absent',
                    'date':           date,
                    'time_in':        None
                })

    conn.close()
    return jsonify(rows)

@app.route("/api/available-courses")
@role_required('faculty', 'admin')
def available_courses():
    """Get list of all available courses."""
    conn   = get_db_connection()
    cursor = conn.cursor()
    
    courses = cursor.execute(
        "SELECT DISTINCT course FROM students WHERE course IS NOT NULL AND course != 'N/A' ORDER BY course"
    ).fetchall()
    
    conn.close()
    return jsonify([c['course'] for c in courses])

@app.route("/api/available-years")
@role_required('faculty', 'admin')
def available_years():
    """Get list of all available years."""
    conn   = get_db_connection()
    cursor = conn.cursor()
    
    years = cursor.execute(
        "SELECT DISTINCT year FROM students WHERE year IS NOT NULL AND year != 'N/A' ORDER BY year"
    ).fetchall()
    
    conn.close()
    return jsonify([y['year'] for y in years])

# -----------------------------------------------------------------------
# AUTOMATIC ABSENCE MARKING: Check for expired schedules
# This function marks students as absent if:
# 1. A schedule has ended (current time > schedule end_time)
# 2. No attendance record exists for that student today
# -----------------------------------------------------------------------
def check_and_mark_expired_schedules():
    """
    Check all active schedules and mark students as absent if:
    - The schedule has ended (current time >= end_time)
    - The student has no attendance entry for today
    - The schedule applies to that student's course/year
    
    Returns: dict with counts of marked absents per schedule
    """
    now = datetime.datetime.now()
    today_date = now.strftime('%Y-%m-%d')
    current_day = now.strftime('%A')
    current_minutes = now.hour * 60 + now.minute
    
    conn = get_db_connection()
    try:
        schedules = conn.execute("SELECT * FROM schedules").fetchall()
        marked_summary = {}
        
        for sched in schedules:
            start_time = sched['start_time']
            end_time = sched['end_time']
            
            if not start_time or not end_time:
                continue
            
            try:
                eh, em = map(int, end_time.split(':'))
            except ValueError:
                continue
            
            sched_end = eh * 60 + em
            
            # Check if schedule has ended
            if current_minutes < sched_end:
                continue  # Schedule still active, skip
            
            # Determine if this schedule applies today
            schedule_applies_today = False
            
            # One-time schedule: check exact date
            if sched['date'] and sched['date'] == today_date:
                schedule_applies_today = True
            # Recurring schedule: check day-of-week
            elif sched['recurring_days']:
                try:
                    recurring_days = json.loads(sched['recurring_days'])
                    if current_day in recurring_days:
                        schedule_applies_today = True
                except Exception:
                    pass
            
            if not schedule_applies_today:
                continue
            
            # Get all students in this class
            students = conn.execute(
                "SELECT student_number, name FROM students WHERE course=? AND year=?",
                (sched['target_course'], sched['target_year'])
            ).fetchall()
            
            marked_count = 0
            for student in students:
                # Check if student has any attendance record for today
                existing = conn.execute(
                    "SELECT id FROM attendance WHERE student_number=? AND date=?",
                    (student['student_number'], today_date)
                ).fetchone()
                
                if not existing:
                    # Mark as absent
                    conn.execute(
                        "INSERT INTO attendance (student_number, name, time_in, date, status) VALUES (?, ?, ?, ?, ?)",
                        (student['student_number'], student['name'], None, today_date, 'Absent')
                    )
                    marked_count += 1
            
            if marked_count > 0:
                marked_summary[sched['id']] = {
                    'schedule_name': sched['name'],
                    'marked_count': marked_count
                }
        
        conn.commit()
        return marked_summary
        
    except Exception as e:
        print(f"[ERROR] check_and_mark_expired_schedules: {e}")
        traceback.print_exc()
        return {}
    finally:
        conn.close()


# -----------------------------------------------------------------------
# AUTOMATIC ABSENCE MARKING: API Endpoint
# Can be called by frontend (e.g., scanner page, dashboard) to check 
# for expired schedules and automatically mark absents
# -----------------------------------------------------------------------
@app.route('/api/check-expired-schedules', methods=['POST'])
@login_required
def check_expired_schedules_endpoint():
    """
    Endpoint to check for expired schedules and mark students as absent.
    Can be called by frontend periodically or when needed.
    """
    try:
        marked = check_and_mark_expired_schedules()
        
        if marked:
            return jsonify({
                "success": True,
                "message": f"Marked absents for {len(marked)} schedule(s)",
                "details": marked
            })
        else:
            return jsonify({
                "success": True,
                "message": "No expired schedules found or all students already marked",
                "details": {}
            })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


# -----------------------------------------------------------------------
# FIX: Public student self-registration endpoint.
#
# The login page (login.html) has a "New student? Create an account" modal
# that calls POST /create-account WITHOUT a session. The original
# /create-account route is decorated with @role_required('faculty','admin'),
# which always returns 401 for unauthenticated requests — silently breaking
# the self-registration feature for students.
#
# This new public endpoint handles that case.  It only ever creates student
# accounts, so there is no privilege-escalation risk.
# -----------------------------------------------------------------------
@app.route('/api/register-student-public', methods=['POST'])
def register_student_public():
    data     = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    course   = data.get('course', 'BSCpE').strip()  # Default to BSCpE if not provided
    year     = data.get('year', '1').strip()        # Default to 1st year if not provided

    if not username or not password:
        return jsonify({"success": False, "message": "Username and password are required"}), 400

    if len(password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters"}), 400

    hashed_pw = generate_password_hash(password)

    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, hashed_pw, 'student')
        )
        user_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()['id']
        conn.execute(
            "INSERT INTO students (user_id, name, student_number, course, year) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, username, course, year)
        )
        conn.commit()
        print(f"[PUBLIC REG] Student self-registered: username={username}")
        return jsonify({"success": True})

    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({"success": False, "message": "Student number already registered"}), 409
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()

# ---------------- ACCOUNT CREATION — Admin (any role) / Faculty (student only) ----------------
@app.route('/create-account', methods=['POST'])
@role_required('faculty', 'admin')
def create_account():
    data     = request.get_json()
    username = data.get('username')
    password = data.get('password')
    role     = data.get('role', 'student')

    if not username or not password:
        return jsonify({"success": False, "message": "Missing credentials"}), 400

    # Faculty can only create student accounts
    if session.get('role') == 'faculty' and role != 'student':
        return jsonify({"success": False, "message": "Faculty can only create student accounts"}), 403

    if role not in ('student', 'faculty', 'admin'):
        return jsonify({"success": False, "message": "Invalid role"}), 400

    hashed_pw = generate_password_hash(password)

    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, hashed_pw, role)
        )

        if role == 'student':
            user_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()['id']
            course  = data.get('course', 'BSCpE')  # Default to BSCpE if not provided
            year    = data.get('year', '1')        # Default to 1st year if not provided
            conn.execute(
                "INSERT INTO students (user_id, name, student_number, course, year) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, username, course, year)
            )

        conn.commit()
        print(f"[ACCOUNT] Created account: username={username}, role={role}")
        if role == 'student':
            print(f"[ACCOUNT] Student record created: student_number={username}")

        conn.close()
        return jsonify({"success": True})

    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": "Username/Student number already exists"}), 409
    except Exception as e:
        conn.rollback()
        conn.close()
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

# ---------------- SESSION INFO ----------------
@app.route('/api/session-info')
def session_info():
    if 'user' not in session:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "username":  session['user'],
        "role":      session.get('role')
    })

# ---------------- RUN SERVER ----------------
if __name__ == "__main__":
    init_db()
    print("Pre-loading face encodings...")
    KNOWN_ENCODINGS, KNOWN_NAMES, KNOWN_IDS = load_known_faces()
    print("Done. Starting server...")
    socketio.run(app, host="127.0.0.1", port=5000, debug=False, use_reloader=False)