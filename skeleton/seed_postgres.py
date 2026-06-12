"""
Seed PostgreSQL with all TransitFlow mock data from train-mock-data/.

Usage:
    python skeleton/seed_postgres.py

Run AFTER docker-compose up -d.
You must first design and create your tables in databases/relational/schema.sql.
Safe to re-run: implement your inserts with ON CONFLICT DO NOTHING.
"""

# TASK 6 EXTENSION: Adds seed functions seed_platform_assignments and
#   seed_delay_records (called from main()). See TASK6.md.

import json
import os
import sys

from argon2 import PasswordHasher
import psycopg2
from psycopg2.extras import execute_values

# ── resolve paths ────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR    = os.path.join(PROJECT_DIR, "train-mock-data")

sys.path.insert(0, PROJECT_DIR)
from skeleton import config as cfg


def load(filename):
    # Helper function to load json data from the mock data directory.
    # Returns a parsed dictionary or list from the specified file.
    with open(os.path.join(DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def connect():
    # Establish and return a connection to the PostgreSQL database.
    # Uses configuration parameters defined in the config module.
    return psycopg2.connect(
        host=cfg.PG_HOST,
        port=cfg.PG_PORT,
        dbname=cfg.PG_DB,
        user=cfg.PG_USER,
        password=cfg.PG_PASSWORD,
    )


def insert_many(cur, table, columns, rows):
    """Bulk insert with ON CONFLICT DO NOTHING. Returns row count inserted."""
    # Skip operation if there is no data to insert.
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT DO NOTHING"
    )
    execute_values(cur, sql, rows)
    return cur.rowcount


# ── seeders ──────────────────────────────────────────────────────────────────

def seed_metro_stations(cur):
    # Seed metro stations from json file.
    # Extracts station IDs and names to populate the metro_stations table.
    data = load("metro_stations.json")
    rows = [(d["station_id"], d["name"]) for d in data]
    insert_many(cur, "metro_stations", ["station_id", "name"], rows)


def seed_national_rail_stations(cur):
    # Seed national rail stations from json file.
    # Extracts station IDs and names to populate the national_rail_stations table.
    data = load("national_rail_stations.json")
    rows = [(d["station_id"], d["name"]) for d in data]
    insert_many(cur, "national_rail_stations", ["station_id", "name"], rows)


def seed_metro_schedules(cur):
    # Seed metro schedules, stops, and operating days.
    # Parses nested schedule data into multiple relational tables.
    data = load("metro_schedules.json")
    
    schedules = []
    stops = []
    days = []
    
    # Process each schedule entry from the JSON data.
    for d in data:
        sch_id = d["schedule_id"]
        schedules.append((
            sch_id, d["line"], d["direction"], d["origin_station_id"],
            d["destination_station_id"], d["first_train_time"], d["last_train_time"],
            d["base_fare_usd"], d["per_stop_rate_usd"], d["frequency_min"]
        ))
        
        # Iterate through the stops in order to preserve the sequence of the route.
        for idx, station_id in enumerate(d.get("stops_in_order", [])):
            travel_time = d.get("travel_time_from_origin_min", {}).get(station_id, 0)
            stops.append((
                sch_id, station_id, idx + 1, travel_time
            ))
            
        # Collect the operating days for this specific schedule.
        for day in d.get("operates_on", []):
            days.append((sch_id, day))
            
    insert_many(cur, "metro_schedules", [
        "schedule_id", "line", "direction", "origin_station_id", "destination_station_id",
        "first_train_time", "last_train_time", "base_fare_usd", "per_stop_rate_usd", "frequency_min"
    ], schedules)
    insert_many(cur, "metro_schedule_stops", ["schedule_id", "station_id", "stop_order", "travel_time_from_origin_min"], stops)
    insert_many(cur, "metro_schedule_days", ["schedule_id", "day_of_week"], days)


def seed_national_rail_schedules(cur):
    # Seed national rail schedules, stops, fares, and operating days.
    # Extracts schedule details including pass-through stations and fare classes.
    data = load("national_rail_schedules.json")
    
    # Map (line, direction) -> { "stops": [...], "times": {...} } from normal schedules
    normal_routes = {}
    for d in data:
        if d.get("service_type") == "normal":
            normal_routes[(d["line"], d["direction"])] = {
                "stops": d.get("stops_in_order", []),
                "times": d.get("travel_time_from_origin_min", {})
            }

    schedules = []
    stops = []
    fares = []
    days = []
    
    # Process each national rail schedule entry from the JSON data.
    for d in data:
        sch_id = d["schedule_id"]
        line = d["line"]
        direction = d["direction"]
        service_type = d["service_type"]
        
        schedules.append((
            sch_id, line, service_type, direction,
            d["origin_station_id"], d["destination_station_id"],
            d["first_train_time"], d["last_train_time"], d["frequency_min"]
        ))
        
        passed_through = set(d.get("passed_through_stations", []))
        
        if service_type == "express" and (line, direction) in normal_routes:
            full_route = normal_routes[(line, direction)]["stops"]
            normal_times = normal_routes[(line, direction)]["times"]
        else:
            full_route = d.get("stops_in_order", [])
            normal_times = {}

        for idx, station_id in enumerate(full_route):
            is_passed = station_id in passed_through
            
            if is_passed:
                travel_time = normal_times.get(station_id, 0)
            else:
                travel_time = d.get("travel_time_from_origin_min", {}).get(station_id, 0)
                
            stops.append((
                sch_id, station_id, idx + 1,
                travel_time, is_passed
            ))
            
        # Parse available fare classes and pricing rates for the schedule.
        for fare_class, prices in d.get("fare_classes", {}).items():
            fares.append((
                sch_id, fare_class, prices["base_fare_usd"], prices["per_stop_rate_usd"]
            ))
            
        # Collect the operating days for this national rail schedule.
        for day in d.get("operates_on", []):
            days.append((sch_id, day))
            
    insert_many(cur, "national_rail_schedules", [
        "schedule_id", "line", "service_type", "direction", "origin_station_id",
        "destination_station_id", "first_train_time", "last_train_time", "frequency_min"
    ], schedules)
    insert_many(cur, "national_rail_schedule_stops", [
        "schedule_id", "station_id", "stop_order", "travel_time_from_origin_min", "is_passed_through"
    ], stops)
    insert_many(cur, "national_rail_schedule_fares", ["schedule_id", "fare_class", "base_fare_usd", "per_stop_rate_usd"], fares)
    insert_many(cur, "national_rail_schedule_days", ["schedule_id", "day_of_week"], days)


def seed_seat_layouts(cur):
    # Seed seat layouts, coaches, and individual seats for national rail.
    # Generates unique coach IDs and maps seats to their respective layouts.
    data = load("national_rail_seat_layouts.json")
    
    layouts = []
    coaches = []
    seats = []
    
    # Process each layout mapping schedules to seat structures.
    for d in data:
        layout_id = d["layout_id"]
        layouts.append((layout_id, d["schedule_id"]))
        
        # Extract coach details for the layout.
        for c in d.get("coaches", []):
            coach_id = f"{layout_id}_{c['coach']}"
            coaches.append((coach_id, layout_id, c["coach"], c["fare_class"]))
            
            # Map each individual seat to its corresponding coach.
            for s in c.get("seats", []):
                seats.append((s["seat_id"], coach_id, s["row"], s["column"]))
                
    insert_many(cur, "national_rail_seat_layouts", ["layout_id", "schedule_id"], layouts)
    insert_many(cur, "national_rail_coaches", ["coach_id", "layout_id", "coach_name", "fare_class"], coaches)
    insert_many(cur, "national_rail_seats", ["seat_id", "coach_id", "row", "seat_column"], seats)


def seed_users(cur):
    # Seed registered users and their hashed passwords.
    # Uses argon2 to securely hash passwords before inserting them.
    data = load("registered_users.json")
    
    users = []
    passwords = []
    ph = PasswordHasher()
    
    # Process and hash passwords for each user profile.
    for d in data:
        user_id = d["user_id"]
        users.append((
            user_id, d["full_name"], d["email"], d.get("phone"), d.get("date_of_birth"),
            d.get("secret_question"),
            d.get("registered_at"), d.get("is_active", True)
        ))
        
        pwd = d["password"]
        hashed_pwd = ph.hash(pwd)
        
        # Hash the secret answer only if it is provided.
        secret_answer = d.get("secret_answer")
        hashed_secret_answer = ph.hash(secret_answer) if secret_answer else None
            
        passwords.append((user_id, hashed_pwd, hashed_secret_answer))
        
    insert_many(cur, "users", [
        "user_id", "full_name", "email", "phone", "date_of_birth",
        "secret_question", "registered_at", "is_active"
    ], users)
    insert_many(cur, "user_passwords", ["user_id", "password_hash", "secret_answer_hash"], passwords)


def seed_national_rail_bookings(cur):
    # Seed national rail bookings.
    # Resolves the correct coach ID for each booking based on the schedule layout.
    data = load("bookings.json")
    layouts_data = load("national_rail_seat_layouts.json")
    
    # Map (schedule_id, coach_name) -> coach_id
    # We need to find layout_id for a schedule to resolve the coach_id
    schedule_to_layout = {l["schedule_id"]: l["layout_id"] for l in layouts_data}
    
    bookings = []
    # Process bookings and resolve coach references.
    for d in data:
        sch_id = d["schedule_id"]
        coach_name = d.get("coach")
        
        coach_id = None
        # Check if the booking has a specific coach and matches a valid layout.
        if coach_name and sch_id in schedule_to_layout:
            layout_id = schedule_to_layout[sch_id]
            coach_id = f"{layout_id}_{coach_name}"
            
        bookings.append((
            d["booking_id"], d["user_id"], sch_id,
            d["origin_station_id"], d["destination_station_id"],
            d["travel_date"], d["departure_time"], d["ticket_type"],
            d["fare_class"], coach_id, d.get("seat_id"),
            d["stops_travelled"], d["amount_usd"], d["status"],
            d["booked_at"], d.get("travelled_at")
        ))
        
    insert_many(cur, "national_rail_bookings", [
        "booking_id", "user_id", "schedule_id", "origin_station_id", "destination_station_id",
        "travel_date", "departure_time", "ticket_type", "fare_class", "coach_id", "seat_id",
        "stops_travelled", "amount_usd", "status", "booked_at", "travelled_at"
    ], bookings)


def seed_metro_travels(cur):
    # Seed metro travel history.
    # Inserts trip records including ticket types and payment statuses.
    data = load("metro_travel_history.json")
    rows = []
    # Format and extract each metro travel history record.
    for d in data:
        rows.append((
            d["trip_id"], d["user_id"], d["schedule_id"],
            d["origin_station_id"], d["destination_station_id"],
            d["travel_date"], d["ticket_type"], d.get("day_pass_ref"),
            d["stops_travelled"], d["amount_usd"], d["status"],
            d["purchased_at"], d.get("travelled_at")
        ))
        
    insert_many(cur, "metro_travel_history", [
        "trip_id", "user_id", "schedule_id", "origin_station_id", "destination_station_id",
        "travel_date", "ticket_type", "day_pass_ref", "stops_travelled", "amount_usd",
        "status", "purchased_at", "travelled_at"
    ], rows)


def seed_payments(cur):
    # Seed payments for both metro and national rail.
    # Segregates payment records based on the booking ID prefix.
    data = load("payments.json")
    metro_payments = []
    rail_payments = []
    
    # Iterate through transactions and route them based on the booking prefix.
    for d in data:
        bid = d["booking_id"]
        row = (d["payment_id"], bid, d["amount_usd"], d["method"], d["status"], d["paid_at"])
        # IDs starting with 'MT' indicate Metro travel payments, otherwise Rail bookings.
        if bid.startswith("MT"):
            metro_payments.append(row)
        else:
            rail_payments.append(row)
            
    insert_many(cur, "metro_payments", ["payment_id", "trip_id", "amount_usd", "method", "status", "paid_at"], metro_payments)
    insert_many(cur, "national_rail_payments", ["payment_id", "booking_id", "amount_usd", "method", "status", "paid_at"], rail_payments)


def seed_feedback(cur):
    # Seed user feedbacks for both metro and national rail.
    # Segregates feedback records based on the booking ID prefix.
    data = load("feedback.json")
    metro_feedbacks = []
    rail_feedbacks = []
    
    # Iterate through feedback records and route them accordingly.
    for d in data:
        bid = d["booking_id"]
        row = (d["feedback_id"], bid, d["user_id"], d["rating"], d.get("comment"), d["submitted_at"])
        # IDs starting with 'MT' belong to Metro trips, otherwise Rail bookings.
        if bid.startswith("MT"):
            metro_feedbacks.append(row)
        else:
            rail_feedbacks.append(row)
            
    insert_many(cur, "metro_feedbacks", ["feedback_id", "trip_id", "user_id", "rating", "comment", "submitted_at"], metro_feedbacks)
    insert_many(cur, "national_rail_feedbacks", ["feedback_id", "booking_id", "user_id", "rating", "comment", "submitted_at"], rail_feedbacks)


# TASK 6 EXTENSION: seed the bonus platform-assignment data.
def seed_platform_assignments(cur):
    # Each JSON row is a (schedule, station) pair plus its platform number.
    # insert_many uses ON CONFLICT DO NOTHING, so re-running is idempotent —
    # the composite PK (schedule_id, station_id) prevents duplicate rows.
    data = load("platform_assignments.json")
    rows = [(r["schedule_id"], r["station_id"], r["platform_number"]) for r in data]
    n = insert_many(cur, "platform_assignments", ["schedule_id", "station_id", "platform_number"], rows)
    print(f"  platform_assignments: {n} rows")


# TASK 6 EXTENSION: seed the bonus historical delay log.
def seed_delay_records(cur):
    # `reason` is optional in the source data, so we default it to None via .get();
    # delay_id is UNIQUE, so ON CONFLICT DO NOTHING keeps the seed idempotent.
    data = load("delay_records.json")
    rows = [
        (r["delay_id"], r["schedule_id"], r["travel_date"], r["delay_minutes"], r.get("reason"))
        for r in data
    ]
    n = insert_many(cur, "delay_records", ["delay_id", "schedule_id", "travel_date", "delay_minutes", "reason"], rows)
    print(f"  delay_records: {n} rows")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    # Main function to execute all seeders in the correct dependency order.
    # Manages the transaction, committing on success and rolling back on failure.
    print("Connecting to PostgreSQL...")
    conn = connect()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Seeding tables (dependency order):")
        seed_metro_stations(cur)
        seed_national_rail_stations(cur)
        seed_metro_schedules(cur)
        seed_national_rail_schedules(cur)
        seed_seat_layouts(cur)
        seed_users(cur)
        seed_national_rail_bookings(cur)
        seed_metro_travels(cur)
        seed_payments(cur)
        seed_feedback(cur)
        seed_platform_assignments(cur)
        seed_delay_records(cur)
        conn.commit()
        print("\nAll done. Database seeded successfully.")
    except Exception as e:
        conn.rollback()
        print(f"\nError: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
