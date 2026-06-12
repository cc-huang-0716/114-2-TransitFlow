# TASK 6 EXTENSION: Wires four bonus tools into the agent —
#   get_platform, get_service_delays, get_loyalty_points, get_stations_by_zone —
#   plus regex parameter-correction/fallbacks for the local router. See TASK6.md.

from __future__ import annotations
import json
import re
from datetime import date
from skeleton.llm_provider import llm
from databases.relational.queries import (
    query_national_rail_availability,
    query_national_rail_fare,
    query_metro_schedules,
    query_metro_fare,
    query_available_seats,
    auto_select_adjacent_seats,
    query_user_profile,
    query_user_bookings,
    execute_booking,
    execute_cancellation,
    query_platform_assignment,
    query_service_delays,
    query_loyalty_balance,
)
from skeleton.rag import hybrid_policy_search
from databases.graph.queries import (
    query_shortest_route,
    query_cheapest_route,
    query_alternative_routes,
    query_interchange_path,
    query_delay_ripple,
    query_stations_by_zone,
)

_STATION_INDEX = {
    "central square": "MS01", "riverside":   "MS02", "northgate":  "MS03",
    "elm park":       "MS04", "westfield":   "MS05", "harbour view": "MS06",
    "old town":       "MS07", "university":  "MS08", "queensbridge": "MS09",
    "parkside":       "MS10", "greenhill":   "MS11", "lakeshore":  "MS12",
    "clifton":        "MS13", "eastwick":    "MS14", "ferndale":   "MS15",
    "hilltop":        "MS16", "broadmoor":   "MS17", "sunnyvale":  "MS18",
    "redwood":        "MS19", "thornton":    "MS20",
    "central station":   "NR01", "maplewood":     "NR02",
    "old town junction": "NR03", "ashford":        "NR04",
    "stonehaven":        "NR05", "bridgeport":     "NR06",
    "ferndale halt":     "NR07", "coalport":       "NR08",
    "dunmore":           "NR09", "langford end":   "NR10",
}


def _inject_station_ids(text):
    """
    Replace station names in text with 'name (ID)' so the LLM reads the ID
    right next to the name and uses it as the parameter value.
    Longer names are substituted first so 'Old Town Junction' beats 'Old Town'.
    Returns the original text unchanged when no stations are found.
    """
    result = text
    seen_ids = set()
    for name in sorted(_STATION_INDEX, key=len, reverse=True):
        sid = _STATION_INDEX[name]
        if sid in seen_ids:
            continue
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        if pattern.search(result):
            result = pattern.sub(f"{name} ({sid})", result)
            seen_ids.add(sid)
    return result

SYSTEM_PROMPT = """You are TransitFlow, a transit assistant for a dual-network system.

Networks: City Metro MS01-MS20 (lines M1-M4) | National Rail NR01-NR10 (lines NR1-NR2)
Interchanges: Central=MS01/NR01 | Old Town=MS07/NR03 | Ferndale=MS15/NR07
Today: {today}

LOGIN RULE: Routes, fares, schedules, and policies work WITHOUT login for all users. Only make_booking and cancel_booking need login — if the user tries to book or cancel and is not logged in, tell them to log in first.

When DATA FROM TRANSITFLOW DATABASE is provided, use it as the only source of truth.
Do not invent, infer, recalculate, or add stations, lines, fares, travel times, transfers, return trips, schedules, seats, or policies that are not present in the database result.
Do not contradict the database result or say a route was not found if the data shows one.

For route results:
- List the station names in the exact order shown in the database result.
- Use the exact total_time_min or total_fare_usd shown in the database result.
- If legs are shown, summarize the legs in order.
- Do not recalculate a different total.
- Do not add a return trip.
- If found is false or the result list is empty, say no valid route was found.

Always reply in the same language as the user.
""".format(today=date.today().isoformat())

TOOLS = [
    {
        "name": "check_national_rail_availability",
        "description": (
            "Check available national rail trains and services between two stations. "
            "Use for any question about what trains run, schedules, timetables, or availability. "
            "Returns schedules, service types, fare classes, and seat occupancy."
        ),
        "parameters": {
            "origin_id":      {"type": "string", "description": "National rail station ID e.g. NR01"},
            "destination_id": {"type": "string", "description": "National rail station ID e.g. NR05"},
            "travel_date":    {"type": "string", "description": "YYYY-MM-DD (optional — omit for general info)"},
        },
        "required": ["origin_id", "destination_id"],
    },
    {
        "name": "get_national_rail_fare",
        "description": "Calculate the fare for a national rail journey on a specific schedule.",
        "parameters": {
            "schedule_id":     {"type": "string", "description": "e.g. NR_SCH01"},
            "fare_class":      {"type": "string", "description": "standard or first"},
            "stops_travelled": {"type": "integer", "description": "Number of stops between origin and destination (from availability result)"},
        },
        "required": ["schedule_id", "fare_class", "stops_travelled"],
    },
    {
        "name": "check_metro_availability",
        "description": "Check available metro services between two metro stations.",
        "parameters": {
            "origin_id":      {"type": "string", "description": "Metro station ID e.g. MS01"},
            "destination_id": {"type": "string", "description": "Metro station ID e.g. MS09"},
        },
        "required": ["origin_id", "destination_id"],
    },
    {
        "name": "calculate_metro_fare",
        "description": "Calculate the metro single-ticket fare for a journey.",
        "parameters": {
            "schedule_id":     {"type": "string", "description": "e.g. MS_SCH01"},
            "stops_travelled": {"type": "integer", "description": "Number of stops between origin and destination"},
        },
        "required": ["schedule_id", "stops_travelled"],
    },
    {
        "name": "get_metro_fare",
        "description": (
            "Get the metro ticket PRICE between two stations. "
            "Use ONLY for fare/price/cost questions ('how much does it cost', 'what is the fare'). "
            "Do NOT use this for route or direction questions — use find_route instead."
        ),
        "parameters": {
            "origin_id":      {"type": "string", "description": "Metro station ID e.g. MS01"},
            "destination_id": {"type": "string", "description": "Metro station ID e.g. MS09"},
        },
        "required": ["origin_id", "destination_id"],
    },
    {
        "name": "get_user_bookings",
        "description": (
            "Retrieve the logged-in user's full booking history (national rail bookings + metro trips). "
            "Use whenever the user asks about their tickets, journeys, or travel history. "
            "Requires login — no parameters needed."
        ),
        "parameters": {},
        "required": [],
    },
    {
        "name": "get_available_seats",
        "description": (
            "Show available seats on a national rail service for a given date and fare class. "
            "Always call this before making a first-class booking, or when the user wants to select a seat."
        ),
        "parameters": {
            "schedule_id":  {"type": "string", "description": "e.g. NR_SCH01"},
            "travel_date":  {"type": "string", "description": "YYYY-MM-DD"},
            "fare_class":   {"type": "string", "description": "standard or first"},
        },
        "required": ["schedule_id", "travel_date", "fare_class"],
    },
    {
        "name": "make_booking",
        "description": (
            "Create a national rail booking for the logged-in user. "
            "REQUIRES LOGIN. Only call after the user has explicitly confirmed all booking details. "
            "Do NOT call this speculatively."
        ),
        "parameters": {
            "schedule_id":            {"type": "string", "description": "e.g. NR_SCH01"},
            "origin_station_id":      {"type": "string", "description": "e.g. NR01"},
            "destination_station_id": {"type": "string", "description": "e.g. NR05"},
            "travel_date":            {"type": "string", "description": "YYYY-MM-DD"},
            "fare_class":             {"type": "string", "description": "standard or first"},
            "seat_id":                {"type": "string", "description": "Specific seat ID (e.g. B05) or 'any' for auto-assign"},
            "ticket_type":            {"type": "string", "description": "single or return (default single)"},
        },
        "required": ["schedule_id", "origin_station_id", "destination_station_id", "travel_date", "fare_class", "seat_id"],
    },
    {
        "name": "cancel_booking",
        "description": (
            "Cancel a national rail booking for the logged-in user. "
            "REQUIRES LOGIN. Only call after the user has explicitly confirmed the cancellation. "
            "The refund amount is calculated automatically per the applicable policy."
        ),
        "parameters": {
            "booking_id": {"type": "string", "description": "Booking reference e.g. BK-A1B2C3"},
        },
        "required": ["booking_id"],
    },
    {
        "name": "search_policy",
        "description": (
            "Search company policy documents. Use for any question about: "
            "refunds, delay compensation, luggage, bicycles, pets, food and drink, "
            "conduct, booking rules, ticket types, fare evasion, or child fares."
        ),
        "parameters": {
            "query": {"type": "string", "description": "Natural language question about policy"},
        },
        "required": ["query"],
    },
    {
    "name": "find_route",
    "description": (
        "Find a route between two stations in the transit graph. "
        "Use this tool for directions, how to get from A to B, route planning, "
        "fastest route, quickest route, shortest travel-time route, or cheapest route. "
        "Set optimise_by='time' when the user asks for fastest, quickest, shortest-time, "
        "or general route directions without mentioning price. "
        "Set optimise_by='cost' ONLY when the user explicitly asks for cheapest, lowest fare, "
        "lowest cost, least expensive, price, or fare. "
        "If the user does not mention cost, fare, price, or cheapest, default to optimise_by='time'."
    ),
        "parameters": {
            "origin_id":      {"type": "string", "description": "Station ID e.g. MS01 or NR01"},
            "destination_id": {"type": "string", "description": "Station ID e.g. MS09 or NR05"},
            "network":        {"type": "string", "description": "metro, rail, or auto (default auto — inferred from IDs)"},
            "optimise_by": {
                            "type": "string",
                            "description": (
                                "Route objective. Use 'time' for fastest, quickest, shortest travel-time, "
                                "or normal route/direction questions. Use 'cost' only for cheapest, lowest fare, "
                                "lowest cost, least expensive, price, or fare questions. Default is 'time'."
                            ),
                        },
        },
        "required": ["origin_id", "destination_id"],
    },
    {
    "name": "find_alternative_routes",
    "description": (
        "Find routes that avoid a specific delayed or closed station. "
        "Use network='auto' by default so the search can include both Metro, "
        "National Rail, and INTERCHANGE_TO transfer links. This is important "
        "for disruption detours where a route may need to switch from NR to MS "
        "or from MS to NR."),
        "parameters": {
            "origin_id":        {"type": "string", "description": "e.g. NR01"},
            "destination_id":   {"type": "string", "description": "e.g. NR05"},
            "avoid_station_id": {"type": "string", "description": "The station to avoid e.g. NR03"},
            "network":          {"type": "string", "description": "metro, rail, or auto"},
        },
        "required": ["origin_id", "destination_id", "avoid_station_id"],
    },
    {
        "name": "get_delay_ripple",
        "description": "Show which stations and lines are affected by a disruption or delay at a given station (within N hops).",
        "parameters": {
            "station_id": {"type": "string", "description": "Station ID e.g. NR03 or MS07"},
            "hops":       {"type": "integer", "description": "How many connections out to check (default 2)"},
        },
        "required": ["station_id"],
    },
    {
        "name": "get_platform",
        "description": (
            "Get the departure platform number for a national rail service at a specific station. "
            "Use when the user asks about platforms, departure boards, or which platform to go to."
        ),
        "parameters": {
            "schedule_id": {"type": "string", "description": "e.g. NR_SCH01"},
            "station_id":  {"type": "string", "description": "e.g. NR01"},
        },
        "required": ["schedule_id", "station_id"],
    },
    {
        "name": "get_service_delays",
        "description": (
            "Look up historical train delay records for national rail services. "
            "Use when the user asks whether a service was delayed, about past delays on a route, "
            "or delay history."
        ),
        "parameters": {
            "schedule_id": {"type": "string", "description": "e.g. NR_SCH01 (optional)"},
            "travel_date": {"type": "string", "description": "YYYY-MM-DD (optional)"},
        },
        "required": [],
    },
    {
        "name": "get_loyalty_points",
        "description": (
            "Show the logged-in user's loyalty points balance and recent earning history. "
            "REQUIRES LOGIN. No parameters needed."
        ),
        "parameters": {},
        "required": [],
    },
    {
        "name": "get_stations_by_zone",
        "description": (
            "List all metro stations in a specific fare zone (1, 2, or 3). "
            "Use when the user asks which stations are in zone 1, 2, or 3, "
            "or asks about zone-based fares."
        ),
        "parameters": {
            "zone": {"type": "integer", "description": "Zone number: 1, 2, or 3"},
        },
        "required": ["zone"],
    },
]

TOOLS_SCHEMA = """\
find_route(origin_id, destination_id, optimise_by?)
check_national_rail_availability(origin_id, destination_id, travel_date?)
get_national_rail_fare(schedule_id, fare_class, stops_travelled)
check_metro_availability(origin_id, destination_id)
calculate_metro_fare(schedule_id, stops_travelled)
get_available_seats(schedule_id, travel_date, fare_class)
make_booking(schedule_id, origin_station_id, destination_station_id, travel_date, fare_class, seat_id, ticket_type?)
cancel_booking(booking_id)
get_user_bookings()
search_policy(query)
find_alternative_routes(origin_id, destination_id, avoid_station_id, network?)
get_delay_ripple(station_id, hops?)
get_platform(schedule_id, station_id)
get_service_delays(schedule_id?, travel_date?)
get_loyalty_points()
get_stations_by_zone(zone)"""

def _execute_tool(
    tool_name,
    params,
    current_user_email = None,
):
    try:
        if tool_name == "check_national_rail_availability":
            result = query_national_rail_availability(**params)

        elif tool_name == "get_national_rail_fare":
            result = query_national_rail_fare(**params)

        elif tool_name == "check_metro_availability":
            result = query_metro_schedules(
                origin_id=params["origin_id"],
                destination_id=params["destination_id"],
            )

        elif tool_name == "calculate_metro_fare":
            result = query_metro_fare(**params)

        elif tool_name == "get_metro_fare":
            schedules = query_metro_schedules(
                origin_id=params["origin_id"],
                destination_id=params["destination_id"],
            )
            if not schedules:
                result = {"error": "No metro service found between these stations."}
            else:
                sched = schedules[0]
                stops = sched.get("stops_in_order") or []
                if isinstance(stops, str):
                    import json as _json
                    stops = _json.loads(stops)
                try:
                    n_stops = stops.index(params["destination_id"]) - stops.index(params["origin_id"])
                except ValueError:
                    n_stops = 1
                fare = query_metro_fare(sched["schedule_id"], n_stops)
                result = {
                    "origin":       sched.get("origin_name", params["origin_id"]),
                    "destination":  sched.get("destination_name", params["destination_id"]),
                    "line":         sched.get("line"),
                    "schedule_id":  sched["schedule_id"],
                    "stops":        n_stops,
                    **(fare or {"error": "Fare lookup failed"}),
                }

        elif tool_name == "get_user_bookings":
            if not current_user_email:
                return json.dumps({"error": "No user is currently logged in."})
            result = query_user_bookings(current_user_email)

        elif tool_name == "get_available_seats":
            result = query_available_seats(**params)

        elif tool_name == "make_booking":
            if not current_user_email:
                return json.dumps({"error": "You must be logged in to make a booking."})
            profile = query_user_profile(current_user_email)
            if not profile:
                return json.dumps({"error": "User profile not found."})
            ok, data = execute_booking(
                user_id=profile["user_id"],
                schedule_id=params["schedule_id"],
                origin_station_id=params["origin_station_id"],
                destination_station_id=params["destination_station_id"],
                travel_date=params["travel_date"],
                fare_class=params["fare_class"],
                seat_id=params["seat_id"],
                ticket_type=params.get("ticket_type", "single"),
            )
            result = data if ok else {"error": data}

        elif tool_name == "cancel_booking":
            if not current_user_email:
                return json.dumps({"error": "You must be logged in to cancel a booking."})
            profile = query_user_profile(current_user_email)
            if not profile:
                return json.dumps({"error": "User profile not found."})
            ok, data = execute_cancellation(
                booking_id=params["booking_id"],
                user_id=profile["user_id"],
            )
            result = data if ok else {"error": data}

        elif tool_name == "search_policy":
            result = hybrid_policy_search(
                query=params["query"],
                llm=llm,
                top_k=3,
            )

        elif tool_name == "find_route":
            origin_id = params["origin_id"].upper()
            destination_id = params["destination_id"].upper()
            network = params.get("network", "auto")
            optimise_by = params.get("optimise_by", "time")
            is_cross = (
                (origin_id.startswith("MS") and destination_id.startswith("NR")) or
                (origin_id.startswith("NR") and destination_id.startswith("MS"))
            )

            if is_cross:
                network = "auto"

            if optimise_by == "cost":
                result = query_cheapest_route(
                    origin_id=origin_id,
                    destination_id=destination_id,
                    network=network,
                )
            elif is_cross:
                result = query_interchange_path(
                    origin_id=origin_id,
                    destination_id=destination_id,
                )
            else:
                result = query_shortest_route(
                    origin_id=origin_id,
                    destination_id=destination_id,
                    network=network,
                )

        elif tool_name == "find_alternative_routes":
            origin_id = params["origin_id"].upper()
            destination_id = params["destination_id"].upper()
            avoid_station_id = params["avoid_station_id"].upper()
            network = "auto"
            routes = query_alternative_routes(
                origin_id=origin_id,
                destination_id=destination_id,
                avoid_station_id=avoid_station_id,
                network=network,
            )
            result = [{"route_number": i + 1, "legs": r} for i, r in enumerate(routes)]

        elif tool_name == "get_delay_ripple":
            result = query_delay_ripple(
                delayed_station_id=params["station_id"],
                hops=params.get("hops", 2),
            )

        elif tool_name == "get_platform":
            result = query_platform_assignment(
                schedule_id=params["schedule_id"],
                station_id=params["station_id"],
            )

        elif tool_name == "get_service_delays":
            result = query_service_delays(
                schedule_id=params.get("schedule_id"),
                travel_date=params.get("travel_date"),
            )

        elif tool_name == "get_loyalty_points":
            if not current_user_email:
                return json.dumps({"error": "You must be logged in to view loyalty points."})
            result = query_loyalty_balance(current_user_email)

        elif tool_name == "get_stations_by_zone":
            result = query_stations_by_zone(zone=int(params["zone"]))

        else:
            result = {"error": f"Unknown tool: {tool_name}"}

        return json.dumps(result, default=str)

    except Exception as e:
        return json.dumps({"error": str(e)})


def _flatten_to_text(obj, depth = 0):
    pad = "  " * depth
    if isinstance(obj, dict):
        if not obj:
            return f"{pad}(empty)"
        lines = []
        for k, v in obj.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                inner = _flatten_to_text(v, depth + 1)
                if inner.strip():
                    lines.append(f"{pad}{k}:\n{inner}")
            else:
                lines.append(f"{pad}{k}: {v}")
        return "\n".join(lines) or f"{pad}(empty)"
    elif isinstance(obj, list):
        if not obj:
            return f"{pad}(no records)"
        parts = []
        for i, item in enumerate(obj, 1):
            if isinstance(item, (dict, list)):
                parts.append(f"{pad}[{i}]")
                parts.append(_flatten_to_text(item, depth + 1))
            else:
                parts.append(f"{pad}- {item}")
        return "\n".join(parts)
    else:
        return f"{pad}{obj}"


def _normalise_result(tool_name, result_json):
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return result_json
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return _flatten_to_text(data)

def _format_route_answer(tool_name, result_json, user_message):
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict) and data.get("error"):
        return f"查詢時發生錯誤：{data['error']}"

    if tool_name == "find_route" and isinstance(data, dict):
        if data.get("found") is False:
            return "找不到符合條件的有效路線。"

        stations = data.get("path") or data.get("stations") or []
        station_text = " → ".join(
            f"{s.get('station_id', '')} {s.get('name', '')}".strip()
            for s in stations
            if isinstance(s, dict)
        )

        lines = []
        if station_text:
            lines.append(f"路線：{station_text}")

        if data.get("total_time_min") is not None:
            lines.append(f"總旅行時間：{data['total_time_min']} 分鐘")

        if data.get("total_fare_usd") is not None:
            lines.append(f"預估總費用：{data['total_fare_usd']} 美元")

    
    if not re.search(r"[\u4e00-\u9fff]", user_message):
        return None
    if isinstance(data, dict) and data.get("error"):
        return f"查詢時發生錯誤：{data['error']}"

    if tool_name == "find_route" and isinstance(data, dict):
        if data.get("found") is False:
            return "找不到符合條件的有效路線。"

        stations = data.get("path") or data.get("stations") or []
        station_text = " → ".join(
            f"{s.get('station_id', '')} {s.get('name', '')}".strip()
            for s in stations
            if isinstance(s, dict)
        )

        lines = []
        if station_text:
            lines.append(f"路線：{station_text}")

        if data.get("total_time_min") is not None:
            lines.append(f"總旅行時間：{data['total_time_min']} 分鐘")

        if data.get("total_fare_usd") is not None:
            lines.append(f"預估總費用：{data['total_fare_usd']} 美元")

        legs = data.get("legs") or []
        if legs:
            lines.append("分段資訊：")
            for leg in legs:
                from_name = leg.get("from_name", leg.get("from_id", ""))
                to_name = leg.get("to_name", leg.get("to_id", ""))
                line = leg.get("line")
                rel = leg.get("relationship_type", "")
                time_min = leg.get("travel_time_min")
                fare = leg.get("fare_usd")

                detail = f"- {from_name} → {to_name}"
                if line:
                    detail += f"（{line}）"
                elif rel == "INTERCHANGE_TO":
                    detail += "（轉乘步行）"

                if time_min is not None:
                    detail += f"，{time_min} 分鐘"
                if fare is not None:
                    detail += f"，{fare} 美元"

                lines.append(detail)

        return "\n".join(lines) if lines else None

    if tool_name == "find_alternative_routes":
        if not data:
            return "避開指定站點後，找不到可行的替代路線。"

        lines = []
        for route in data:
            route_no = route.get("route_number", "?")
            legs = route.get("legs", [])
            lines.append(f"替代路線 {route_no}：")
            if not legs:
                lines.append("- 無路段資料")
                continue

            station_chain = []
            total = 0
            for i, leg in enumerate(legs):
                if i == 0:
                    station_chain.append(f"{leg.get('from_id', '')} {leg.get('from_name', '')}".strip())
                station_chain.append(f"{leg.get('to_id', '')} {leg.get('to_name', '')}".strip())
                total += leg.get("travel_time_min") or 0

            lines.append(" → ".join(station_chain))
            lines.append(f"總旅行時間：約 {total} 分鐘")

        return "\n".join(lines)

    if tool_name == "get_delay_ripple":
        if not data:
            return "沒有找到受影響的附近站點。"

        lines = ["可能受影響的站點："]
        for item in data:
            station = f"{item.get('station_id', '')} {item.get('name', '')}".strip()
            hops = item.get("hops_away")
            affected = item.get("lines_affected") or []
            affected_text = ", ".join(affected) if affected else "未標示"
            lines.append(f"- {station}：距離 {hops} hop，涉及 {affected_text}")
        return "\n".join(lines)

    return None


def _summarise_result(tool_name, result_json):
    return result_json


def _parse_tool_calls(llm_response):
    import re
    text = llm_response.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    decoder = json.JSONDecoder()
    for m in re.finditer(r'\{', text):
        try:
            data, _ = decoder.raw_decode(text, m.start())
            if "tool_calls" in data:
                return data["tool_calls"]
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return None

def _extract_alternative_route_ids(message: str, station_ids: list[str]):
    if len(station_ids) < 3:
        return None, None, None

    def _first_id_after(patterns):
        for pattern in patterns:
            m = re.search(pattern, message, re.IGNORECASE)
            if m:
                return m.group(1).upper()
        return None

    avoid_id = _first_id_after([
        r"\bavoid(?:ing)?\b[^\n,.?;:]*?\b(MS\d{2}|NR\d{2})\b",
        r"\b(?:closed|closure|blocked|disrupted|delayed)\b[^\n,.?;:]*?\b(MS\d{2}|NR\d{2})\b",
        r"\b(?:disruption|delay)\s+(?:at|on|near)?\s*[^\n,.?;:]*?\b(MS\d{2}|NR\d{2})\b",
        r"\bwithout\b[^\n,.?;:]*?\b(MS\d{2}|NR\d{2})\b",
        r"\bnot\s+pass\s+through\b[^\n,.?;:]*?\b(MS\d{2}|NR\d{2})\b",
    ])

    origin_id = destination_id = None

    from_to = re.search(
        r"\bfrom\b[^\n,.?;:]*?\b(MS\d{2}|NR\d{2})\b.*?\bto\b[^\n,.?;:]*?\b(MS\d{2}|NR\d{2})\b",
        message,
        re.IGNORECASE,
    )
    if from_to:
        origin_id = from_to.group(1).upper()
        destination_id = from_to.group(2).upper()

    if not avoid_id and origin_id and destination_id:
        for sid in station_ids:
            if sid not in {origin_id, destination_id}:
                avoid_id = sid
                break

    if avoid_id and (not origin_id or not destination_id):
        remaining = [sid for sid in station_ids if sid != avoid_id]
        if len(remaining) >= 2:
            origin_id = remaining[0]
            destination_id = remaining[1]

    if not avoid_id and len(station_ids) >= 3:
        avoid_id = station_ids[0]
        origin_id = station_ids[1]
        destination_id = station_ids[2]

    return origin_id, destination_id, avoid_id

def run_agent(
    user_message,
    history,
    debug = False,
    current_user_email = None,
):
    debug_info = []
    if current_user_email:
        profile = query_user_profile(current_user_email)
        if profile:
            user_display = f"{profile['full_name']} (email: {current_user_email}, user_id: {profile['user_id']})"
        else:
            user_display = current_user_email
        contextual_prompt = SYSTEM_PROMPT + (
            f"\n\nLogged-in user: {user_display}. "
            "Answer personal booking queries for this user without asking for their email or ID. "
            "Use get_user_bookings() for any booking history request. "
            "Use make_booking / cancel_booking for booking and cancellation requests."
        )
    else:
        contextual_prompt = SYSTEM_PROMPT + (
            "\n\nNo user is currently logged in. "
            "If the user asks about personal bookings, history, or wants to make/cancel a booking, "
            "tell them they must log in first."
        )
    recent_history = history[-4:] if len(history) > 4 else history
    _augmented_message = _inject_station_ids(user_message)

    tool_selection_prompt = f"""Output only this JSON (no other text):
{{"tool_calls": [{{"name": "TOOL", "params": {{"KEY": "VALUE"}}}}]}}
Or if no tool needed: {{"tool_calls": []}}

STATIONS: Metro=MS01-MS20, Rail=NR01-NR10
USER: {current_user_email or "not logged in"}
get_user_bookings: call (no params) when logged-in user asks about their bookings, tickets, or travel history.
make_booking/cancel_booking: only if user is logged in.
Alternative/avoid/closed/disrupted route questions: use find_alternative_routes.
Route/path/journey questions: use find_route. Policy questions: use search_policy.
Never use "" as a param value. Omit optional params if unknown.

TOOLS:
{TOOLS_SCHEMA}

HISTORY:
{json.dumps(recent_history, indent=None)}

USER: "{_augmented_message}"

Examples:
"fastest route MS01 to MS14" -> {{"tool_calls": [{{"name": "find_route", "params": {{"origin_id": "MS01", "destination_id": "MS14", "optimise_by": "time"}}}}]}}
"cheapest NR01 to NR05" -> {{"tool_calls": [{{"name": "find_route", "params": {{"origin_id": "NR01", "destination_id": "NR05", "optimise_by": "cost"}}}}]}}
"trains NR01 to NR03 on 2025-06-01" -> {{"tool_calls": [{{"name": "check_national_rail_availability", "params": {{"origin_id": "NR01", "destination_id": "NR03", "travel_date": "2025-06-01"}}}}]}}
"refund policy" -> {{"tool_calls": [{{"name": "search_policy", "params": {{"query": "refund policy"}}}}]}}
"hello" -> {{"tool_calls": []}}
"show my bookings" -> {{"tool_calls": [{{"name": "get_user_bookings", "params": {{}}}}]}}
"book me a seat NR01 to NR05 on 2025-06-01" -> {{"tool_calls": [{{"name": "check_national_rail_availability", "params": {{"origin_id": "NR01", "destination_id": "NR05", "travel_date": "2025-06-01"}}}}]}}

JSON:"""

    if llm.get_chat_provider() == "ollama":
        tool_calls = llm.ollama_tool_call(
            recent_history, TOOLS, _augmented_message,
            system_prompt=(
                "You are a tool router. Call the right tool based on the user message. "
                f"Logged-in user: {current_user_email or 'none'}. "
                "My bookings/tickets/travel history → get_user_bookings (no params). "
                "Book a ticket / make a booking → check_national_rail_availability first, then make_booking. "
                "Cancel a booking → cancel_booking. "
                "Policy/rules/conduct/compensation/luggage/bicycle questions → search_policy. "
                "Route/directions/how-to-get/path questions → find_route ONLY (never get_metro_fare). "
                "Fastest/quickest/shortest-time route questions → find_route with optimise_by='time'. "
                "Cheapest/lowest-fare/lowest-cost/price route questions → find_route with optimise_by='cost'. "
                "If no cost/fare/price words appear, use optimise_by='time'. "
                "Metro fare/price/cost/how-much-does-it-cost questions → get_metro_fare. "
                "Rail fare/cost/price questions → check_national_rail_availability then get_national_rail_fare. "
                "Schedule/timetable/trains/services questions → check_national_rail_availability or check_metro_availability. "
                "Only call a tool when needed. Output nothing except tool calls."
                "Alternative/avoid/closed/disrupted route questions → find_alternative_routes. "
                "Platform / which platform / departure board questions → get_platform. "
                "Delay history / past delays / was there a delay questions → get_service_delays. "
                "Loyalty points / my points / points balance questions → get_loyalty_points (login required). "
                "Zone stations / stations in zone / zone 1 or 2 or 3 questions → get_stations_by_zone. "
            ),
        )
        if debug:
            debug_info.append(f"**Tool selection (native):** {tool_calls}")
    else:
        selection_response = llm.chat(
            messages=[{"role": "user", "content": tool_selection_prompt}],
            system_prompt="JSON only. You are a router. Output valid JSON. No empty string param values.",
        )
        tool_calls = _parse_tool_calls(selection_response) or []
        if debug:
            debug_info.append(f"**Tool selection:** {selection_response}")
    _user_station_ids_raw = re.findall(
        r'\b(MS\d{2}|NR\d{2})\b',
        user_message,
        re.IGNORECASE,
    )

    tool_calls = tool_calls or []

    _user_station_ids = []
    for sid in _user_station_ids_raw:
        sid = sid.upper()
        if sid not in _user_station_ids:
            _user_station_ids.append(sid)

    _lower = _augmented_message.lower()
    _station_ids_raw = re.findall(
        r'\b(MS\d{2}|NR\d{2})\b',
        _augmented_message,
        re.IGNORECASE,
    )

    _station_ids = []
    for sid in _station_ids_raw:
        sid = sid.upper()
        if sid not in _station_ids:
            _station_ids.append(sid)

    _two_stations = len(_station_ids) >= 2


    def _fallback(name: str, params: dict, reason: str):
        nonlocal tool_calls
        tool_calls = [{"name": name, "params": params}]
        if debug:
            debug_info.append(f"**Fallback:** {reason} → {name}({params})")

    _route_triggers = {
        "fastest route", "quickest route", "shortest route", "cheapest route",
        "best route", "how to get", "directions from", "route from", "route to",
        "get from", "travel from", "way from", "path from"
    }

    _cost_triggers = {
        "cheap", "cheapest", "lowest cost", "lowest fare",
        "least expensive", "fare", "price", "cost"
    }

    _time_triggers = {
        "fastest", "quickest", "shortest time", "shortest travel time",
        "least time", "as fast as possible"
    }

    _alternative_triggers = {
        "alternative route", "alternative routes", "avoid", "avoiding",
        "closed", "closure", "disruption", "disrupted", "delay", "delayed",
        "route around", "bypass", "blocked", "not pass through"
    }

    _is_route = (
        any(kw in _lower for kw in _route_triggers) or
        (_two_stations and "route" in _lower)
    )
    _is_alternative = any(kw in _lower for kw in _alternative_triggers)

    _delay_impact_triggers = {
    "affected", "affected stations", "impact", "ripple", "ripple effect",
    "within", "within 2 hops", "within two hops", "hops",
    "may be affected", "which stations"
    }

    _is_delay_impact = (
        any(kw in _lower for kw in ["delay", "delayed", "disruption", "disrupted"])
        and any(kw in _lower for kw in _delay_impact_triggers)
        and len(_station_ids) >= 1
    )

    def _clean_station_id(value):
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
        return None


    ids_for_alt = _user_station_ids if len(_user_station_ids) >= 3 else _station_ids
    parsed_origin_id, parsed_destination_id, parsed_avoid_station_id = _extract_alternative_route_ids(
        _augmented_message,
        ids_for_alt,
    )

    cleaned_tool_calls = []

    for call in tool_calls:
        if call.get("name") != "find_alternative_routes":
            cleaned_tool_calls.append(call)
            continue

        params = call.setdefault("params", {})

        origin_id = _clean_station_id(params.get("origin_id")) or parsed_origin_id
        destination_id = _clean_station_id(params.get("destination_id")) or parsed_destination_id
        avoid_station_id = _clean_station_id(params.get("avoid_station_id")) or parsed_avoid_station_id

        if origin_id and destination_id and avoid_station_id:
            params["origin_id"] = origin_id
            params["destination_id"] = destination_id
            params["avoid_station_id"] = avoid_station_id
            params["network"] = "auto"
            cleaned_tool_calls.append(call)
        else:
            if debug:
                debug_info.append(
                    "**Correction:** skipped incomplete find_alternative_routes params "
                    f"and will try fallback parsing. params={params}"
                )

    tool_calls = cleaned_tool_calls

    # Correct parameters for the bonus tools. The small router model often fills
    # schedule_id with a station-style ID (e.g. "NR05") or mis-reads the zone,
    # so we re-extract the real values from the user message via regex.
    _schedule_ids = []
    for raw in re.findall(r'\bNR[_ ]?SCH\d+\b', _augmented_message, re.IGNORECASE):
        digits = re.search(r'\d+', raw).group(0)
        sid = f"NR_SCH{digits}"
        if sid not in _schedule_ids:
            _schedule_ids.append(sid)

    _nr_station_ids = [s for s in _station_ids if s.startswith("NR")]
    _zone_match = re.search(r'\bzone\s*([123])\b', _lower)

    def _valid_schedule_id(v):
        return isinstance(v, str) and re.fullmatch(r'NR_SCH\d+', v.strip().upper() or "")

    for call in tool_calls:
        name = call.get("name")
        params = call.setdefault("params", {})

        if name == "get_platform":
            if not _valid_schedule_id(params.get("schedule_id")) and _schedule_ids:
                params["schedule_id"] = _schedule_ids[0]
            elif isinstance(params.get("schedule_id"), str):
                params["schedule_id"] = params["schedule_id"].strip().upper()
            sid = _clean_station_id(params.get("station_id"))
            if (not sid or not sid.startswith("NR")) and _nr_station_ids:
                sid = _nr_station_ids[0]
            if sid:
                params["station_id"] = sid

        elif name == "get_service_delays":
            if _valid_schedule_id(params.get("schedule_id")):
                params["schedule_id"] = params["schedule_id"].strip().upper()
            elif _schedule_ids:
                params["schedule_id"] = _schedule_ids[0]
            else:
                params.pop("schedule_id", None)
            td = params.get("travel_date")
            if not (isinstance(td, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', td.strip() or "")):
                params.pop("travel_date", None)

        elif name == "get_stations_by_zone":
            if _zone_match:
                params["zone"] = int(_zone_match.group(1))
            else:
                try:
                    params["zone"] = int(params.get("zone"))
                except (TypeError, ValueError):
                    params.pop("zone", None)

        elif name == "search_policy":
            # The router sometimes echoes the parameter schema instead of a query.
            q = params.get("query")
            params.clear()
            params["query"] = q.strip() if isinstance(q, str) and q.strip() else user_message

    has_alternative_tool = any(
        call.get("name") == "find_alternative_routes"
        for call in tool_calls
    )

    has_service_delay_tool = any(
        call.get("name") == "get_service_delays"
        for call in tool_calls
    )

    # Historical delay-log query (distinct from the graph ripple-impact query):
    # "were there delays on NR_SCH01", "delay history", "any late-running trains".
    _is_service_delay = (
        not _is_delay_impact
        and not (_is_alternative and (len(_station_ids) >= 3 or len(_user_station_ids) >= 3))
        and any(kw in _lower for kw in ["delay", "delayed", "delays", "late-running", "late running"])
        and (
            len(_schedule_ids) >= 1
            or any(w in _lower for w in ["recent", "history", "past", "last month", "previous", "ever been"])
        )
    )

    if _is_delay_impact:
        station_id = _station_ids[0].upper()

        hop_match = re.search(r'\b(\d+)\s*hops?\b', _lower)
        hops = int(hop_match.group(1)) if hop_match else 2

        _fallback(
            "get_delay_ripple",
            {
                "station_id": station_id,
                "hops": hops,
            },
            "delay ripple impact query",
        )
    elif _is_service_delay and not has_service_delay_tool:
        params = {}
        if _schedule_ids:
            params["schedule_id"] = _schedule_ids[0]
        _td = re.search(r'\d{4}-\d{2}-\d{2}', _lower)
        if _td:
            params["travel_date"] = _td.group(0)
        _fallback("get_service_delays", params, "service delay history query")
    elif (
        _is_alternative
        and not has_alternative_tool
        and (len(_user_station_ids) >= 3 or len(_station_ids) >= 3)
    ):
        ids_for_alt = _user_station_ids if len(_user_station_ids) >= 3 else _station_ids

        origin_id, destination_id, avoid_station_id = _extract_alternative_route_ids(
            _augmented_message,
            ids_for_alt,
        )

        if origin_id and destination_id and avoid_station_id:
            _fallback(
                "find_alternative_routes",
                {
                    "origin_id": origin_id,
                    "destination_id": destination_id,
                    "avoid_station_id": avoid_station_id,
                    "network": "auto",
                },
                "alternative/disruption route query",
            )

    elif _is_route and _two_stations and not _is_alternative and not has_alternative_tool:
        _opt = "cost" if any(kw in _lower for kw in _cost_triggers) else "time"
        existing = next((c for c in tool_calls if c.get("name") == "find_route"), None)

        if existing:
            params = existing.setdefault("params", {})
            params["origin_id"] = params.get("origin_id") or _station_ids[0].upper()
            params["destination_id"] = params.get("destination_id") or _station_ids[1].upper()

            _o = params.get("origin_id", "").upper()
            _d = params.get("destination_id", "").upper()
            if (
                (_o.startswith("MS") and _d.startswith("NR")) or
                (_o.startswith("NR") and _d.startswith("MS"))
            ):
                params["network"] = "auto"

            if any(kw in _lower for kw in _time_triggers):
                params["optimise_by"] = "time"
                if debug:
                    debug_info.append("**Correction:** fastest/quickest route query → optimise_by='time'")
            elif any(kw in _lower for kw in _cost_triggers):
                params["optimise_by"] = "cost"
                if debug:
                    debug_info.append("**Correction:** cheapest/fare/cost route query → optimise_by='cost'")
            else:
                params["optimise_by"] = params.get("optimise_by", _opt)

        else:
            _fallback(
                "find_route",
                {
                    "origin_id": _station_ids[0].upper(),
                    "destination_id": _station_ids[1].upper(),
                    "optimise_by": _opt,
                },
                "route query",
            )

    elif not tool_calls and _two_stations:
        _avail_triggers = {"train", "trains", "service", "services", "run from", "runs from",
                           "schedule", "timetable", "available", "availability"}
        if any(kw in _lower for kw in _avail_triggers):
            o, d = _station_ids[0].upper(), _station_ids[1].upper()
            _travel_date = next(
                (w for w in _lower.split() if re.match(r'\d{4}-\d{2}-\d{2}', w)), None
            )
            _params = {"origin_id": o, "destination_id": d}
            if _travel_date:
                _params["travel_date"] = _travel_date
            _tool = "check_national_rail_availability" if o.startswith("NR") else "check_metro_availability"
            _fallback(_tool, _params, "availability query")

    if current_user_email and not tool_calls:
        _personal_triggers = {"my booking", "my ticket", "my trip", "my journey", "my history",
                               "my reservation", "show booking", "view booking", "check booking",
                               "list booking", "show my", "view my"}
        if any(kw in _lower for kw in _personal_triggers):
            _fallback("get_user_bookings", {}, "personal booking query")

    tool_results = []
    for call in tool_calls:
        tool_name = call.get("name", "")
        params    = call.get("params") or call.get("parameters", {})

        if any(v == "" for v in params.values()):
            if debug:
                debug_info.append(f"**Skipped** `{tool_name}` — empty params: {params}")
            continue

        if debug:
            debug_info.append(f"**Calling:** `{tool_name}({params})`")

        result_json = _execute_tool(tool_name, params, current_user_email)

        summary = _summarise_result(tool_name, result_json)

        if debug:
            debug_info.append(
                f"**Result (raw):** ```json\n{result_json[:300]}\n```\n"
                f"**Summary sent to LLM:** {summary}"
            )

        tool_results.append({
            "tool":    tool_name,
            "params":  params,
            "result":  result_json,
            "summary": summary,
        })
    
    for tr in tool_results:
        direct_answer = _format_route_answer(tr["tool"], tr["result"], user_message)
        if direct_answer:
            updated_history = history + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": direct_answer},
            ]
            if debug:
                debug_info.append("**Direct formatter:** route answer generated without final LLM call")
                return direct_answer, updated_history, "\n\n".join(debug_info)
            return direct_answer, updated_history


    _DB_KEYWORDS = {"booking", "ticket", "schedule", "fare", "route", "seat",
                    "train", "metro", "journey", "trip", "history", "reservation"}
    if tool_results:
        data_block = "\n\n".join(
            f"[{tr['tool']}]\n{_normalise_result(tr['tool'], tr['result'])}"
            for tr in tool_results
        )
        if debug:
            debug_info.append(f"**Data (normalised):**\n{data_block}")
        content = (
            f"DATA FROM TRANSITFLOW DATABASE:\n{data_block}"
            f"\n\nUser asks: {user_message}"
            f"\n\nAnswer using only the data above:"
        )
    elif any(kw in user_message.lower() for kw in _DB_KEYWORDS):
        content = (
            f"User asks: {user_message}\n\n"
            "IMPORTANT: No data was retrieved from the TransitFlow database for this query. "
            "Do NOT invent any bookings, fares, schedules, seat numbers, or travel times. "
            "Tell the user no data was found."
        )
    else:
        content = user_message

    final_messages = history + [{"role": "user", "content": content}]

    answer = llm.chat(messages=final_messages, system_prompt=contextual_prompt)

    updated_history = history + [
        {"role": "user",      "content": user_message},
        {"role": "assistant", "content": answer},
    ]

    if debug:
        return answer, updated_history, "\n\n".join(debug_info)
    return answer, updated_history