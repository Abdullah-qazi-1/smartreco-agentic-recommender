"""

Structured recommendation reasoning for UI rendering.

Builds the reasoning object consumed by Dashboard and AI Insights.

Uses the same scoring_engine pipeline as get_recommendation_candidates().

"""

from typing import Any, Dict, List



from sqlalchemy.orm import Session



from database.models import User

from services.retrieval import get_last_search_query

from services.scoring_engine import (

    build_category_profile_for_retrieval,

    count_personalization_events,

    fetch_scoring_events,

    remove_bot_noise,

    resolve_retrieval_categories,

)

from services.scoring_weights import (

    MIN_EVENTS_FOR_PERSONALIZATION,

    MIN_CATEGORY_SCORE_FOR_TAG,

)



logger_namespace = "smartreco.reasoning"





def _explicit_interest_labels(user: User) -> List[str]:

    if not user.interests:

        return []

    return [t.strip() for t in user.interests.split(",") if t.strip()]





def build_recommendation_reasoning(

    db: Session,

    user: User,

    tracking_enabled: bool = True,

) -> Dict[str, Any]:

    """Returns structured reasoning for UI key-factor cards and match badge."""

    recent_search = get_last_search_query(db, user.id)

    explicit = _explicit_interest_labels(user)



    if not tracking_enabled:

        return {

            "personalized": False,

            "reason": "tracking_disabled",

            "match_score": 65,

            "top_categories": [],

            "recent_search": recent_search,

            "explicit_interests_used": explicit,

            "interest_summary": "AI tracking is off — showing popular catalog picks.",

            "search_summary": "Enable tracking in Settings to incorporate search behavior.",

            "data_processing_pct": 40,

        }



    raw_events = fetch_scoring_events(db, user)

    cleaned_events = remove_bot_noise(raw_events)

    event_count = count_personalization_events(cleaned_events)



    if event_count < MIN_EVENTS_FOR_PERSONALIZATION:

        return {

            "personalized": False,

            "reason": "cold_start",

            "match_score": 72,

            "top_categories": [],

            "recent_search": recent_search,

            "explicit_interests_used": explicit,

            "interest_summary": (

                f"Only {event_count} behavioral signals so far — browse and search "

                f"more to unlock fully personalized picks."

            ),

            "search_summary": (

                f'Recent search: "{recent_search}"' if recent_search

                else "No catalog searches recorded yet."

            ),

            "data_processing_pct": min(95, 40 + event_count * 8),

        }



    category_scores, _ = build_category_profile_for_retrieval(
        db, user, pre_cleaned_events=cleaned_events
    )

    sorted_cats = sorted(category_scores.items(), key=lambda kv: kv[1], reverse=True)

    positive_cats = [(c, round(s, 3)) for c, s in sorted_cats if s > MIN_CATEGORY_SCORE_FOR_TAG]



    retrieval_cats = resolve_retrieval_categories(positive_cats)

    top_categories = positive_cats[:3]

    if len(retrieval_cats) == 1 and top_categories:

        top_categories = [top_categories[0]]

    elif len(retrieval_cats) >= 2:

        top_categories = positive_cats[:2]



    top_score = top_categories[0][1] if top_categories else 0.0

    match_score = min(98, max(65, int(70 + top_score * 4)))



    explicit_used = [

        label for label in explicit

        if any(label.lower() in cat.lower() for cat, _ in top_categories)

    ]



    cat_labels = ", ".join(c for c, _ in top_categories[:2]) if top_categories else "your interests"

    interest_summary = (

        f"Strongest signals in {cat_labels} from dwell time, views, and onboarding interests."

        if top_categories else "Interest profile is still forming from your activity."

    )

    search_summary = (

        f'Latest catalog search: "{recent_search}" — used to refine cross-domain picks.'

        if recent_search

        else "No recent searches — recommendations driven by browsing behavior only."

    )



    return {

        "personalized": True,

        "reason": None,

        "match_score": match_score,

        "top_categories": top_categories,

        "retrieval_categories": retrieval_cats,

        "recent_search": recent_search,

        "explicit_interests_used": explicit_used or explicit[:3],

        "interest_summary": interest_summary,

        "search_summary": search_summary,

        "data_processing_pct": min(98, 60 + event_count * 2),

        "event_count": event_count,

    }

