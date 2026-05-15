#!/usr/bin/env python3
"""Check if course formats match between schedules and students"""
import sqlite3

conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row

print("=" * 70)
print("COURSE FORMAT VERIFICATION")
print("=" * 70)

print("\n--- SCHEDULE TARGET COURSES ---")
schedules = conn.execute("SELECT DISTINCT target_course FROM schedules").fetchall()
schedule_courses = set()
for s in schedules:
    course = s['target_course']
    schedule_courses.add(course)
    print(f"  '{course}'")

print("\n--- STUDENT COURSES ---")
students = conn.execute("SELECT DISTINCT course FROM students").fetchall()
student_courses = set()
for s in students:
    course = s['course']
    student_courses.add(course)
    print(f"  '{course}'")

print("\n--- MATCHING CHECK ---")
print(f"Schedule courses: {schedule_courses}")
print(f"Student courses:  {student_courses}")

missing_in_students = schedule_courses - student_courses
extra_in_students = student_courses - schedule_courses

if missing_in_students:
    print(f"\n❌ MISMATCH: Schedules expect these courses but NO students have them:")
    for course in missing_in_students:
        print(f"   '{course}'")

if extra_in_students:
    print(f"\n⚠️  EXTRA: Students have these courses but NO schedules use them:")
    for course in extra_in_students:
        print(f"   '{course}'")

if not missing_in_students and not extra_in_students:
    print(f"\n✓ PERFECT: All course formats match!")

print("\n--- DETAILED BREAKDOWN ---")
print("\nSchedules grouped by course:")
sched_by_course = conn.execute("""
    SELECT target_course, COUNT(*) as count
    FROM schedules
    GROUP BY target_course
""").fetchall()
for row in sched_by_course:
    print(f"  '{row['target_course']}': {row['count']} schedule(s)")

print("\nStudents grouped by course:")
stud_by_course = conn.execute("""
    SELECT course, COUNT(*) as count
    FROM students
    GROUP BY course
""").fetchall()
for row in stud_by_course:
    print(f"  '{row['course']}': {row['count']} student(s)")

conn.close()
