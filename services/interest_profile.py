"""
Level 4.3 — Interest Profile Builder. (Level 4.4: weights centralized,
negative signals added — see CLAUDE.md item 2.)

Builds a per-user, per-category interest score combining:
  1. Explicit onboarding interests.
  2. Behavioral signals (dwell-time confidence + recency decay +
     search-alignment boost). 🆕 Quick-close (<5s) now applies a small
     NEGATIVE weight instead of being discarded — a fast close is a
     real "not interested" signal, not silence.
  3. 🆕 Review-based signal: the user's own 4-5★ reviews boost that
     category; 1-2★ reviews penalize it.
  4. 🆕 Explicit "Not Interested" dismiss events apply a strong
     negative weight to that product's category.
  5. Category-relatedness spreading (unchanged).
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from database.models import User, Event, Product, Review
from services.category_taxonomy import (
    ONBOARDING_TO_PRODUCT_CATEGORY_WEIGHTS,
    related_weight,
    infer_category_from_query,
    get_related_categories,
)
from services.scoring_weights import (
    NO_DWELL_DATA_WEIGHT,
    SEARCH_ALIGNED_BOOST_WEIGHT,
    SEARCH_ALIGNMENT_WINDOW_MINUTES,
    ALIGNMENT_RELATEDNESS_THRESHOLD,
    EXPLICIT_INTEREST_WEIGHT,
    SPREAD_FACTOR,
    EVENT_LOOKBACK_DAYS,
    REVIEW_POSITIVE_RATING_CUTOFF,
    REVIEW_NEGATIVE_RATING_CUTOFF,
    REVIEW_POSITIVE_WEIGHT,
    REVIEW_NEGATIVE_WEIGHT,
    DISMISS_WEIGHT,
)
from services.scoring_engine import recency_weight, time_multiplier

PRODUCT_LINKED_EVENT_TYPES = {"view", "time_spent", "click"}


def _decay_factor(event_time: datetime, now: datetime) -> float:
    """Alias for scoring_engine.recency_weight (keeps 0.01 floor used by profile builder)."""
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    days_elapsed = max((now - event_time).total_seconds() / 86400.0, 0.0)
    return max(recency_weight(days_elapsed), 0.01)


def _dwell_seconds_for_event(event: Event) -> Optional[float]:
    if not event.event_metadata:
        return None
    try:
        meta = json.loads(event.event_metadata)
    except (TypeError, ValueError):
        return None
    seconds = meta.get("seconds")
    return float(seconds) if isinstance(seconds, (int, float)) else None


def _search_query_for_event(event: Event) -> Optional[str]:
    if not event.event_metadata:
        return None
    try:
        meta = json.loads(event.event_metadata)
    except (TypeError, ValueError):
        return None
    return meta.get("query")


def _is_search_aligned(
    product_events: List[Event],
    search_events: List[Event],
    product_category: str,
) -> bool:
    if not search_events:
        return False

    product_times = [e.created_at for e in product_events if e.created_at]
    if not product_times:
        return False

    window = timedelta(minutes=SEARCH_ALIGNMENT_WINDOW_MINUTES)

    for search_event in search_events:
        if not search_event.created_at:
            continue
        query = _search_query_for_event(search_event)
        if not query:
            continue

        close_enough = any(
            abs((search_event.created_at - pt).total_seconds()) <= window.total_seconds()
            for pt in product_times
        )
        if not close_enough:
            continue

        inferred_category = infer_category_from_query(query)
        if not inferred_category:
            continue

        if related_weight(inferred_category, product_category) >= ALIGNMENT_RELATEDNESS_THRESHOLD:
            return True

    return False


def _confidence_weight_for_product(
    product_events: List[Event],
    search_events: List[Event],
    product_category: str,
) -> float:
    """
    Dwell confidence using scoring_engine.time_multiplier() tiers.
    Repeat visits across days and search-alignment boosts are preserved.
    """
    distinct_days = {e.created_at.date() for e in product_events if e.created_at}
    if len(distinct_days) >= 2:
        return 1.0

    time_spent_events = [e for e in product_events if e.event_type == "time_spent"]
    best_seconds = None
    if time_spent_events:
        best_seconds = max(
            (s for s in (_dwell_seconds_for_event(e) for e in time_spent_events) if s is not None),
            default=None,
        )

    if best_seconds is not None:
        dwell_weight = time_multiplier(best_seconds)
    else:
        dwell_weight = NO_DWELL_DATA_WEIGHT * time_multiplier(None)

    if _is_search_aligned(product_events, search_events, product_category):
        return max(dwell_weight, SEARCH_ALIGNED_BOOST_WEIGHT)

    return dwell_weight


def _spread_related(raw_scores: Dict[str, float]) -> Dict[str, float]:
    spread: Dict[str, float] = defaultdict(float)
    for category, score in raw_scores.items():
        spread[category] += score
        for other_category, relatedness in get_related_categories(category).items():
            spread[other_category] += score * relatedness * SPREAD_FACTOR
    return dict(spread)


def _apply_review_signals(db: Session, user: User, raw_scores: Dict[str, float], now: datetime) -> None:
    """
    🆕 Level 4.4 item 2 — the user's own reviews shape their profile:
    4-5★ boosts that category, 1-2★ penalizes it. 3★ is neutral
    (no strong signal either way) and is intentionally skipped.
    """
    reviews = (
        db.query(Review.rating, Review.created_at, Product.category)
        .join(Product, Review.product_id == Product.id)
        .filter(Review.user_id == user.id)
        .all()
    )

    for rating, created_at, category in reviews:
        if rating is None or category is None:
            continue

        decay = _decay_factor(created_at, now) if created_at else 1.0

        if rating >= REVIEW_POSITIVE_RATING_CUTOFF:
            raw_scores[category] += REVIEW_POSITIVE_WEIGHT * decay
        elif rating <= REVIEW_NEGATIVE_RATING_CUTOFF:
            raw_scores[category] += REVIEW_NEGATIVE_WEIGHT * decay
        # 3★ (neutral) intentionally not scored either way


def _apply_dismiss_signals(db: Session, user: User, raw_scores: Dict[str, float], now: datetime) -> None:
    """
    🆕 Level 4.4 item 2 — "Not Interested" dismiss events (event_type
    "dismiss", stamped via the same agent_eligible pipeline as every
    other event) apply a strong negative weight to that product's
    category. Uses the EXISTING Event table — no new table needed.
    """
    dismiss_events = (
        db.query(Event.product_id, Event.created_at)
        .filter(
            Event.user_id == user.id,
            Event.agent_eligible == True,  # noqa: E712
            Event.event_type == "dismiss",
            Event.product_id.isnot(None),
        )
        .all()
    )
    if not dismiss_events:
        return

    product_ids = {pid for pid, _ in dismiss_events}
    products = (
        db.query(Product.id, Product.category)
        .filter(Product.id.in_(product_ids))
        .all()
    )
    category_by_product = {pid: category for pid, category in products}

    for product_id, created_at in dismiss_events:
        category = category_by_product.get(product_id)
        if not category:
            continue
        decay = _decay_factor(created_at, now) if created_at else 1.0
        raw_scores[category] += DISMISS_WEIGHT * decay


def build_category_profile(db: Session, user: User) -> Dict[str, float]:
    now = datetime.now(timezone.utc)
    raw_scores: Dict[str, float] = defaultdict(float)

    # --- Explicit onboarding signal ---
    if user.interests and user.interests_updated_at:
        decay = _decay_factor(user.interests_updated_at, now)
        for onboarding_label in user.interests.split(","):
            onboarding_label = onboarding_label.strip()
            mapped = ONBOARDING_TO_PRODUCT_CATEGORY_WEIGHTS.get(onboarding_label, {})
            for product_category, weight in mapped.items():
                raw_scores[product_category] += EXPLICIT_INTEREST_WEIGHT * weight * decay

    # --- Behavioral signal ---
    lookback_cutoff = now - timedelta(days=EVENT_LOOKBACK_DAYS)
    all_events = (
        db.query(Event)
        .filter(
            Event.user_id == user.id,
            Event.agent_eligible == True,  # noqa: E712
            Event.created_at >= lookback_cutoff,
        )
        .all()
    )

    product_events_raw = [e for e in all_events if e.event_type in PRODUCT_LINKED_EVENT_TYPES and e.product_id]
    search_events = [e for e in all_events if e.event_type == "search"]

    if product_events_raw:
        events_by_product: Dict[int, List[Event]] = defaultdict(list)
        for e in product_events_raw:
            events_by_product[e.product_id].append(e)

        products = (
            db.query(Product.id, Product.category)
            .filter(Product.id.in_(events_by_product.keys()))
            .all()
        )
        category_by_product = {pid: category for pid, category in products}

        for product_id, product_events in events_by_product.items():
            category = category_by_product.get(product_id)
            if not category:
                continue

            confidence_weight = _confidence_weight_for_product(product_events, search_events, category)
            if confidence_weight == 0:
                continue  # 🆕 only truly-neutral (0) is discarded now; negatives pass through

            most_recent = max((e.created_at for e in product_events if e.created_at), default=now)
            decay = _decay_factor(most_recent, now)

            raw_scores[category] += confidence_weight * decay

    # --- 🆕 Negative signals: reviews + dismissals ---
    _apply_review_signals(db, user, raw_scores, now)
    _apply_dismiss_signals(db, user, raw_scores, now)

    return _spread_related(dict(raw_scores))


def get_dominant_categories(db: Session, user: User, top_n: int = 2) -> List[str]:
    profile = build_category_profile(db, user)
    if not profile:
        return []
    ranked = sorted(profile.items(), key=lambda kv: kv[1], reverse=True)
    # 🆕 filter out categories that went net-negative — a category the
    # user has actively signaled AGAINST shouldn't be "dominant" even
    # if it's the least-bad option among all-negative scores.
    positive_ranked = [(c, s) for c, s in ranked if s > 0]
    return [category for category, _score in positive_ranked[:top_n]]