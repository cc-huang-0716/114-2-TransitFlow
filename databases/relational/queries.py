"""
TransitFlow — PostgreSQL / Relational Database Layer
=====================================================
This module handles all queries to PostgreSQL.

TWO ROLES ARE SERVED HERE:
  1. Relational  → dual-network transit (metro + national rail),
                   availability, fares, bookings, seat selection
  2. Vector      → policy document similarity search (pgvector)

STUDENT TASK
------------
Design your schema in databases/relational/schema.sql, seed it with
skeleton/seed_postgres.py, then implement the query functions below.

Functions prefixed with `query_`  are read-only lookups called by the agent.
Functions prefixed with `execute_` are write operations (booking/cancellation).

The vector functions (query_policy_vector_search, store_policy_document)
are already implemented — do not modify them.
"""

# TASK 6 EXTENSION: This file contains bonus-feature query functions
#   query_platform_assignment, query_service_delays, query_loyalty_balance,
#   plus loyalty-point award logic inside execute_booking. See TASK6.md.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

from skeleton.config import PG_DSN, VECTOR_TOP_K, VECTOR_SIMILARITY_THRESHOLD


def _connect():
    """Return a new psycopg2 connection with autocommit enabled."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _gen_booking_id() -> str:
    # UUID4 matches the UUID primary key type on national_rail_bookings.
    # Stored in the booking_id (business ID) column so it is shown to users.
    return f"BK-{uuid.uuid4().hex[:8].upper()}"


def _gen_payment_id() -> str:
    # UUID4 matches the UUID primary key type on the payments tables.
    return f"PM-{uuid.uuid4().hex[:8].upper()}"


# ── Example ───────────────────────────────────────────────────────────────────
# The block below shows the query pattern: open a cursor, run SQL, return rows.
# Use _connect() for read-only queries; for write operations use a manual
# connection with conn.commit() / conn.rollback() (see execute_booking below).

def example_query() -> dict:
    """Example: returns the name of the connected database."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT current_database() AS db;")
            return dict(cur.fetchone())

# TODO: Implement the query_ and execute_ functions below.
# ─────────────────────────────────────────────────────────────────────────────


# ── NATIONAL RAIL AVAILABILITY ────────────────────────────────────────────────

def query_national_rail_availability(
    origin_id: str,
    destination_id: str,
    travel_date: Optional[str] = None,
) -> list[dict]:
    """
    Return national rail schedules that serve both origin and destination stations
    in the correct order, along with seat occupancy for the requested travel date.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        travel_date:     e.g. "2025-06-01" — used to count bookings; omit for general info
    """
    # Why two separate JOINs on schedule_stops (orig_stop & dest_stop)?
    # A single schedule serves many stations; we need to confirm BOTH the
    # origin and destination appear on the same schedule AND that the origin
    # comes before the destination (stop_order comparison). Filtering
    # is_passed_through = FALSE ensures express trains that skip a station
    # are not returned as valid options for that station.
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            params: list = [origin_id, destination_id]

            select_cols = """
                SELECT
                    s.schedule_id,
                    s.line,
                    s.service_type,
                    s.direction,
                    orig_st.name  AS origin_name,
                    dest_st.name  AS destination_name,
                    s.first_train_time::text,
                    s.last_train_time::text,
                    s.frequency_min,
                    orig_stop.stop_order  AS origin_stop_order,
                    dest_stop.stop_order  AS dest_stop_order,
                    (dest_stop.stop_order - orig_stop.stop_order) AS stops_travelled,
                    (dest_stop.travel_time_from_origin_min
                     - orig_stop.travel_time_from_origin_min) AS travel_time_min
            """

            if travel_date:
                select_cols += """,
                    COALESCE(seat_counts.total_seats, 0)    AS total_seats,
                    COALESCE(booking_counts.booked_seats, 0) AS booked_seats,
                    COALESCE(seat_counts.total_seats, 0)
                      - COALESCE(booking_counts.booked_seats, 0) AS available_seats
                """

            from_clause = """
                FROM national_rail_schedules s
                JOIN national_rail_schedule_stops orig_stop
                    ON orig_stop.schedule_id = s.schedule_id
                   AND orig_stop.station_id  = %s
                   AND orig_stop.is_passed_through = FALSE
                JOIN national_rail_schedule_stops dest_stop
                    ON dest_stop.schedule_id = s.schedule_id
                   AND dest_stop.station_id  = %s
                   AND dest_stop.is_passed_through = FALSE
                JOIN national_rail_stations orig_st
                    ON orig_st.station_id = orig_stop.station_id
                JOIN national_rail_stations dest_st
                    ON dest_st.station_id = dest_stop.station_id
            """

            if travel_date:
                from_clause += """
                LEFT JOIN (
                    SELECT sl.schedule_id, COUNT(*) AS total_seats
                    FROM national_rail_seat_layouts sl
                    JOIN national_rail_coaches c  ON c.layout_id  = sl.layout_id
                    JOIN national_rail_seats   st ON st.coach_id  = c.coach_id
                    GROUP BY sl.schedule_id
                ) seat_counts ON seat_counts.schedule_id = s.schedule_id
                LEFT JOIN (
                    SELECT b.schedule_id, COUNT(*) AS booked_seats
                    FROM national_rail_bookings b
                    WHERE b.travel_date = %s AND b.status != 'cancelled'
                    GROUP BY b.schedule_id
                ) booking_counts ON booking_counts.schedule_id = s.schedule_id
                JOIN national_rail_schedule_days sd
                    ON sd.schedule_id = s.schedule_id
                   AND sd.day_of_week = %s
                """
                from datetime import date as _date
                d = _date.fromisoformat(travel_date)
                day_abbr = d.strftime("%a").lower()   # e.g. 'mon', 'tue'
                params.extend([travel_date, day_abbr])

            where_clause = """
                WHERE orig_stop.stop_order < dest_stop.stop_order
                ORDER BY s.schedule_id
            """

            cur.execute(select_cols + from_clause + where_clause, params)
            return [dict(row) for row in cur.fetchall()]


def query_national_rail_fare(
    schedule_id: str,
    fare_class: str,
    stops_travelled: int,
) -> Optional[dict]:
    """
    Calculate the fare for a national rail journey.

    Args:
        schedule_id:     e.g. "NR_SCH01"
        fare_class:      "standard" or "first"
        stops_travelled: number of stops between origin and destination (inclusive)

    Returns:
        dict with fare_class, base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    sql = """
        SELECT fare_class,
               base_fare_usd,
               per_stop_rate_usd,
               (base_fare_usd + per_stop_rate_usd * %s) AS total_fare_usd
        FROM national_rail_schedule_fares
        WHERE schedule_id = %s AND fare_class = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (stops_travelled, schedule_id, fare_class))
            row = cur.fetchone()
            return dict(row) if row else None


# ── METRO SCHEDULES & FARE ────────────────────────────────────────────────────

def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    """
    Return metro schedules that serve both origin and destination in the correct order.

    Args:
        origin_id:       e.g. "MS01"
        destination_id:  e.g. "MS09"
    """
    sql = """
        SELECT
            s.schedule_id,
            s.line,
            s.direction,
            orig_st.name  AS origin_name,
            dest_st.name  AS destination_name,
            s.first_train_time::text,
            s.last_train_time::text,
            s.frequency_min,
            s.base_fare_usd,
            s.per_stop_rate_usd,
            orig_stop.stop_order  AS origin_stop_order,
            dest_stop.stop_order  AS dest_stop_order,
            (dest_stop.stop_order - orig_stop.stop_order) AS stops_travelled,
            (dest_stop.travel_time_from_origin_min
             - orig_stop.travel_time_from_origin_min) AS travel_time_min,
            (
                SELECT json_agg(sub.station_id ORDER BY sub.stop_order)
                FROM metro_schedule_stops sub
                WHERE sub.schedule_id = s.schedule_id
            ) AS stops_in_order
        FROM metro_schedules s
        JOIN metro_schedule_stops orig_stop
            ON orig_stop.schedule_id = s.schedule_id
           AND orig_stop.station_id  = %s
        JOIN metro_schedule_stops dest_stop
            ON dest_stop.schedule_id = s.schedule_id
           AND dest_stop.station_id  = %s
        JOIN metro_stations orig_st ON orig_st.station_id = orig_stop.station_id
        JOIN metro_stations dest_st ON dest_st.station_id = dest_stop.station_id
        WHERE orig_stop.stop_order < dest_stop.stop_order
        ORDER BY s.schedule_id
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (origin_id, destination_id))
            return [dict(row) for row in cur.fetchall()]


def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]:
    """
    Calculate the metro fare for a single-ticket journey.

    Args:
        schedule_id:     e.g. "MS_SCH01"
        stops_travelled: number of stops between origin and destination

    Returns:
        dict with base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    sql = """
        SELECT base_fare_usd,
               per_stop_rate_usd,
               (base_fare_usd + per_stop_rate_usd * %s) AS total_fare_usd
        FROM metro_schedules
        WHERE schedule_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (stops_travelled, schedule_id))
            row = cur.fetchone()
            return dict(row) if row else None


# ── SEAT SELECTION ────────────────────────────────────────────────────────────

def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
) -> list[dict]:
    """
    Return available seats for a national rail journey on a given date.

    Args:
        schedule_id:  e.g. "NR_SCH01"
        travel_date:  e.g. "2025-06-01"
        fare_class:   "standard" or "first"

    Returns:
        List of dicts: {seat_id, coach, row, column}
    """
    # Why NOT EXISTS instead of a LEFT JOIN + NULL check?
    # NOT EXISTS short-circuits as soon as it finds ONE matching booking,
    # making it faster for schedules with many bookings. It also avoids
    # inflating the result set the way a LEFT JOIN would when a seat has
    # multiple cancelled bookings on different dates.
    sql = """
        SELECT st.seat_id,
               c.coach_name AS coach,
               st.row,
               st.seat_column AS column
        FROM national_rail_seats st
        JOIN national_rail_coaches c      ON c.coach_id  = st.coach_id
        JOIN national_rail_seat_layouts l ON l.layout_id = c.layout_id
        WHERE l.schedule_id = %s
          AND c.fare_class  = %s
          AND NOT EXISTS (
              SELECT 1
              FROM national_rail_bookings b
              WHERE b.schedule_id = %s
                AND b.travel_date = %s
                AND b.coach_id    = st.coach_id
                AND b.seat_id     = st.seat_id
                AND b.status     != 'cancelled'
          )
        ORDER BY c.coach_name, st.row, st.seat_column
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (schedule_id, fare_class, schedule_id, travel_date))
            return [dict(row) for row in cur.fetchall()]


def auto_select_adjacent_seats(available_seats: list[dict], count: int) -> list[str]:
    """
    Select `count` seats that are as close together as possible (same row preferred,
    then adjacent rows). Returns a list of seat_ids.

    Args:
        available_seats: output of query_available_seats()
        count:           number of seats needed
    """
    if not available_seats or count <= 0:
        return []
    if count >= len(available_seats):
        return [s["seat_id"] for s in available_seats[:count]]

    from collections import defaultdict
    rows: dict[int, list[dict]] = defaultdict(list)
    for seat in available_seats:
        rows[seat["row"]].append(seat)

    for row_seats in sorted(rows.values(), key=lambda s: s[0]["row"]):
        if len(row_seats) >= count:
            return [s["seat_id"] for s in row_seats[:count]]

    sorted_seats = sorted(available_seats, key=lambda s: (s["row"], s["column"]))
    return [s["seat_id"] for s in sorted_seats[:count]]


# ── USER & BOOKING QUERIES ────────────────────────────────────────────────────

def query_user_profile(user_email: str) -> Optional[dict]:
    """Return a user's profile by email."""
    sql = """
        SELECT user_id, full_name, email, phone,
               date_of_birth::text, is_active
        FROM users
        WHERE email = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (user_email,))
            row = cur.fetchone()
            return dict(row) if row else None


def query_user_bookings(user_email: str) -> dict:
    """
    Return a user's combined booking history (national rail + metro).

    Returns:
        dict with keys 'national_rail' (list) and 'metro' (list)
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Resolve user_id from email
            cur.execute("SELECT user_id FROM users WHERE email = %s", (user_email,))
            user_row = cur.fetchone()
            if not user_row:
                return {"national_rail": [], "metro": []}
            user_id = user_row["user_id"]

            # National rail bookings
            cur.execute("""
                SELECT b.booking_id, b.travel_date::text, b.departure_time::text,
                       b.ticket_type, b.fare_class, b.stops_travelled,
                       b.amount_usd, b.status,
                       orig.name AS origin_name, dest.name AS destination_name,
                       b.schedule_id, b.coach_id, b.seat_id
                FROM national_rail_bookings b
                JOIN national_rail_stations orig ON orig.station_id = b.origin_station_id
                JOIN national_rail_stations dest ON dest.station_id = b.destination_station_id
                WHERE b.user_id = %s
                ORDER BY b.travel_date DESC
            """, (user_id,))
            rail = [dict(row) for row in cur.fetchall()]

            # Metro trips
            cur.execute("""
                SELECT t.trip_id, t.travel_date::text,
                       t.ticket_type, t.stops_travelled,
                       t.amount_usd, t.status,
                       orig.name AS origin_name, dest.name AS destination_name,
                       t.schedule_id, t.day_pass_ref
                FROM metro_travel_history t
                JOIN metro_stations orig ON orig.station_id = t.origin_station_id
                JOIN metro_stations dest ON dest.station_id = t.destination_station_id
                WHERE t.user_id = %s
                ORDER BY t.travel_date DESC
            """, (user_id,))
            metro = [dict(row) for row in cur.fetchall()]

            return {"national_rail": rail, "metro": metro}


def query_payment_info(booking_id: str) -> Optional[dict]:
    """Return payment record for a booking or metro trip."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if booking_id.startswith("MT"):
                sql = """
                    SELECT payment_id, trip_id AS booking_id,
                           amount_usd, method, status, paid_at::text
                    FROM metro_payments
                    WHERE trip_id = %s
                """
            else:
                sql = """
                    SELECT payment_id, booking_id,
                           amount_usd, method, status, paid_at::text
                    FROM national_rail_payments
                    WHERE booking_id = %s
                """
            cur.execute(sql, (booking_id,))
            row = cur.fetchone()
            return dict(row) if row else None


# ── TRANSACTIONAL OPERATIONS ──────────────────────────────────────────────────

def execute_booking(
    user_id: str,
    schedule_id: str,
    origin_station_id: str,
    destination_station_id: str,
    travel_date: str,
    fare_class: str,
    seat_id: str,
    ticket_type: str = "single",
) -> tuple[bool, dict | str]:
    """
    Create a national rail booking for a logged-in user.

    Args:
        user_id:                e.g. "RU01" — must match the logged-in user
        schedule_id:            e.g. "NR_SCH01"
        origin_station_id:      e.g. "NR01"
        destination_station_id: e.g. "NR05"
        travel_date:            e.g. "2025-06-01"
        fare_class:             "standard" or "first"
        seat_id:                e.g. "B05" (or "any" to auto-assign)
        ticket_type:            "single" (default) or "return"

    Returns:
        (True, booking_dict)   on success
        (False, error_message) on failure
    """
    # Why disable autocommit and use a manual transaction?
    # Booking creation involves TWO inserts (booking + payment) that must
    # succeed or fail atomically. If the payment insert fails after the
    # booking was already committed, we'd have an orphaned booking with no
    # payment record — violating data integrity.
    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Calculate stops_travelled
            cur.execute("""
                SELECT stop_order FROM national_rail_schedule_stops
                WHERE schedule_id = %s AND station_id = %s AND is_passed_through = FALSE
            """, (schedule_id, origin_station_id))
            orig_row = cur.fetchone()
            if not orig_row:
                conn.rollback()
                return (False, f"Origin station {origin_station_id} not found on schedule {schedule_id}.")

            cur.execute("""
                SELECT stop_order FROM national_rail_schedule_stops
                WHERE schedule_id = %s AND station_id = %s AND is_passed_through = FALSE
            """, (schedule_id, destination_station_id))
            dest_row = cur.fetchone()
            if not dest_row:
                conn.rollback()
                return (False, f"Destination station {destination_station_id} not found on schedule {schedule_id}.")

            stops_travelled = dest_row["stop_order"] - orig_row["stop_order"]
            if stops_travelled <= 0:
                conn.rollback()
                return (False, "Destination must come after origin on this schedule.")

            # 2. Calculate fare
            fare = query_national_rail_fare(schedule_id, fare_class, stops_travelled)
            if not fare:
                conn.rollback()
                return (False, f"No fare found for {fare_class} class on schedule {schedule_id}.")
            amount_usd = float(fare["total_fare_usd"])

            # 3. Get departure time from schedule
            cur.execute(
                "SELECT first_train_time::text FROM national_rail_schedules WHERE schedule_id = %s",
                (schedule_id,),
            )
            sch_row = cur.fetchone()
            departure_time = sch_row["first_train_time"] if sch_row else "00:00"

            # 4. Resolve seat
            actual_coach_id = None
            actual_seat_id = None

            if seat_id.lower() == "any":
                available = query_available_seats(schedule_id, travel_date, fare_class)
                if not available:
                    conn.rollback()
                    return (False, "No seats available for this service and date.")
                selected = auto_select_adjacent_seats(available, 1)
                if not selected:
                    conn.rollback()
                    return (False, "Could not auto-assign a seat.")
                # Find the full seat info to get coach_id
                chosen = next(s for s in available if s["seat_id"] == selected[0])
                actual_seat_id = chosen["seat_id"]
                # Resolve coach_id from coach_name
                cur.execute("""
                    SELECT c.coach_id
                    FROM national_rail_coaches c
                    JOIN national_rail_seat_layouts l ON l.layout_id = c.layout_id
                    WHERE l.schedule_id = %s AND c.coach_name = %s AND c.fare_class = %s
                """, (schedule_id, chosen["coach"], fare_class))
                coach_row = cur.fetchone()
                actual_coach_id = coach_row["coach_id"] if coach_row else None
            else:
                # Specific seat requested — find it by seat_id
                cur.execute("""
                    SELECT st.seat_id, c.coach_id, c.coach_name
                    FROM national_rail_seats st
                    JOIN national_rail_coaches c ON c.coach_id = st.coach_id
                    JOIN national_rail_seat_layouts l ON l.layout_id = c.layout_id
                    WHERE l.schedule_id = %s AND c.fare_class = %s AND st.seat_id = %s
                """, (schedule_id, fare_class, seat_id))
                seat_row = cur.fetchone()
                if not seat_row:
                    conn.rollback()
                    return (False, f"Seat {seat_id} not found for {fare_class} class on this schedule.")

                # Check if already booked
                cur.execute("""
                    SELECT 1 FROM national_rail_bookings
                    WHERE schedule_id = %s AND travel_date = %s
                      AND coach_id = %s AND seat_id = %s AND status != 'cancelled'
                """, (schedule_id, travel_date, seat_row["coach_id"], seat_id))
                if cur.fetchone():
                    conn.rollback()
                    return (False, f"Seat {seat_id} is already booked for this date.")

                actual_coach_id = seat_row["coach_id"]
                actual_seat_id = seat_row["seat_id"]

            # 5. Create booking
            booking_id = _gen_booking_id()
            now = datetime.now(timezone.utc)

            cur.execute("""
                INSERT INTO national_rail_bookings
                    (booking_id, user_id, schedule_id,
                     origin_station_id, destination_station_id,
                     travel_date, departure_time, ticket_type, fare_class,
                     coach_id, seat_id, stops_travelled, amount_usd,
                     status, booked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', %s)
            """, (
                booking_id, user_id, schedule_id,
                origin_station_id, destination_station_id,
                travel_date, departure_time, ticket_type, fare_class,
                actual_coach_id, actual_seat_id, stops_travelled, amount_usd,
                now,
            ))

            # 6. Create payment record
            payment_id = _gen_payment_id()
            cur.execute("""
                INSERT INTO national_rail_payments
                    (payment_id, booking_id, amount_usd, method, status, paid_at)
                VALUES (%s, %s, %s, 'credit_card', 'paid', %s)
            """, (payment_id, booking_id, amount_usd, now))

            # 7. Award loyalty points (1 point per $1 spent, minimum 1)
            points_earned = max(1, int(float(amount_usd)))
            cur.execute(
                "UPDATE users SET loyalty_points = loyalty_points + %s WHERE user_id = %s",
                (points_earned, user_id),
            )
            cur.execute(
                "INSERT INTO loyalty_transactions (user_id, trip_ref, points, reason) "
                "VALUES (%s, %s, %s, %s)",
                (user_id, booking_id, points_earned, "booking"),
            )

            conn.commit()
            return (True, {
                "booking_id": booking_id,
                "schedule_id": schedule_id,
                "origin_station_id": origin_station_id,
                "destination_station_id": destination_station_id,
                "travel_date": travel_date,
                "departure_time": departure_time,
                "ticket_type": ticket_type,
                "fare_class": fare_class,
                "seat_id": actual_seat_id,
                "stops_travelled": stops_travelled,
                "amount_usd": amount_usd,
                "status": "confirmed",
                "payment_id": payment_id,
            })

    except Exception as e:
        conn.rollback()
        return (False, f"Booking failed: {e}")
    finally:
        conn.close()


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel a national rail booking owned by the given user.

    Calculates the refund amount according to the booking's service type:
      - Normal service: RF001 windows (100% / 75% / 50% / 0%)
      - Express service: RF002 windows (100% / 50% / 0%)

    Args:
        booking_id: e.g. "BK001"
        user_id:    must match the booking's user_id

    Returns:
        (True, result_dict)  with refund_amount_usd and policy note
        (False, error_msg)
    """
    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Fetch booking and verify ownership
            cur.execute("""
                SELECT b.*, s.service_type
                FROM national_rail_bookings b
                JOIN national_rail_schedules s ON s.schedule_id = b.schedule_id
                WHERE b.booking_id = %s
            """, (booking_id,))
            booking = cur.fetchone()

            if not booking:
                conn.rollback()
                return (False, f"Booking {booking_id} not found.")
            if booking["user_id"] != user_id:
                conn.rollback()
                return (False, "This booking does not belong to you.")
            if booking["status"] == "cancelled":
                conn.rollback()
                return (False, "This booking has already been cancelled.")
            if booking["status"] == "completed":
                conn.rollback()
                return (False, "Cannot cancel a completed journey.")

            # 2. Calculate hours until departure
            from datetime import date as _date
            travel_dt = datetime.combine(
                booking["travel_date"] if isinstance(booking["travel_date"], _date)
                    else _date.fromisoformat(str(booking["travel_date"])),
                datetime.strptime(str(booking["departure_time"]), "%H:%M:%S").time()
                    if ":" in str(booking["departure_time"])
                    else datetime.strptime(str(booking["departure_time"]), "%H:%M").time(),
                tzinfo=timezone.utc,
            )
            now = datetime.now(timezone.utc)
            hours_before = (travel_dt - now).total_seconds() / 3600

            # 3. Determine refund based on service type and time window
            # Why separate refund policies per service_type?
            # Express services (RF002) have stricter cancellation windows
            # because express trains have limited capacity and higher demand.
            # Normal services (RF001) offer more gradual refund tiers to
            # encourage early cancellations and allow re-selling the seat.
            service_type = booking["service_type"]
            amount = float(booking["amount_usd"])

            if service_type == "express":
                # RF002: Express service refund policy
                policy_id = "RF002"
                if hours_before >= 48:
                    refund_pct = 100
                    admin_fee = 1.00
                    window = "Early cancellation (≥48h before departure)"
                elif hours_before >= 24:
                    refund_pct = 50
                    admin_fee = 1.00
                    window = "Late cancellation (24–48h before departure)"
                else:
                    refund_pct = 0
                    admin_fee = 0.00
                    window = "No refund (<24h before departure)"
            else:
                # RF001: Normal service refund policy
                policy_id = "RF001"
                if hours_before >= 48:
                    refund_pct = 100
                    admin_fee = 0.00
                    window = "Early cancellation (≥48h before departure)"
                elif hours_before >= 24:
                    refund_pct = 75
                    admin_fee = 0.50
                    window = "Standard cancellation (24–48h before departure)"
                elif hours_before >= 2:
                    refund_pct = 50
                    admin_fee = 0.50
                    window = "Late cancellation (2–24h before departure)"
                else:
                    refund_pct = 0
                    admin_fee = 0.00
                    window = "No refund (<2h before departure)"

            refund_amount = round(amount * refund_pct / 100 - admin_fee, 2)
            if refund_amount < 0:
                refund_amount = 0.00

            # 4. Update booking status
            cur.execute("""
                UPDATE national_rail_bookings
                SET status = 'cancelled'
                WHERE booking_id = %s
            """, (booking_id,))

            # 5. Insert refund payment record if applicable
            if refund_amount > 0:
                refund_payment_id = _gen_payment_id()
                cur.execute("""
                    INSERT INTO national_rail_payments
                        (payment_id, booking_id, amount_usd, method, status, paid_at)
                    VALUES (%s, %s, %s, 'credit_card', 'refunded', %s)
                """, (refund_payment_id, booking_id, refund_amount, now))

            conn.commit()
            return (True, {
                "booking_id": booking_id,
                "status": "cancelled",
                "policy_applied": policy_id,
                "cancellation_window": window,
                "original_amount_usd": amount,
                "refund_percent": refund_pct,
                "admin_fee_usd": admin_fee,
                "refund_amount_usd": refund_amount,
            })

    except Exception as e:
        conn.rollback()
        return (False, f"Cancellation failed: {e}")
    finally:
        conn.close()


# ── AUTHENTICATION QUERIES ────────────────────────────────────────────────────

def register_user(
    email: str,
    first_name: str,
    surname: str,
    year_of_birth: int,
    password: str,
    secret_question: str,
    secret_answer: str,
) -> tuple[bool, str]:
    """
    Register a new user.
    Returns (True, user_id) on success or (False, error_message) on failure.

    NOTE: passwords are stored as plain text here intentionally for teaching
    purposes. In production, replace with a salted hash (e.g. bcrypt).
    """
    from argon2 import PasswordHasher

    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check for duplicate email
            cur.execute("SELECT 1 FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                conn.rollback()
                return (False, "An account with this email already exists.")

            # Generate next user_id business key (RU + sequential number).
            # This is stored in the user_id column (UNIQUE NOT NULL);
            # the actual UUID primary key is auto-generated by the database.
            cur.execute("SELECT user_id FROM users ORDER BY user_id DESC LIMIT 1")
            last = cur.fetchone()
            if last:
                try:
                    num = int(last["user_id"].replace("RU", "")) + 1
                except ValueError:
                    num = 1
            else:
                num = 1
            user_id = f"RU{num:02d}"

            full_name = f"{first_name} {surname}"
            date_of_birth = f"{year_of_birth}-01-01"  # approximate

            cur.execute("""
                INSERT INTO users (user_id, full_name, email, date_of_birth, secret_question)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, full_name, email, date_of_birth, secret_question))

            ph = PasswordHasher()
            hashed_password = ph.hash(password)
            hashed_answer = ph.hash(secret_answer)

            cur.execute("""
                INSERT INTO user_passwords (user_id, password_hash, secret_answer_hash)
                VALUES (%s, %s, %s)
            """, (user_id, hashed_password, hashed_answer))

            conn.commit()
            return (True, user_id)
    except Exception as e:
        conn.rollback()
        return (False, f"Registration failed: {e}")
    finally:
        conn.close()


def login_user(email: str, password: str) -> Optional[dict]:
    """
    Verify credentials. Returns a user dict on success or None on failure.
    Dict keys: user_id, email, full_name, first_name, surname, phone, date_of_birth, is_active.
    """
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    sql = """
        SELECT u.user_id, u.email, u.full_name, u.phone,
               u.date_of_birth::text, u.is_active,
               p.password_hash
        FROM users u
        JOIN user_passwords p ON p.user_id = u.user_id
        WHERE u.email = %s AND u.is_active = TRUE
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (email,))
            row = cur.fetchone()
            if not row:
                return None

            ph = PasswordHasher()
            try:
                ph.verify(row["password_hash"], password)
            except VerifyMismatchError:
                return None

            # Split full_name into first_name and surname for the UI
            parts = row["full_name"].split(" ", 1)
            first_name = parts[0]
            surname = parts[1] if len(parts) > 1 else ""

            return {
                "user_id": row["user_id"],
                "email": row["email"],
                "full_name": row["full_name"],
                "first_name": first_name,
                "surname": surname,
                "phone": row["phone"],
                "date_of_birth": row["date_of_birth"],
                "is_active": row["is_active"],
            }


def get_user_secret_question(email: str) -> Optional[str]:
    """Return the secret question for a registered email, or None if not found."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT secret_question FROM users WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def verify_secret_answer(email: str, answer: str) -> bool:
    """Return True if the provided answer matches the stored secret answer (case-insensitive)."""
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    sql = """
        SELECT p.secret_answer_hash
        FROM user_passwords p
        JOIN users u ON u.user_id = p.user_id
        WHERE u.email = %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (email,))
            row = cur.fetchone()
            if not row or not row[0]:
                return False

            ph = PasswordHasher()
            try:
                ph.verify(row[0], answer)
                return True
            except VerifyMismatchError:
                return False


def update_password(email: str, new_password: str) -> bool:
    """Update the password for a user. Returns True if the row was updated."""
    from argon2 import PasswordHasher

    ph = PasswordHasher()
    hashed = ph.hash(new_password)
    sql = """
        UPDATE user_passwords
        SET password_hash = %s
        WHERE user_id = (SELECT user_id FROM users WHERE email = %s)
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (hashed, email))
            return cur.rowcount > 0


# ── PLATFORM ASSIGNMENTS ─────────────────────────────────────────────────────

def query_platform_assignment(schedule_id: str, station_id: str) -> Optional[dict]:
    """
    Return the platform number for a national rail service at a specific station.

    Args:
        schedule_id: e.g. "NR_SCH01"
        station_id:  e.g. "NR01"

    Returns:
        dict with schedule_id, station_id, station_name, platform_number, line, service_type;
        or None if no record exists.
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    pa.schedule_id,
                    pa.station_id,
                    st.name          AS station_name,
                    pa.platform_number,
                    nrs.line,
                    nrs.service_type,
                    nrs.direction
                FROM platform_assignments pa
                JOIN national_rail_stations  st  ON st.station_id   = pa.station_id
                JOIN national_rail_schedules nrs ON nrs.schedule_id = pa.schedule_id
                WHERE pa.schedule_id = %s AND pa.station_id = %s
            """, (schedule_id, station_id))
            row = cur.fetchone()
            return dict(row) if row else None


# ── SERVICE DELAY RECORDS ─────────────────────────────────────────────────────

def query_service_delays(
    schedule_id: Optional[str] = None,
    travel_date: Optional[str] = None,
) -> list[dict]:
    """
    Return historical delay records for national rail services.

    Both parameters are optional — returns all records when omitted.

    Args:
        schedule_id: e.g. "NR_SCH01" (optional)
        travel_date: "YYYY-MM-DD" (optional)

    Returns:
        List of dicts with delay_id, schedule_id, line, service_type,
        travel_date, delay_minutes, reason, reported_at.
    """
    conditions = []
    params: list = []
    if schedule_id:
        conditions.append("dr.schedule_id = %s")
        params.append(schedule_id)
    if travel_date:
        conditions.append("dr.travel_date = %s")
        params.append(travel_date)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            dr.delay_id,
            dr.schedule_id,
            nrs.line,
            nrs.service_type,
            dr.travel_date::text,
            dr.delay_minutes,
            dr.reason,
            dr.reported_at::text
        FROM delay_records dr
        JOIN national_rail_schedules nrs ON nrs.schedule_id = dr.schedule_id
        {where_clause}
        ORDER BY dr.travel_date DESC, dr.delay_id
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


# ── LOYALTY POINTS ────────────────────────────────────────────────────────────

def query_loyalty_balance(user_email: str) -> Optional[dict]:
    """
    Return the loyalty points balance and recent transactions for a user.

    Args:
        user_email: the logged-in user's email address

    Returns:
        dict with full_name, loyalty_points, recent_transactions (last 5);
        or None if the user is not found.
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id, full_name, loyalty_points
                FROM users
                WHERE email = %s AND is_active = TRUE
            """, (user_email,))
            user = cur.fetchone()
            if not user:
                return None

            cur.execute("""
                SELECT trip_ref, points, reason, created_at::text
                FROM loyalty_transactions
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 5
            """, (user["user_id"],))
            transactions = [dict(row) for row in cur.fetchall()]

            return {
                "full_name":           user["full_name"],
                "loyalty_points":      user["loyalty_points"],
                "recent_transactions": transactions,
            }


# ── VECTOR / RAG QUERIES — do not modify ─────────────────────────────────────

def query_policy_vector_search(embedding: list[float], top_k: int = VECTOR_TOP_K) -> list[dict]:
    """
    Find the most relevant policy documents for a given query embedding.

    Args:
        embedding: Query vector from llm.embed(user_question)
        top_k:     Number of results to return

    Returns:
        List of dicts with title, category, content, and similarity score
    """
    sql = """
        SELECT
            title,
            category,
            content,
            1 - (embedding <=> %s::vector) AS similarity
        FROM policy_documents
        WHERE 1 - (embedding <=> %s::vector) > %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (vec_str, vec_str, VECTOR_SIMILARITY_THRESHOLD, vec_str, top_k))
            return [dict(row) for row in cur.fetchall()]


def query_policy_keyword_search(
    query: str,
    top_k: int = VECTOR_TOP_K,
    category: Optional[str] = None,
) -> list[dict]:
    """
    Keyword / full-text search over policy documents.

    This complements pgvector retrieval for policy questions where exact terms
    matter, such as refund windows, cancellation fees, ticket types, luggage,
    pets, bicycles, or booking deadlines.

    Args:
        query:    Natural language policy question.
        top_k:    Number of keyword candidates to return.
        category: Optional metadata category inferred by rag.py.

    Returns:
        List of dicts with policy document metadata and a keyword score.
    """
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return []

    # Full-text search gives a real lexical relevance score.
    # ILIKE fallback catches exact phrases and short terms that full-text search
    # may ignore, e.g. "2 hours", "pet", "bike".
    sql = """
        WITH q AS (
            SELECT
                plainto_tsquery('english', %(query)s) AS tsq,
                %(like_query)s AS like_query,
                %(category)s AS category_filter
        )
        SELECT
            pd.id,
            pd.title,
            pd.category,
            pd.content,
            pd.source_file,
            pd.created_at,
            ts_rank_cd(
                to_tsvector(
                    'english',
                    coalesce(pd.title, '') || ' ' ||
                    coalesce(pd.category, '') || ' ' ||
                    coalesce(pd.content, '')
                ),
                q.tsq
            ) AS keyword_score
        FROM policy_documents pd
        CROSS JOIN q
        WHERE
            (
                to_tsvector(
                    'english',
                    coalesce(pd.title, '') || ' ' ||
                    coalesce(pd.category, '') || ' ' ||
                    coalesce(pd.content, '')
                ) @@ q.tsq
                OR lower(pd.title)    LIKE q.like_query
                OR lower(pd.category) LIKE q.like_query
                OR lower(pd.content)  LIKE q.like_query
            )
            AND (
                q.category_filter IS NULL
                OR lower(pd.category) LIKE '%%' || lower(q.category_filter) || '%%'
            )
        ORDER BY
            keyword_score DESC,
            pd.title ASC
        LIMIT %(top_k)s
    """

    params = {
        "query": cleaned_query,
        "like_query": f"%{cleaned_query.lower()}%",
        "category": category,
        "top_k": top_k,
    }

    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def store_policy_document(
    title: str,
    category: str,
    content: str,
    embedding: list[float],
    source_file: str = "",
) -> int:
    """
    Insert a policy document with its embedding into the database.
    Used by skeleton/seed_vectors.py — students don't need to call this directly.

    Returns:
        The new document's id
    """
    sql = """
        INSERT INTO policy_documents (title, category, content, embedding, source_file)
        VALUES (%s, %s, %s, %s::vector, %s)
        RETURNING id
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (title, category, content, vec_str, source_file))
            return cur.fetchone()[0]
