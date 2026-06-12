# README — TransitFlow Contribution Summary

## Overview

This README summarises my main contribution to the TransitFlow database management project.

My work focused mainly on three parts:

1. **Neo4j graph database implementation**
2. **Hybrid RAG policy retrieval**
3. **Agent integration, debugging, and final testing**

The goal of my work was to make TransitFlow able to answer graph-based route questions and policy-related natural language questions through the Gradio assistant interface.

---

## 1. Neo4j Graph Database Contribution

### 1.1 Purpose

I worked on the graph database component because route planning is naturally a graph traversal problem. In TransitFlow, stations are represented as nodes, and physical links between stations are represented as relationships.

The graph database is responsible for:

- Fastest route search
- Cheapest route search
- Cross-network interchange routes
- Alternative routes avoiding a closed or delayed station
- Delay ripple analysis

---

### 1.2 Graph Schema

The graph uses two main node labels:

```text
MetroStation
NationalRailStation
```

The graph uses three main relationship types:

```text
METRO_LINK
RAIL_LINK
INTERCHANGE_TO
```

This design allows the system to model both the city metro network and the national rail network, while also supporting transfers between the two networks.

---

### 1.3 Main Files

The main graph-related files I worked on were:

```text
skeleton/seed_neo4j.py
databases/graph/queries.py
```

`seed_neo4j.py` is used to create and populate the Neo4j graph.

`databases/graph/queries.py` contains the graph query functions used by the agent.

---

### 1.4 Implemented Graph Functions

I implemented and tested the following graph functions:

```python
query_station_connections()
query_shortest_route()
query_cheapest_route()
query_interchange_path()
query_alternative_routes()
query_delay_ripple()
```

| Function | Purpose |
|---|---|
| `query_station_connections()` | Finds directly connected stations |
| `query_shortest_route()` | Finds the fastest route by travel time |
| `query_cheapest_route()` | Finds the cheapest route by fare |
| `query_interchange_path()` | Finds routes between Metro and National Rail |
| `query_alternative_routes()` | Finds routes avoiding a closed or delayed station |
| `query_delay_ripple()` | Finds stations affected within N hops of a disruption |

---

## 2. Route Query Testing

I tested the graph query functions through both direct Python calls and the Gradio UI.

Example tested query:

```text
What is the fastest route from MS01 to MS09?
```

Expected route:

```text
MS01 Central Square
→ MS07 Old Town
→ MS18 Sunnyvale
→ MS08 University
→ MS09 Queensbridge

Total travel time: 11 minutes
```

Another tested query:

```text
If NR03 is delayed, which stations may be affected within 2 hops?
```

Expected affected stations:

```text
1 hop:
- MS07 Old Town
- NR02 Maplewood
- NR04 Ashford

2 hops:
- MS01 Central Square
- MS18 Sunnyvale
- NR01 Central Station
- NR05 Stonehaven
```

---

## 3. Alternative Route Debugging

One important debugging issue involved alternative routes.

The system could find normal Metro-to-Rail routes, but alternative route queries sometimes failed when the route needed to cross from National Rail to Metro or from Metro to National Rail.

The issue was that the alternative route search could become too restrictive if it only searched one relationship type.

The fix was to make the alternative route query use all three relationship types when `network="auto"`:

```text
METRO_LINK | RAIL_LINK | INTERCHANGE_TO
```

This allows the system to find valid detours across both transit networks.

---

## 4. Agent Integration Contribution

### 4.1 Main File

The main agent file I worked on was:

```text
skeleton/agent.py
```

I helped connect the database query functions to the LLM-based assistant.

The agent is responsible for deciding which backend tool should be called when a user asks a question.

---

### 4.2 Tool Routing

I helped improve the routing logic for these intents:

| User intent | Tool |
|---|---|
| Fastest route | `find_route` with `optimise_by="time"` |
| Cheapest route | `find_route` with `optimise_by="cost"` |
| Alternative route | `find_alternative_routes` |
| Delay ripple / disruption impact | `get_delay_ripple` |
| Policy question | `search_policy` |

This made the system more reliable when the local LLM did not select the correct tool by itself.

---

### 4.3 Direct Route Formatter

I also helped stabilise route answers by using a direct formatter for graph query results.

This reduced the chance that the LLM would invent route details, recalculate travel time incorrectly, or contradict the database output.

The route answer formatter uses the database result directly, including:

```text
station order
total travel time
total fare
route legs
relationship type
line information
```

---

## 5. Hybrid RAG Contribution

### 5.1 Purpose

I worked on the Hybrid RAG component for policy-related questions.

Policy questions are different from structured database queries because users may ask them in flexible natural language.

For example:

```text
What is the company policy on travelling with a bicycle on national rail?
```

A pure keyword search may miss semantically similar wording, while a pure vector search may miss exact policy terms. Therefore, the system uses Hybrid RAG.

---

### 5.2 Main File

The main Hybrid RAG file I worked on was:

```text
skeleton/rag.py
```

---

### 5.3 Hybrid RAG Functions

The Hybrid RAG implementation includes:

```python
normalize_query()
infer_policy_category()
extract_keywords()
vector_policy_search()
keyword_policy_search()
fuse_rag_results()
format_rag_context()
hybrid_policy_search()
```

The retrieval pipeline is:

```text
User question
→ Query normalisation
→ Category inference
→ Keyword extraction
→ Vector search
→ Keyword search
→ Result fusion and reranking
→ Context formatting
→ LLM grounded answer
```

---

### 5.4 Vector Search

Vector search is used to retrieve semantically similar policy documents.

This helps when the user uses wording that does not exactly match the policy document.

Example:

```text
bike
```

can still match:

```text
bicycle policy
```

---

### 5.5 Keyword Search

Keyword search is used to capture exact terms, such as:

```text
refund
ticket
seat selection
student ticket
bicycle
delay compensation
```

This makes retrieval more precise for policy-specific questions.

---

### 5.6 Fusion and Reranking

The system combines vector search results and keyword search results.

Each document may include:

```text
matched_by
hybrid_score
category_bonus
hybrid_overlap_bonus
vector_rank
keyword_rank
similarity
```

Documents found by both vector and keyword search receive an additional overlap bonus.

This makes the final retrieved context more reliable than using only one retrieval method.

---

## 6. RAG Debugging

During testing, the policy search tool was selected correctly, but the debug panel showed an embedding error:

```text
404 Client Error: Not Found for url: http://localhost:11434/api/embeddings
```

I diagnosed that the required Ollama embedding model was missing.

The fix was:

```powershell
ollama pull nomic-embed-text
```

Then I tested the embedding endpoint directly:

```powershell
Invoke-RestMethod http://localhost:11434/api/embeddings `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"model":"nomic-embed-text","prompt":"bicycle policy"}'
```

After confirming the embedding API worked, I reran:

```powershell
python skeleton/seed_vectors.py
```

The system successfully embedded and stored 13 policy documents.

---

## 7. Hybrid RAG Test Example

Test query:

```text
What is the company policy on travelling with a bicycle on national rail?
```

Expected retrieved document:

```text
Travel Policies — National Rail
```

The retrieved policy included rules for:

```text
foldable bicycles
standard bicycles
peak-hour restrictions
bicycle placement
bicycle fees
```

This confirmed that the answer was grounded in retrieved policy documents rather than generated only from the LLM's general knowledge.

---

## 8. Final Debugging and Code Validation

I also helped fix final syntax and integration issues.

One issue was an extra empty `elif` statement in `skeleton/agent.py`, which caused:

```text
IndentationError: expected an indented block after 'elif'
```

I fixed the fallback control flow and verified the syntax using:

```powershell
python -m py_compile skeleton/agent.py
python -m compileall skeleton databases
```

These checks helped confirm that the Python files compiled correctly before final testing.

---

## 9. Main Files Related to My Work

The main files related to my contribution were:

```text
skeleton/seed_neo4j.py
databases/graph/queries.py
skeleton/agent.py
skeleton/rag.py
skeleton/seed_vectors.py
```

---

## 10. Summary

Overall, my contribution focused on making TransitFlow work as an integrated AI database assistant.

I contributed to:

- Neo4j graph database design and route query implementation
- Fastest route, cheapest route, interchange route, and alternative route support
- Delay ripple analysis
- Agent tool routing and fallback logic
- Hybrid RAG policy retrieval
- Ollama embedding and pgvector debugging
- Final syntax checking and UI testing

These components helped the system answer both graph-based route questions and policy-related natural language questions in a grounded and reliable way.
