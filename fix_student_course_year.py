#!/usr/bin/env python3
"""Fix student course/year values to match schedule expectations"""
import sqlite3

conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row

print("=== FIXING STUDENT COURSE/YEAR VALUES ===\n")

# Mapping of full names to abbreviations
course_mapping = {
    'BS Computer Engineering': 'BSCpE',
    'BS Mechanical Engineering': 'BSMechE',
    '3rd Year': '3',
    '1st Year': '1',
    '2nd Year': '2',
    '4th Year': '4',
}

print("Current students before fix:")
students = conn.execute("SELECT student_number, course, year FROM students").fetchall()
for s in students:
    print(f"  {s['student_number']}: course='{s['course']}', year='{s['year']}'")

print("\nUpdating students...")
for student in students:
    old_course = student['course']
    old_year = student['year']
    
    # Map to abbreviated values
    new_course = course_mapping.get(old_course, old_course)
    new_year = course_mapping.get(old_year, old_year)
    
    conn.execute(
        "UPDATE students SET course=?, year=? WHERE student_number=?",
        (new_course, new_year, student['student_number'])
    )
    print(f"  {student['student_number']}: '{old_course}' → '{new_course}', '{old_year}' → '{new_year}'")

conn.commit()

print("\nStudents after fix:")
students = conn.execute("SELECT student_number, course, year FROM students").fetchall()
for s in students:
    print(f"  {s['student_number']}: course='{s['course']}', year='{s['year']}'")

print("\n--- VERIFICATION: Testing mark_absents logic ---")
import datetime
today = datetime.datetime.now().strftime('%Y-%m-%d')

schedules = conn.execute("SELECT * FROM schedules").fetchall()
for sched in schedules:
    students_in_sched = conn.execute(
        "SELECT student_number FROM students WHERE course=? AND year=?",
        (sched['target_course'], sched['target_year'])
    ).fetchall()
    print(f"Schedule '{sched['name']}' ({sched['target_course']}/{sched['target_year']}): {len(students_in_sched)} students")

conn.close()
print("\n✓ Fix complete!")
