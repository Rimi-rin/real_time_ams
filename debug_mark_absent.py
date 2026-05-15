#!/usr/bin/env python3
"""Debug script to diagnose mark_absent issue"""
import sqlite3
import json
import datetime

conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row

print("=" * 70)
print("DEBUG: MARK ABSENT ISSUE")
print("=" * 70)

print("\n--- ALL SCHEDULES ---")
schedules = conn.execute("SELECT * FROM schedules").fetchall()
if schedules:
    for sched in schedules:
        print(f"\nSchedule ID {sched['id']}: {sched['name']}")
        print(f"  target_course: '{sched['target_course']}'")
        print(f"  target_year: '{sched['target_year']}'")
        print(f"  start_time: {sched['start_time']}")
        print(f"  end_time: {sched['end_time']}")
        print(f"  date: {sched['date']}")
        print(f"  recurring_days: {sched['recurring_days']}")
else:
    print("  (no schedules found)")

print("\n--- ALL STUDENTS ---")
students = conn.execute("SELECT * FROM students").fetchall()
if students:
    print(f"Total students: {len(students)}")
    # Group by course/year
    by_course_year = {}
    for student in students:
        key = (student['course'], student['year'])
        if key not in by_course_year:
            by_course_year[key] = []
        by_course_year[key].append(student['student_number'])
    
    for (course, year), student_nums in sorted(by_course_year.items()):
        print(f"\n  Course '{course}', Year '{year}': {len(student_nums)} students")
        for num in student_nums[:3]:  # Show first 3
            print(f"    - {num}")
        if len(student_nums) > 3:
            print(f"    ... and {len(student_nums) - 3} more")
else:
    print("  (no students found)")

print("\n--- TODAY'S ATTENDANCE RECORDS ---")
today = datetime.datetime.now().strftime('%Y-%m-%d')
attendance = conn.execute("SELECT * FROM attendance WHERE date=?", (today,)).fetchall()
print(f"Date: {today}")
print(f"Total records: {len(attendance)}")
if attendance:
    statuses = {}
    for record in attendance:
        status = record['status'] or 'None'
        statuses[status] = statuses.get(status, 0) + 1
    for status, count in sorted(statuses.items()):
        print(f"  {status}: {count}")

print("\n--- SIMULATION: If we call mark_absents for each schedule ---")
for sched in schedules:
    print(f"\n[Schedule {sched['id']}: {sched['name']}]")
    print(f"  Looking for students with course='{sched['target_course']}' AND year='{sched['target_year']}'")
    
    students_in_sched = conn.execute(
        "SELECT student_number, name FROM students WHERE course=? AND year=?",
        (sched['target_course'], sched['target_year'])
    ).fetchall()
    
    print(f"  Found {len(students_in_sched)} students")
    
    if students_in_sched:
        would_mark = 0
        for student in students_in_sched:
            existing = conn.execute(
                "SELECT id FROM attendance WHERE student_number=? AND date=?",
                (student['student_number'], today)
            ).fetchone()
            if not existing:
                would_mark += 1
        print(f"  Would mark as absent: {would_mark} students")
    else:
        print(f"  WARNING: No students found matching this schedule!")

conn.close()
