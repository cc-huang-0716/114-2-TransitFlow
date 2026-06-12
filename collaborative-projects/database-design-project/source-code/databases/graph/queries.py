"""
TransitFlow — Neo4j Graph Database Query Layer
=============================================

This module provides Neo4j graph query functions that can be called by the TransitFlow AI assistant.
These functions mainly handle network topology problems, such as route search, transfers,
alternative routes, delay impact areas, and directly adjacent stations.

Tool selection principles:
- query_shortest_route:
    Use this when the user asks for the fastest route, shortest travel time, quickest route,
    fastest route, or generally asks “how to get from A to B” without specifying fare,
    avoiding stations, or delay impact.

- query_cheapest_route:
    Use this when the user asks for the cheapest route, lowest fare, lowest fare,
    cheapest way, or when cost minimization is the main objective.

- query_alternative_routes:
    Use this when the user asks for alternative routes, avoiding a station, station closure,
    station delay, disruption, closure, avoid station, or route around a problem.

- query_interchange_path:
    Use this when the user explicitly asks about transfers between metro and national rail,
    interchange, transfer, or needs an explanation of where to switch from one transport
    network to another.

- query_delay_ripple:
    Use this when the user asks which nearby stations would be affected by a delay or
    disruption at a certain station, the scope of impact, ripple effect, affected stations,
    or stations within N hops.
    This is not a route planning tool, but a network impact analysis tool.

- query_station_connections:
    Use this when the user asks which directly adjacent stations a station has,
    direct connections, adjacent stations, nearby stations, or what direct transfers/connections
    are available at that station.
    This only queries one-hop connections and does not calculate a complete route.

Station ID rules:
- Metro station IDs start with "MS", for example "MS01".
- National rail station IDs start with "NR", for example "NR01".
"""

# TASK 6 EXTENSION: This file contains the bonus-feature query function
#   query_stations_by_zone (metro fare-zone lookup). See TASK6.md.

from __future__ import annotations

from typing import Optional

from neo4j import GraphDatabase

from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD


def _driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def example_count_nodes() :
    """
    Example function: calculates the total number of nodes in the current Neo4j graph.

    Note:
        This function is mainly used for development, testing, and verifying whether
        the Neo4j connection is successful.
        It is not an official query tool for the TransitFlow AI agent.
        If the user asks about routes, transfers, fares, delay impacts, or station connections,
        this function should not be called.

    Suitable for:
        - Developers who want to quickly check whether Neo4j contains data.
        - Testing whether data has been successfully seeded into the graph database.

    Not suitable for:
        - Not suitable for answering any route planning questions.
        - Not suitable for answering questions about the fastest route, cheapest route,
        alternative routes, transfers, or delay impacts.

    Returns:
        int, the total number of all nodes in the current graph.
    """
    with _driver() as driver:
        with driver.session() as session:
            result = session.run("MATCH (n) RETURN count(n) AS total")
            return result.single()["total"]

def _relationship_pattern(network: str, origin_id: str = "", destination_id: str = "") :
    """
    Returns a safe Cypher relationship type pattern based on the specified network
    and the origin/destination station IDs.

    Note:
        This is an internal helper function, not a tool intended to be called directly
        by the AI agent or the user.
        Since Cypher relationship types cannot be passed in as parameters, this function
        only returns relationship types that are explicitly allowed in the program,
        preventing arbitrary user input from being inserted directly into Cypher.

    Network rules:
        - "metro":
            Only allows METRO_LINK, suitable for routes between metro stations.
        - "rail":
            Only allows RAIL_LINK, suitable for routes between national rail stations.
        - "auto":
            If both origin_id and destination_id start with "MS", only use METRO_LINK.
            If both origin_id and destination_id start with "NR", only use RAIL_LINK.
            If the origin and destination belong to different networks, allow METRO_LINK,
            RAIL_LINK, and INTERCHANGE_TO so that routes can transfer across metro and
            national rail.
        - Other values:
            To maintain usability, METRO_LINK, RAIL_LINK, and INTERCHANGE_TO are allowed
            by default.

    Parameters:
        network:
            The search network scope. Recommended values are "metro", "rail", or "auto".
        origin_id:
            The origin station ID, such as "MS01" or "NR01".
        destination_id:
            The destination station ID, such as "MS09" or "NR05".

    Returns:
        str, a relationship type string that can be safely inserted into a Cypher
        relationship pattern.
        For example, "METRO_LINK", "RAIL_LINK", or "METRO_LINK|RAIL_LINK|INTERCHANGE_TO".
    """
    network = (network or "auto").lower()

    if network == "metro":
        return "METRO_LINK"

    if network == "rail":
        return "RAIL_LINK"

    if network == "auto":
        if origin_id.startswith("MS") and destination_id.startswith("MS"):
            return "METRO_LINK"
        if origin_id.startswith("NR") and destination_id.startswith("NR"):
            return "RAIL_LINK"
        return "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"

    return "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"

def _alternative_relationship_pattern(network="auto"):
    """
    Relationship pattern for alternative-route search.

    Unlike normal shortest-route search, an alternative route may need to leave the
    original network and come back through an interchange. For example, when a
    National Rail station is disrupted, the best valid detour may use Metro links.

    Therefore network="auto" intentionally allows all transit relationship types.
    Explicit network="metro" or network="rail" remains restricted for cases where
    a caller truly wants a single-network search.
    """
    network = (network or "auto").lower()

    if network == "metro":
        return "METRO_LINK"

    if network == "rail":
        return "RAIL_LINK"

    return "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"

def query_station_connections(station_id: str) :
    """
    Queries the one-hop direct connection stations of a given station.

    Purpose:
        Returns the metro, national rail, or interchange connections that are directly adjacent
        to the specified station.
        This function only queries stations that are directly connected. It does not calculate
        a complete route from an origin to a destination, nor does it search for the fastest route,
        cheapest route, or alternative routes.

    Suitable for:
        - When the user asks which stations are directly connected to a certain station.
        - When the user asks about adjacent stations, nearby stations, or direct connections.
        - When the user asks which available lines, direct transfers, or one-hop neighbors a station has.
        - When the user wants to understand the direct connection relationships of a station
        in the graph network.
        - When the user asks “Which stations are directly near MS01?” or “Who is NR03 directly connected to?”

    Not suitable for:
        - If the user asks for the fastest route from A to B, use query_shortest_route instead.
        - If the user asks for the cheapest route from A to B, use query_cheapest_route instead.
        - If the user asks about avoiding a station, station closure, delay detours,
        or alternative routes, use query_alternative_routes instead.
        - If the user asks for a complete transfer path between metro and national rail,
        use query_interchange_path instead.
        - If the user asks which stations would be affected by a delay at a certain station,
        use query_delay_ripple instead.

    Parameters:
        station_id:
            The station ID whose direct connections should be queried.
            Metro station IDs start with "MS", for example "MS01".
            National rail station IDs start with "NR", for example "NR01".

    Returns:
        list[dict], where each dict represents a one-hop direct connection from the station,
        including:
        - from_id: The queried station ID.
        - from_name: The queried station name.
        - relationship_type: The connection type, such as METRO_LINK, RAIL_LINK, or INTERCHANGE_TO.
        - line: The metro or rail line name; may be None for INTERCHANGE_TO.
        - travel_time_min: The estimated time of this direct connection, in minutes.
        - fare_usd: The estimated cost of this direct connection; usually 0 for walking transfers.
        - to_id: The adjacent station ID.
        - to_name: The adjacent station name.
        - to_labels: The Neo4j labels of the adjacent station.

    Example use cases:
        User asks: “Which stations is MS01 directly connected to?”
        User asks: “Which directly adjacent stations are near Central Square?”
        User asks: “What direct connections does NR03 have?”
    """
    cypher = """
    MATCH (s {station_id: $station_id})-[r]->(n)
    RETURN
        s.station_id AS from_id,
        s.name AS from_name,
        type(r) AS relationship_type,
        r.line AS line,
        coalesce(r.travel_time_min, r.walk_time_min, 0) AS travel_time_min,
        coalesce(r.fare_usd, r.fare_standard, 0) AS fare_usd,
        n.station_id AS to_id,
        n.name AS to_name,
        labels(n) AS to_labels
    ORDER BY relationship_type, line, to_id
    """

    with _driver() as driver:
        with driver.session() as session:
            result = session.run(cypher, station_id=station_id)
            return [dict(record) for record in result]


def query_shortest_route(
    origin_id,
    destination_id,
    network = "auto",
) :
    """
    Purpose:
        Queries the route with the shortest total travel time between two stations.

    Suitable for:
        - When the user asks for the quickest route, shortest-time route, quickest route,
        or fastest route.
        - When the user asks “how to get from A to B” without specifically requesting
        the cheapest route, avoiding a station, or checking delay impact.
        - Can handle routes within the metro network, within the national rail network,
        and cross-network metro / national rail routes when network="auto".

    Not suitable for:
        - If the user asks for the cheapest route, lowest fare, lowest fare, or cheapest route,
        use query_cheapest_route instead.
        - If the user asks about avoiding a station, station closure, delay detours,
        or alternative routes, use query_alternative_routes instead.
        - If the user mainly wants to know which stations would be affected by a delay
        at a certain station, use query_delay_ripple instead.
        - If the user only asks which stations are directly connected to a certain station,
        use query_station_connections instead.
        - If the user explicitly emphasizes transfer points or interchange explanations,
        prioritize query_interchange_path.

    Parameters:
        origin_id:
            The origin station ID. Metro station IDs start with "MS", for example "MS01";
            national rail station IDs start with "NR", for example "NR01".
        destination_id:
            The destination station ID. Same format as origin_id, such as "MS09" or "NR05".
        network:
            The search scope.
            - "metro": only searches the metro network.
            - "rail": only searches the national rail network.
            - "auto": automatically infers the network based on station IDs; if the origin
            and destination belong to different networks, INTERCHANGE_TO transfers are allowed.

    Returns:
        dict, including:
        - found: whether a route was found.
        - origin_id, destination_id: the origin and destination station IDs.
        - total_time_min: the estimated total travel time, in minutes.
        - path: an ordered list of stations.
        - legs: each route segment’s from/to station, relationship_type, line,
        and travel_time_min.
    """
    origin_id = origin_id.upper()
    destination_id = destination_id.upper()
    rel_pattern = _relationship_pattern(network, origin_id, destination_id)
    max_depth = 8

    cypher = f"""
    MATCH path = (a {{station_id: $origin_id}})-[:{rel_pattern}*1..{max_depth}]-(b {{station_id: $destination_id}})
    WHERE all(i IN range(0, size(nodes(path)) - 2)
              WHERE NOT nodes(path)[i] IN nodes(path)[i + 1..])
    WITH path,
         reduce(total = 0, r IN relationships(path) |
             total + coalesce(r.travel_time_min, r.walk_time_min, 0)
         ) AS total_time_min
    ORDER BY total_time_min ASC, length(path) ASC
    LIMIT 1
    RETURN
        total_time_min,
        [n IN nodes(path) | {{
            station_id: n.station_id,
            name: n.name,
            labels: labels(n)
        }}] AS stations,
        [i IN range(0, size(relationships(path)) - 1) | {{
            from_id: nodes(path)[i].station_id,
            from_name: nodes(path)[i].name,
            to_id: nodes(path)[i + 1].station_id,
            to_name: nodes(path)[i + 1].name,
            relationship_type: type(relationships(path)[i]),
            line: relationships(path)[i].line,
            travel_time_min: coalesce(
                relationships(path)[i].travel_time_min,
                relationships(path)[i].walk_time_min,
                0
            )
        }}] AS legs
    """

    with _driver() as driver:
        with driver.session() as session:
            record = session.run(
                cypher,
                origin_id=origin_id,
                destination_id=destination_id,
            ).single()

            if record is None:
                return {
                    "found": False,
                    "origin_id": origin_id,
                    "destination_id": destination_id,
                    "total_time_min": None,
                    "path": [],
                    "legs": [],
                }

            return {
                "found": True,
                "origin_id": origin_id,
                "destination_id": destination_id,
                "total_time_min": record["total_time_min"],
                "path": record["stations"],
                "legs": record["legs"],
            }


def query_cheapest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
    fare_class: str = "standard",
) :
    """
    Queries the route with the lowest estimated cost between two stations.

    Purpose:
        Finds the route with the lowest estimated transportation cost from the origin
        to the destination based on fare_usd, fare_standard, or fare_first on Neo4j
        graph relationships.
        This function prioritizes the lowest cost, not the shortest travel time.

    Suitable for:
        - When the user asks for the cheapest route, lowest fare, cheapest route, or lowest fare.
        - When the user wants to compare which route from A to B costs less.
        - When the user explicitly states that they care more about fare or cost than speed.
        - When the user asks for the estimated route cost for standard or first class.

    Not suitable for:
        - If the user asks for the fastest route, shortest time, quickest route, or fastest route,
        use query_shortest_route instead.
        - If the user asks about avoiding a station, station closure, delay detours,
        or alternative routes, use query_alternative_routes instead.
        - If the user asks which stations would be affected by a delay at a certain station,
        use query_delay_ripple instead.
        - If the user only asks which stations are directly connected to a certain station,
        use query_station_connections instead.
        - If the user wants to query actual ticket prices, seat prices, service fares,
        or booking information, this function should not be used; this function only provides
        estimated costs for graph routes.

    Parameters:
        origin_id:
            The origin station ID.
            Metro station IDs start with "MS", for example "MS01".
            National rail station IDs start with "NR", for example "NR01".
        destination_id:
            The destination station ID.
            Same format as origin_id, such as "MS09" or "NR05".
        network:
            The search network scope.
            - "metro": only searches the metro network.
            - "rail": only searches the national rail network.
            - "auto": automatically determines the search scope and allows cross-network
            transfers when necessary.
        fare_class:
            The fare class for national rail.
            - "standard": uses fare_standard.
            - "first": uses fare_first.
            Metro links use fare_usd.
            INTERCHANGE_TO walking transfers are usually treated as 0 dollars.

    Returns:
        dict, including:
        - found: whether a route was found.
        - origin_id: the origin station ID.
        - destination_id: the destination station ID.
        - fare_class: the rail fare class used.
        - total_fare_usd: the estimated total cost, in USD.
        - stations: an ordered list of stations.
        - legs: each route segment, including from/to station, relationship_type, line,
        fare_usd, and other information.

    Example use cases:
        User asks: “What is the cheapest way to get from MS01 to NR05?”
        User asks: “What is the cheapest route from Central Square to Stonehaven?”
        User asks: “Which first-class route from NR01 to NR05 has the lowest cost?”
    """
    rel_pattern = _relationship_pattern(network, origin_id, destination_id)
    fare_class = (fare_class or "standard").lower()

    if fare_class == "first":
        fare_expr = "coalesce(r.fare_usd, r.fare_first, r.fare_standard, 0)"
        leg_fare_expr = """
        coalesce(
            relationships(path)[i].fare_usd,
            relationships(path)[i].fare_first,
            relationships(path)[i].fare_standard,
            0
        )
        """
    else:
        fare_expr = "coalesce(r.fare_usd, r.fare_standard, 0)"
        leg_fare_expr = """
        coalesce(
            relationships(path)[i].fare_usd,
            relationships(path)[i].fare_standard,
            0
        )
        """

    cypher = f"""
    MATCH path = (a {{station_id: $origin_id}})-[:{rel_pattern}*1..8]-(b {{station_id: $destination_id}})
    WHERE all(i IN range(0, size(nodes(path)) - 2)
              WHERE NOT nodes(path)[i] IN nodes(path)[i + 1..])
    WITH path,
         reduce(total = 0.0, r IN relationships(path) |
             total + {fare_expr}
         ) AS total_fare_usd
    ORDER BY total_fare_usd ASC, length(path) ASC
    LIMIT 1
    RETURN
        total_fare_usd,
        [n IN nodes(path) | {{
            station_id: n.station_id,
            name: n.name,
            labels: labels(n)
        }}] AS stations,
        [i IN range(0, size(relationships(path)) - 1) | {{
            from_id: nodes(path)[i].station_id,
            from_name: nodes(path)[i].name,
            to_id: nodes(path)[i + 1].station_id,
            to_name: nodes(path)[i + 1].name,
            relationship_type: type(relationships(path)[i]),
            line: relationships(path)[i].line,
            fare_usd: {leg_fare_expr}
        }}] AS legs
    """

    with _driver() as driver:
        with driver.session() as session:
            record = session.run(
                cypher,
                origin_id=origin_id,
                destination_id=destination_id,
            ).single()

            if record is None:
                return {
                    "found": False,
                    "origin_id": origin_id,
                    "destination_id": destination_id,
                    "fare_class": fare_class,
                    "total_fare_usd": None,
                    "stations": [],
                    "legs": [],
                }

            return {
                "found": True,
                "origin_id": origin_id,
                "destination_id": destination_id,
                "fare_class": fare_class,
                "total_fare_usd": record["total_fare_usd"],
                "stations": record["stations"],
                "legs": record["legs"],
            }


def query_alternative_routes(
    origin_id,
    destination_id,
    avoid_station_id,
    network = "auto",
    max_routes = 3,
) :
    """
    Queries alternative routes that avoid a specified station.

    Purpose:
        Finds feasible alternative routes from the origin to the destination based on the Neo4j graph,
        while excluding any paths that contain avoid_station_id.
        This function is mainly used for station closures, delays, disruptions, detours,
        and alternative route scenarios.

    Suitable for:
        - When the user asks for an alternative route, alternative routes, or another way to go.
        - When the user explicitly asks to avoid a certain station, such as “do not pass through NR03.”
        - When the user indicates that a station is closed, under construction, delayed,
        disrupted, or affected by a disruption.
        - When the user asks “if a certain station is unavailable, how can I get from A to B?”
        - When the user wants to know whether a feasible route still exists after avoiding
        a problem station.

    Not suitable for:
        - If the user is simply asking for the fastest route and does not mention avoiding
        a station or alternative routes, use query_shortest_route instead.
        - If the user asks for the cheapest route or lowest fare, use query_cheapest_route instead.
        - If the user mainly wants to know where to transfer between metro and national rail,
        and does not ask to avoid any station, use query_interchange_path instead.
        - If the user only wants to know which nearby stations would be affected by a delay
        at a certain station, use query_delay_ripple instead.
        - If the user only asks which stations are directly connected to a certain station,
        use query_station_connections instead.

    Parameters:
        origin_id:
            The origin station ID.
            Metro station IDs start with "MS", for example "MS01".
            National rail station IDs start with "NR", for example "NR01".
        destination_id:
            The destination station ID.
            Same format as origin_id, such as "MS09" or "NR05".
        avoid_station_id:
            The station ID that must be avoided.
            None of the returned routes should contain this station.
            For example, "NR03" or "MS07".
        network:
            The search network scope.
            - "metro": only searches the metro network.
            - "rail": only searches the national rail network.
            - "auto": automatically determines the search scope and allows cross-network
            metro / national rail transfers when necessary.
        max_routes:
            The maximum number of alternative routes to return.
            Defaults to 3. If fewer feasible routes exist, only the found routes are returned.

    Returns:
        list[list[dict]].
        The outer list represents multiple alternative routes.
        Each route is a list of legs; each leg dict contains:
        - from_id: the origin station ID of this leg.
        - from_name: the origin station name of this leg.
        - to_id: the destination station ID of this leg.
        - to_name: the destination station name of this leg.
        - relationship_type: METRO_LINK, RAIL_LINK, or INTERCHANGE_TO.
        - line: the metro or rail line name; may be None for transfers.
        - travel_time_min: the estimated travel time of this leg, in minutes.

        If no feasible route exists after avoiding the specified station, returns an empty list [].

    Example use cases:
        User asks: “If NR03 is closed, are there any other routes from MS01 to NR05?”
        User asks: “Please avoid NR03 when going from MS01 to NR05.”
        User asks: “Find alternative routes from Central Square to Stonehaven avoiding Old Town Junction.”
    """
    origin_id = origin_id.upper()
    destination_id = destination_id.upper()
    avoid_station_id = avoid_station_id.upper()
    rel_pattern = _alternative_relationship_pattern(network)
    max_routes = max(1, int(max_routes))
    max_depth = 12

    cypher = f"""
    MATCH path = (a {{station_id: $origin_id}})-[:{rel_pattern}*1..{max_depth}]-(b {{station_id: $destination_id}})
    WHERE NONE(n IN nodes(path) WHERE n.station_id = $avoid_station_id)
    AND all(i IN range(0, size(nodes(path)) - 2)
            WHERE NOT nodes(path)[i] IN nodes(path)[i + 1..])
    WITH path,
         [n IN nodes(path) | n.station_id] AS route_key,
         reduce(total = 0, r IN relationships(path) |
             total + coalesce(r.travel_time_min, r.walk_time_min, 0)
         ) AS total_time_min
    ORDER BY total_time_min ASC, length(path) ASC
    WITH route_key, collect({{path: path, total_time_min: total_time_min}})[0] AS best_path
    WITH best_path.path AS path, best_path.total_time_min AS total_time_min
    ORDER BY total_time_min ASC, length(path) ASC
    LIMIT $max_routes
    RETURN
        [i IN range(0, size(relationships(path)) - 1) | {{
            from_id: nodes(path)[i].station_id,
            from_name: nodes(path)[i].name,
            to_id: nodes(path)[i + 1].station_id,
            to_name: nodes(path)[i + 1].name,
            relationship_type: type(relationships(path)[i]),
            line: relationships(path)[i].line,
            travel_time_min: coalesce(
                relationships(path)[i].travel_time_min,
                relationships(path)[i].walk_time_min,
                0
            )
        }}] AS legs
    """

    with _driver() as driver:
        with driver.session() as session:
            result = session.run(
                cypher,
                origin_id=origin_id,
                destination_id=destination_id,
                avoid_station_id=avoid_station_id,
                max_routes=max_routes,
            )
            return [record["legs"] for record in result]


def query_interchange_path(origin_id: str, destination_id: str) :
    """
    Queries cross-network routes that include a transfer between metro and national rail.

    Purpose:
        Finds a route from the origin to the destination where the route must include
        at least one INTERCHANGE_TO segment.
        This function is mainly used to explain how to transfer between metro and
        national rail, where the interchange happens, and the walking time required
        for the transfer.

    Suitable for:
        - When the user asks how to transfer between metro and national rail.
        - When the user explicitly mentions interchange, transfer, transferring,
        changing services, or cross-network travel.
        - When the user wants to know how to get from a metro station to a national rail station.
        - When the user wants to know how to get from a national rail station to a metro station.
        - When the user asks “where do I transfer from metro to national rail?”
        or “which station allows interchange?”

    Not suitable for:
        - If the user is simply asking for the fastest route and does not explicitly
        request a transfer explanation, use query_shortest_route instead.
        - If the user asks for the cheapest route or lowest fare,
        use query_cheapest_route instead.
        - If the user asks about avoiding a station, station closure, delay detours,
        or alternative routes, use query_alternative_routes instead.
        - If the user asks which nearby stations would be affected by a delay
        at a certain station, use query_delay_ripple instead.
        - If the user only asks which stations are directly connected to a certain station,
        use query_station_connections instead.
        - If both the origin and destination are within the same network, and the user
        does not request a transfer or interchange, this function should not be prioritized.

    Parameters:
        origin_id:
            The origin station ID.
            Metro station IDs start with "MS", for example "MS01".
            National rail station IDs start with "NR", for example "NR01".
        destination_id:
            The destination station ID.
            Same format as origin_id, such as "MS09" or "NR05".

    Returns:
        dict, including:
        - found: whether a cross-network route containing an interchange was found.
        - origin_id: the origin station ID.
        - destination_id: the destination station ID.
        - stations: an ordered list of stations.
        - interchange_points: a list of interchange segments, including from_id, to_id,
        from_name, to_name, and walk_time_min.
        - total_time_min: the estimated total travel time, including riding time and
        walking time for transfers.

        If no route containing INTERCHANGE_TO is found, found will be False.

    Example use cases:
        User asks: “Where do I transfer when going from MS01 to NR05?”
        User asks: “How do I transfer from metro to national rail from Central Square?”
        User asks: “What is the cross-network transfer route from NR01 to MS18?”
    """
    cypher = """
    MATCH path = (a {station_id: $origin_id})-[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*1..8]-(b {station_id: $destination_id})
    WHERE ANY(r IN relationships(path) WHERE type(r) = "INTERCHANGE_TO")
      AND all(i IN range(0, size(nodes(path)) - 2)
              WHERE NOT nodes(path)[i] IN nodes(path)[i + 1..])
    WITH path,
         reduce(total = 0, r IN relationships(path) |
             total + coalesce(r.travel_time_min, r.walk_time_min, 0)
         ) AS total_time_min
    ORDER BY total_time_min ASC, length(path) ASC
    LIMIT 1
    RETURN
        total_time_min,
        [n IN nodes(path) | {
            station_id: n.station_id,
            name: n.name,
            labels: labels(n)
        }] AS stations,
        [n IN nodes(path)
            WHERE "MetroStation" IN labels(n) OR "NationalRailStation" IN labels(n)
            | {
                station_id: n.station_id,
                name: n.name,
                labels: labels(n)
            }
        ] AS all_points,
        [i IN range(0, size(relationships(path)) - 1)
            WHERE type(relationships(path)[i]) = "INTERCHANGE_TO"
            | {
                from_id: nodes(path)[i].station_id,
                from_name: nodes(path)[i].name,
                to_id: nodes(path)[i + 1].station_id,
                to_name: nodes(path)[i + 1].name,
                walk_time_min: coalesce(relationships(path)[i].walk_time_min, 0)
            }
        ] AS interchange_points
    """

    with _driver() as driver:
        with driver.session() as session:
            record = session.run(
                cypher,
                origin_id=origin_id,
                destination_id=destination_id,
            ).single()

            if record is None:
                return {
                    "found": False,
                    "origin_id": origin_id,
                    "destination_id": destination_id,
                    "stations": [],
                    "interchange_points": [],
                    "total_time_min": None,
                }

            return {
                "found": True,
                "origin_id": origin_id,
                "destination_id": destination_id,
                "stations": record["stations"],
                "interchange_points": record["interchange_points"],
                "total_time_min": record["total_time_min"],
            }


def query_delay_ripple(delayed_station_id: str, hops: int = 2) :
    """
    Queries nearby stations that may be affected when a station is delayed or disrupted.

    Purpose:
        Starting from the specified delayed_station_id, searches outward N hops in the Neo4j graph
        to identify stations that may be affected by delays, closures, disruptions, or operational issues.
        This function is used for network impact scope analysis, not for planning a route from A to B.

    Suitable for:
        - When the user asks which stations would be affected by a delay at a certain station.
        - When the user asks about disruption impact, delay ripple, ripple effect, or affected stations.
        - When the user asks which stations within N stops / N hops of a certain station may be affected.
        - When the user wants to understand the spread of a station incident.
        - When the user asks “If NR03 has a problem, which nearby stations would be affected?”

    Not suitable for:
        - If the user asks how to get from A to B, use query_shortest_route instead.
        - If the user asks for the cheapest way to get from A to B, use query_cheapest_route instead.
        - If the user asks whether there is an alternative route after avoiding a station,
        use query_alternative_routes instead.
        - If the user asks for a transfer path between metro and national rail,
        use query_interchange_path instead.
        - If the user only wants to know which stations are directly connected to a certain station,
        use query_station_connections instead.

    Parameters:
        delayed_station_id:
            The origin station ID where the delay, disruption, or issue occurs.
            Metro station IDs start with "MS", for example "MS01".
            National rail station IDs start with "NR", for example "NR03".
        hops:
            The number of graph hops to search outward from delayed_station_id.
            Defaults to 2.
            A larger value means a wider impact scope.
            This system limits hops to a reasonable range to avoid overly large queries.

    Returns:
        list[dict], where each dict represents a potentially affected station, including:
        - station_id: the affected station ID.
        - name: the affected station name.
        - labels: Neo4j labels, such as MetroStation or NationalRailStation.
        - hops_away: the shortest graph hop count from the delayed station to this station.
        - lines_affected: the metro line, rail line, or INTERCHANGE involved in the affected path.

        If no affected stations are found, returns an empty list [].

    Example use cases:
        User asks: “Which stations would be affected by a delay at NR03?”
        User asks: “Show affected stations within 2 hops of Old Town Junction.”
        User asks: “If MS01 has a problem, would stations within two stops nearby be affected?”
    """
    hops = max(1, min(int(hops), 10))

    cypher = f"""
    MATCH path = (s {{station_id: $delayed_station_id}})-[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*1..{hops}]-(affected)
    WHERE s <> affected
    WITH affected,
         length(path) AS hops_away,
    
    [r IN relationships(path) |
      CASE
        WHEN r.line IS NOT NULL THEN r.line
        WHEN type(r) = "INTERCHANGE_TO" THEN "INTERCHANGE"
        ELSE null
      END
    ] AS lines
         
    ORDER BY hops_away ASC, affected.station_id ASC
    RETURN
        affected.station_id AS station_id,
        affected.name AS name,
        labels(affected) AS labels,
        min(hops_away) AS hops_away,
        collect(lines) AS line_lists
    """

    with _driver() as driver:
        with driver.session() as session:
            result = session.run(cypher, delayed_station_id=delayed_station_id)

            rows = []
            for record in result:
                lines_affected = sorted({
                    line
                    for line_list in record["line_lists"]
                    for line in line_list
                    if line is not None
                })

                rows.append({
                    "station_id": record["station_id"],
                    "name": record["name"],
                    "labels": record["labels"],
                    "hops_away": record["hops_away"],
                    "lines_affected": lines_affected,
                })

            return rows


# ── ZONE QUERIES ─────────────────────────────────────────────────────────────

def query_stations_by_zone(zone: int) -> list[dict]:
    """
    Return all MetroStation nodes in a given fare zone.

    Args:
        zone: 1, 2, or 3

    Returns:
        List of dicts with station_id, name, zone, lines, ordered by station_id.
    """
    cypher = """
    MATCH (s:MetroStation {zone: $zone})
    RETURN s.station_id AS station_id, s.name AS name, s.zone AS zone, s.lines AS lines
    ORDER BY s.station_id
    """
    with _driver() as driver:
        with driver.session() as session:
            result = session.run(cypher, zone=zone)
            return [
                {
                    "station_id": record["station_id"],
                    "name":       record["name"],
                    "zone":       record["zone"],
                    "lines":      list(record["lines"]) if record["lines"] else [],
                }
                for record in result
            ]