"""
Spec-aligned scoring engine for SmartReco recommendations.

Used by get_recommendation_candidates() for category selection, similarity
filtering, owned-product exclusion, and recommendation rotation (diversify).
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from sqlalchemy.orm import Session

from database.models import Event, Product, Recommendation, User
from services.category_taxonomy import (
    ONBOARDING_TO_PRODUCT_CATEGORY_WEIGHTS,
    infer_category_from_query,
)
from services.scoring_weights import (
    CATEGORY_DOMINANCE_RATIO,
    DECAY_HALF_LIFE_DAYS,
    EVENT_BASE_WEIGHTS,
    EXPLICIT_INTEREST_BOOST,
    LOOKBACK_DAYS,
    MIN_EVENTS_FOR_PERSONALIZATION,
    SIMILARITY_THRESHOLD,
)

# Event types that use dwell-time multiplier (spec: product_view)
VIEW_EVENT_TYPES = frozenset({"view", "time_spent", "product_view"})


def recency_weight(days_ago: float, half_life: float = DECAY_HALF_LIFE_DAYS) -> float:
    """Exponential recency decay: half_life-day half-life."""
    return math.pow(0.5, max(days_ago, 0.0) / half_life)


def time_multiplier(seconds_spent: Optional[float]) -> float:
    """Dwell-time multiplier for view / product_view events (spec tiers)."""
    if seconds_spent is None:
        return 1.0
    if seconds_spent < 5:
        return 0.2
    if seconds_spent < 30:
        return 1.0
    if seconds_spent < 120:
        return 1.5
    return 2.0


def frequency_boost(count: int) -> float:
    """Log-based dampening boost for repeated category events."""
    return 1 + math.log(count + 1, 2)


def remove_bot_noise(
    events: Sequence[Dict[str, Any]],
    min_gap_seconds: float = 0.3,
) -> List[Dict[str, Any]]:
    """Drop events that fire faster than min_gap_seconds after the previous one."""
    if not events:
        return []

    sorted_events = sorted(events, key=lambda e: e["timestamp"])
    cleaned: List[Dict[str, Any]] = []
    last_ts: Optional[datetime] = None

    for event in sorted_events:
        ts = event["timestamp"]
        if last_ts is not None and (ts - last_ts).total_seconds() < min_gap_seconds:
            continue
        cleaned.append(event)
        last_ts = ts

    return cleaned


def event_score(event: Dict[str, Any]) -> float:
    """Per-event score: base weight × recency × dwell multiplier."""
    base = EVENT_BASE_WEIGHTS.get(event["type"], 0.5)
    recency = recency_weight(event["days_ago"])
    mult = (
        time_multiplier(event.get("seconds_spent"))
        if event["type"] in VIEW_EVENT_TYPES
        else 1.0
    )
    return base * recency * mult


def compute_category_scores(
    events: Sequence[Dict[str, Any]],
    explicit_interests: Optional[Sequence[str]] = None,
) -> Dict[str, float]:
    """
    Aggregate per-category scores with frequency_boost and explicit-interest boost.
    Skips bounce/dismiss from positive category aggregation (negative handled via weight).
    """
    explicit_set = set(explicit_interests or [])
    raw_scores: Dict[str, float] = {}
    counts: Dict[str, int] = {}

    for event in events:
        if event["type"] in ("bounce",):
            continue
        category = event.get("category")
        if not category:
            continue

        score = event_score(event)
        if event["type"] == "dismiss":
            # Negative signal — still affects category score via negative weight
            pass

        raw_scores[category] = raw_scores.get(category, 0.0) + score
        counts[category] = counts.get(category, 0) + 1

    final_scores: Dict[str, float] = {}
    for category, score in raw_scores.items():
        score *= frequency_boost(counts[category])
        if category in explicit_set:
            score *= EXPLICIT_INTEREST_BOOST
        final_scores[category] = score

    return final_scores


def resolve_retrieval_categories(
    sorted_cats: List[Tuple[str, float]],
    dominance_ratio: float = CATEGORY_DOMINANCE_RATIO,
) -> List[str]:
    """
    If top category dominates (> ratio × second), retrieve from one category only.
    Otherwise blend the top two categories.
    """
    positive = [(c, s) for c, s in sorted_cats if s > 0]
    if not positive:
        return []

    if len(positive) == 1:
        return [positive[0][0]]

    if positive[0][1] > positive[1][1] * dominance_ratio:
        return [positive[0][0]]

    return [positive[0][0], positive[1][0]]


def filter_already_owned(
    candidates: Sequence[Product],
    enrolled_ids: set,
) -> List[Product]:
    """Exclude products the user is already enrolled in."""
    if not enrolled_ids:
        return list(candidates)
    return [c for c in candidates if c.id not in enrolled_ids]


def get_last_shown_product_ids(db: Session, user_id: int) -> List[int]:
    """Product IDs from the user's most recent recommendation row."""
    rec = (
        db.query(Recommendation)
        .filter(Recommendation.user_id == user_id, Recommendation.is_latest == True)  # noqa: E712
        .order_by(Recommendation.created_at.desc())
        .first()
    )
    if not rec or not rec.product_ids:
        return []
    try:
        ids = json.loads(rec.product_ids)
        return [int(i) for i in ids if i is not None]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def diversify(
    candidates: Sequence[Product],
    final_count: int = 3,
    user_id: Optional[int] = None,
    db: Optional[Session] = None,
) -> List[Product]:
    """
    Deprioritize products from the last recommendation; random-sample final_count
    from fresh pool, falling back to full pool when needed.
    """
    if not candidates:
        return []

    last_shown: List[int] = []
    if user_id is not None and db is not None:
        last_shown = get_last_shown_product_ids(db, user_id)

    pool = list(candidates[: final_count + 3])
    fresh = [c for c in pool if c.id not in last_shown]
    chosen_pool = fresh if len(fresh) >= final_count else pool

    sample_size = min(final_count, len(chosen_pool))
    if sample_size <= 0:
        return []
    return random.sample(chosen_pool, sample_size)


def explicit_product_categories(user: User) -> set:
    """Map onboarding interest labels to product category names for EXPLICIT_INTEREST_BOOST."""
    categories: set = set()
    if not user.interests:
        return categories
    for label in user.interests.split(","):
        label = label.strip()
        if not label:
            continue
        mapped = ONBOARDING_TO_PRODUCT_CATEGORY_WEIGHTS.get(label, {})
        categories.update(mapped.keys())
        categories.add(label)
    return categories


def _parse_metadata(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _category_for_event(
    event: Event,
    product_categories: Dict[int, str],
) -> Optional[str]:
    if event.product_id and event.product_id in product_categories:
        return product_categories[event.product_id]
    if event.event_type == "search":
        meta = _parse_metadata(event.event_metadata)
        query = meta.get("query")
        if isinstance(query, str) and query.strip():
            return infer_category_from_query(query)
    return None


def fetch_scoring_events(db: Session, user: User) -> List[Dict[str, Any]]:
    """Load agent-eligible events and normalize for scoring_engine functions."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    rows = (
        db.query(Event)
        .filter(
            Event.user_id == user.id,
            Event.agent_eligible == True,  # noqa: E712
            Event.created_at >= cutoff,
        )
        .order_by(Event.created_at.asc())
        .all()
    )

    product_ids = {e.product_id for e in rows if e.product_id}
    product_categories: Dict[int, str] = {}
    if product_ids:
        for pid, category in (
            db.query(Product.id, Product.category)
            .filter(Product.id.in_(product_ids))
            .all()
        ):
            if category:
                product_categories[pid] = category

    normalized: List[Dict[str, Any]] = []
    for event in rows:
        if not event.created_at:
            continue
        ts = event.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        days_ago = max((now - ts).total_seconds() / 86400.0, 0.0)
        meta = _parse_metadata(event.event_metadata)
        seconds_spent = meta.get("seconds")
        if isinstance(seconds_spent, (int, float)):
            seconds_spent = float(seconds_spent)
        else:
            seconds_spent = None

        category = _category_for_event(event, product_categories)
        normalized.append({
            "type": event.event_type,
            "category": category,
            "days_ago": days_ago,
            "seconds_spent": seconds_spent,
            "timestamp": ts,
            "product_id": event.product_id,
        })

    return normalized


def count_personalization_events(events: Sequence[Dict[str, Any]]) -> int:
    """Count events after bot filtering for cold-start gate."""
    return len(events)


def build_category_profile_for_retrieval(
    db: Session,
    user: User,
    pre_cleaned_events: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """
    Full scoring pipeline: fetch → bot filter → category scores.
    Returns (category_scores, cleaned_events).
    """
    if pre_cleaned_events is not None:
        cleaned = pre_cleaned_events
    else:
        raw = fetch_scoring_events(db, user)
        cleaned = remove_bot_noise(raw)
    explicit = explicit_product_categories(user)
    scores = compute_category_scores(cleaned, explicit_interests=explicit)
    return scores, cleaned


def passes_similarity_threshold(score: float) -> bool:
    return score >= SIMILARITY_THRESHOLD
