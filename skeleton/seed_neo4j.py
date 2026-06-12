"""
TransitFlow — Neo4j Seeder
Run once after starting Docker:
    python skeleton/seed_neo4j.py

Loads station and network data from train-mock-data/:
  - metro_stations.json         — city metro stations and adjacencies
  - national_rail_stations.json — national rail stations and adjacencies

Design your graph schema (node labels, relationship types, properties)
based on the data in these files, then implement the seed() function below.
"""

# TASK 6 EXTENSION: Writes the bonus `zone` property onto MetroStation nodes
#   during seeding (metro fare zones 1-3). See TASK6.md.

import json
import os
import sys

sys.path.insert(0, ".")

from neo4j import GraphDatabase
from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train-mock-data")
)


def _load(filename):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def seed():
    metro_stations = _load("metro_stations.json")
    rail_stations  = _load("national_rail_stations.json")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:

        session.run("MATCH (n) DETACH DELETE n")
        print("  Cleared existing graph data")


        session.run("""
        CREATE CONSTRAINT metro_station_id_unique IF NOT EXISTS
        FOR (s:MetroStation)
        REQUIRE s.station_id IS UNIQUE
        """)

        session.run("""
        CREATE CONSTRAINT national_rail_station_id_unique IF NOT EXISTS
        FOR (s:NationalRailStation)
        REQUIRE s.station_id IS UNIQUE
        """)


        for station in metro_stations:
            session.run(
                """
                MERGE (s:MetroStation {station_id: $station_id})
                SET
                    s.name = $name,
                    s.zone = $zone,
                    s.lines = $lines,
                    s.is_interchange_metro = $is_interchange_metro,
                    s.is_interchange_national_rail = $is_interchange_national_rail
                """,
                station_id=station["station_id"],
                name=station["name"],
                zone=station.get("zone"),
                lines=station.get("lines", []),
                is_interchange_metro=station.get("is_interchange_metro", False),
                is_interchange_national_rail=station.get("is_interchange_national_rail", False),
            )

        print(f"  Created {len(metro_stations)} MetroStation nodes")


        for station in rail_stations:
            session.run(
                """
                MERGE (s:NationalRailStation {station_id: $station_id})
                SET
                    s.name = $name,
                    s.lines = $lines,
                    s.is_interchange_metro = $is_interchange_metro,
                    s.is_interchange_national_rail = $is_interchange_national_rail
                """,
                station_id=station["station_id"],
                name=station["name"],
                lines=station.get("lines", []),
                is_interchange_metro=station.get("is_interchange_metro", False),
                is_interchange_national_rail=station.get("is_interchange_national_rail", False),
            )

        print(f"  Created {len(rail_stations)} NationalRailStation nodes")

  
        for station in metro_stations:
            from_id = station["station_id"]

            for adjacent in station.get("adjacent_stations", []):
                session.run(
                    """
                    MATCH (a:MetroStation {station_id: $from_id})
                    MATCH (b:MetroStation {station_id: $to_id})
                    MERGE (a)-[r:METRO_LINK {line: $line}]->(b)
                    SET
                        r.travel_time_min = $travel_time_min,
                        r.fare_usd = $fare_usd
                    """,
                    from_id=from_id,
                    to_id=adjacent["station_id"],
                    line=adjacent["line"],
                    travel_time_min=adjacent["travel_time_min"],
                    fare_usd=1.0,
                )

        print("  Created METRO_LINK relationships")

        for station in rail_stations:
            from_id = station["station_id"]

            for adjacent in station.get("adjacent_stations", []):
                session.run(
                    """
                    MATCH (a:NationalRailStation {station_id: $from_id})
                    MATCH (b:NationalRailStation {station_id: $to_id})
                    MERGE (a)-[r:RAIL_LINK {line: $line}]->(b)
                    SET
                        r.travel_time_min = $travel_time_min,
                        r.fare_standard = $fare_standard,
                        r.fare_first = $fare_first
                    """,
                    from_id=from_id,
                    to_id=adjacent["station_id"],
                    line=adjacent["line"],
                    travel_time_min=adjacent["travel_time_min"],
                    fare_standard=2.0,
                    fare_first=4.0,
                )

        print("  Created RAIL_LINK relationships")

        for station in metro_stations:
            if station.get("is_interchange_national_rail") and station.get("interchange_national_rail_station_id"):
                metro_id = station["station_id"]
                rail_id = station["interchange_national_rail_station_id"]

                session.run(
                    """
                    MATCH (m:MetroStation {station_id: $metro_id})
                    MATCH (r:NationalRailStation {station_id: $rail_id})
                    MERGE (m)-[to_rail:INTERCHANGE_TO]->(r)
                    SET to_rail.walk_time_min = $walk_time_min
                    MERGE (r)-[to_metro:INTERCHANGE_TO]->(m)
                    SET to_metro.walk_time_min = $walk_time_min
                    """,
                    metro_id=metro_id,
                    rail_id=rail_id,
                    walk_time_min=5,
                )

        print("  Created INTERCHANGE_TO relationships")

    driver.close()
    print("\nNeo4j graph seeded successfully.")
    print("   Open http://localhost:7475 to explore the graph.")


if __name__ == "__main__":
    print("Connecting to Neo4j...")
    seed()
