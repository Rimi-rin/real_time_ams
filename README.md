# Face Recognition Attendance System

An automated attendance tracking system using face recognition, built with Flask and OpenCV.

## Features

- **Face Recognition** — Automatic attendance marking via facial recognition
- **Schedule Management** — Create and manage class schedules
- **Attendance Logging** — Track student attendance with timestamps
- **Multi-User Roles** — Admin, Faculty, and Student accounts
- **Real-time Scanning** — Live camera feed for attendance marking
- **Attendance Reports** — Generate and export attendance records

## Requirements

- Python 3.8+
- SQLite3
- Webcam for face scanning

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/attendance-system.git
cd attendance-system
```

### 2. Create Virtual Environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Initialize Database

```bash
python app.py
```

Then visit `http://127.0.0.1:5000` in your browser and the database will be initialized automatically.

## Running the Application

```bash
python app.py
```

The application will be available at `http://127.0.0.1:5000`

### Default Admin Login
- **Username:** admin
- **Password:** password123

## Project Structure

```
attendance-system/
├── app.py                          # Main Flask application
├── requirements.txt                # Python dependencies
├── attendance.db                   # SQLite database (auto-created)
├── templates/                      # HTML templates
│   ├── scanner.html               # Face scanner interface
│   ├── attendance_log.html        # Attendance records
│   ├── dashboard.html             # User dashboard
│   └── ...
├── static/                         # Static files (CSS, JS)
├── dataset/                        # Student face datasets
└── scripts/                        # Utility scripts
    ├── fix_student_course_year.py # Fix student course/year format
    └── check_course_formats.py    # Verify course format matching
```

## Key Fixes

### Mark Absent Bug Fix
If "Mark Absent" function wasn't working, run this script to fix existing student records:

```bash
python fix_student_course_year.py
```

This ensures student course/year formats match schedule expectations.

## Deployment Options

### Option 1: Heroku (Free tier available)
1. Create Heroku account
2. Install Heroku CLI
3. Run: `heroku create`
4. Push code: `git push heroku main`

### Option 2: PythonAnywhere
1. Sign up at pythonanywhere.com
2. Upload files via web interface
3. Configure web app settings

### Option 3: DigitalOcean / AWS / Google Cloud
Deploy as a traditional web app with a production WSGI server.

## Database Notes

- The app uses SQLite for development
- For production, consider PostgreSQL or MySQL
- Student records must have properly formatted course/year fields

## Troubleshooting

**Mark Absent not working?**
- Run: `python check_course_formats.py` to verify course format matching
- Run: `python fix_student_course_year.py` to fix existing records

**Face recognition not detecting faces?**
- Ensure webcam is working
- Check lighting conditions
- Make sure student face is in dataset folder

## License

MIT License
