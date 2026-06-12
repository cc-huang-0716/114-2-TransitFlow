from __future__ import annotations

import re
from typing import Any, Optional

from databases.relational.queries import (
    query_policy_vector_search,
    query_policy_keyword_search,
)
from skeleton.config import VECTOR_TOP_K

CATEGORY_KEYWORDS = {
    "refund": [
        "refund", "refunded", "refunds", "money back", "reimburse", "reimbursement",
        "cancel", "cancelled", "cancellation", "compensation", "delay", "delayed",
        "late", "missed train", "disruption",
    ],
    "ticket": [
        "ticket", "tickets", "ticket type", "fare type", "single", "return",
        "child", "children", "student", "adult", "senior", "pass", "discount",
    ],
    "booking": [
        "booking", "book", "reserved", "reservation", "reserve", "advance",
        "change booking", "change ticket", "modify", "reschedule", "seat selection",
    ],
    "travel": [
        "travel", "policy", "rule", "rules", "luggage", "baggage", "bicycle",
        "bike", "pet", "pets", "food", "drink", "conduct", "smoking", "lost property",
    ],
}


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "do", "does",
    "for", "from", "get", "how", "i", "if", "in", "is", "it", "me", "my", "of",
    "on", "or", "the", "there", "to", "what", "when", "where", "with", "would",
    "you", "your",
}


def normalize_query(query):
    query = (query or "").strip().lower()
    query = re.sub(r"\s+", " ", query)
    return query


def infer_policy_category(query):
    q = normalize_query(query)

    best_category = None
    best_hits = 0

    for category, keywords in CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in q)
        if hits > best_hits:
            best_category = category
            best_hits = hits

    return best_category


def extract_keywords(query):
    q = normalize_query(query)
    tokens = re.findall(r"[a-z0-9]+", q)
    keywords = [t for t in tokens if len(t) >= 3 and t not in STOPWORDS]
    phrases = [
        "money back", "child ticket", "student ticket", "ticket type",
        "fare type", "delay compensation", "cancellation fee", "seat selection",
    ]
    for phrase in phrases:
        if phrase in q and phrase not in keywords:
            keywords.append(phrase)

    return keywords


def vector_policy_search(query, llm, top_k = VECTOR_TOP_K):
    embedding = llm.embed(query)
    results = query_policy_vector_search(embedding=embedding, top_k=top_k)

    enriched = []
    for rank, doc in enumerate(results, start=1):
        item = dict(doc)
        item["retrieval_source"] = "vector"
        item["vector_rank"] = rank
        enriched.append(item)
    return enriched


def keyword_policy_search(
    query,
    top_k = VECTOR_TOP_K,
    category = None,
):
    results = query_policy_keyword_search(
        query=query,
        top_k=top_k,
        category=category,
    )

    enriched = []
    for rank, doc in enumerate(results, start=1):
        item = dict(doc)
        item["retrieval_source"] = "keyword"
        item["keyword_rank"] = rank
        enriched.append(item)
    return enriched


def _doc_key(doc):
    if doc.get("id") is not None:
        return f"id:{doc['id']}"

    title = str(doc.get("title", "")).strip().lower()
    category = str(doc.get("category", "")).strip().lower()
    source_file = str(doc.get("source_file", "")).strip().lower()
    content_prefix = str(doc.get("content", ""))[:160].strip().lower()
    return f"{title}|{category}|{source_file}|{content_prefix}"


def fuse_rag_results(
    vector_results,
    keyword_results,
    category = None,
    top_k = VECTOR_TOP_K,
):
    fused = {}
    rrf_k = 60

    def add_result(doc: dict[str, Any], source: str, rank: int) -> None:
        key = _doc_key(doc)
        if key not in fused:
            fused[key] = {
                "doc": dict(doc),
                "score": 0.0,
                "matched_by": set(),
            }

        fused[key]["score"] += 1 / (rrf_k + rank)
        fused[key]["matched_by"].add(source)
        stored_doc = fused[key]["doc"]
        for field in (
            "id", "title", "category", "content", "source_file", "created_at",
            "similarity", "keyword_rank", "vector_rank", "keyword_score",
        ):
            if field in doc and stored_doc.get(field) in (None, ""):
                stored_doc[field] = doc[field]
            elif field == "similarity" and field in doc:
                stored_doc[field] = doc[field]

    for rank, doc in enumerate(vector_results, start=1):
        add_result(doc, "vector", rank)

    for rank, doc in enumerate(keyword_results, start=1):
        add_result(doc, "keyword", rank)

    ranked_items = []
    for item in fused.values():
        doc = item["doc"]
        matched_by = item["matched_by"]
        if category and category.lower() in str(doc.get("category", "")).lower():
            item["score"] += 0.05
            doc["category_bonus"] = True
        else:
            doc["category_bonus"] = False
        if len(matched_by) >= 2:
            item["score"] += 0.10
            doc["hybrid_overlap_bonus"] = True
        else:
            doc["hybrid_overlap_bonus"] = False

        doc["matched_by"] = sorted(matched_by)
        doc["hybrid_score"] = round(float(item["score"]), 4)
        ranked_items.append(item)

    ranked_items.sort(key=lambda x: x["score"], reverse=True)
    return [item["doc"] for item in ranked_items[:top_k]]


def format_rag_context(docs):
    if not docs:
        return (
            "No relevant policy documents were found. "
            "Answer that the policy information is unavailable in the current database."
        )

    context_parts = []
    for i, doc in enumerate(docs, start=1):
        content = str(doc.get("content", "")).strip()
        if len(content) > 1200:
            content = content[:1200].rstrip() + "..."

        similarity = doc.get("similarity")
        if similarity is not None:
            try:
                similarity = round(float(similarity), 3)
            except (TypeError, ValueError):
                pass

        context_parts.append(
            f"[Policy Document {i}]\n"
            f"Title: {doc.get('title', 'Untitled')}\n"
            f"Category: {doc.get('category', 'Unknown')}\n"
            f"Matched By: {', '.join(doc.get('matched_by', []))}\n"
            f"Hybrid Score: {doc.get('hybrid_score', 'N/A')}\n"
            f"Vector Similarity: {similarity if similarity is not None else 'N/A'}\n"
            f"Content:\n{content}"
        )

    return "\n\n".join(context_parts)


def hybrid_policy_search(query, llm, top_k = VECTOR_TOP_K):
    normalized_query = normalize_query(query)
    category = infer_policy_category(normalized_query)
    candidate_k = max(top_k * 2, 6)

    vector_results = vector_policy_search(
        query=normalized_query,
        llm=llm,
        top_k=candidate_k,
    )
    keyword_results = keyword_policy_search(
        query=normalized_query,
        top_k=candidate_k,
        category=category,
    )

    fused_results = fuse_rag_results(
        vector_results=vector_results,
        keyword_results=keyword_results,
        category=category,
        top_k=top_k,
    )

    return {
        "query": query,
        "normalized_query": normalized_query,
        "retrieval_method": "hybrid_rag",
        "inferred_category": category,
        "keywords": extract_keywords(normalized_query),
        "vector_candidate_count": len(vector_results),
        "keyword_candidate_count": len(keyword_results),
        "retrieved_documents": fused_results,
        "context": format_rag_context(fused_results),
    }
