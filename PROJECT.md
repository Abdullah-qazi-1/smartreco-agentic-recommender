# PROJECT.md — SmartReco Implementation Status

> Evaluator-facing audit of the SmartReco Build Challenge spec.  
> Every status below was verified against the codebase as of Aug 2026.  
> **Accuracy over completeness** — items are not marked done unless the code exists today.

---

## Summary

| Area | Status |
|------|--------|
| Auth & roles | ✅ Mostly complete |
| Product CRUD + dual-write | ✅ Complete |
| Event tracking (debounce/batch) | ✅ Complete |
| Scoring engine (prompt spec) | ✅ Complete |
| RAG retrieval + LLM narrative | ✅ Complete |
| Caching / trigger logic | ✅ Complete |
| UI wiring (dashboard, AI Insights, profile, settings) | ✅ Complete |
| Bonus (LangGraph, scheduler, LangSmith) | ✅ Complete — see §11 |

---

## 1. Authentication & Roles

| Requirement | Status | Proof |
|-------------|--------|-------|
| User signup | ✅ Done | `routers/auth.py` → `signup_submit()` |
| User login (session cookie) | ✅ Done | `routers/auth.py` → `login_submit()`; `main.py` SessionMiddleware |
| Password hashing (bcrypt) | ✅ Done | `routers/auth.py` → `hash_password()`, `verify_password()` |
| Admin role | ✅ Done | `database/models.py` → `User.role`; `create_admin.py` |
| Student / instructor dual mode | ✅ Done | `User.active_mode`; `routers/auth.py` → `switch_mode()` |
| Onboarding interests + experience level | ✅ Done | `routers/auth.py` → `onboarding_interests_page()`, `onboarding_interests_submit()` |
| Protected routes (redirect if logged out) | ✅ Done | `get_current_user()` in `routers/auth.py`; used across routers and `main.py` |
| Instructor course ownership checks | ✅ Done | `routers/products.py` → `_can_manage_course()` |

---

## 2. Product Catalog & Dual-Write (SQL + Vector DB)

| Requirement | Status | Proof |
|-------------|--------|-------|
| List / browse catalog | ✅ Done | `routers/products.py` → `list_products()` |
| Course detail page | ✅ Done | `routers/products.py` → `product_detail()` |
| Create product | ✅ Done | `routers/products.py` → `api_create_product()` → `product_service.create_product()` |
| Update product | ✅ Done | `routers/products.py` → `api_update_product()` → `product_service.update_product()` |
| Delete product | ✅ Done | `routers/products.py` → `api_delete_product()` → `product_service.delete_product()` |
| SQLite write | ✅ Done | SQLAlchemy in `services/product_service.py` |
| Chroma upsert on create/update | ✅ Done | `product_service.create_product()` / `update_product()` → `chroma_client.upsert_product()` |
| Chroma delete on remove | ✅ Done | `product_service.delete_product()` → `chroma_client.delete_product()` |
| Canonical product serializer | ✅ Done | `database/models.py` → `Product.to_dict()` |
| Embeddings via Mesh (mandatory provider) | ✅ Done | `database/chroma_client.py` → `embed_text()` calls Mesh's `/embeddings` endpoint (`MESH_EMBED_MODEL`, default `openai/text-embedding-3-small`) — corrected from a prior version of this table that incorrectly claimed a local `SentenceTransformer` was used; no such code exists in this repo |

---

## 3. Event Tracking (Frontend)

| Requirement | Status | Proof |
|-------------|--------|-------|
| Debounced catalog search (500 ms) | ✅ Done | `static/js/debounce.js` → `debounce()`; `templates/catalog.html` → `debounce(runSearch, 500)` — lowered from 1000ms for a snappier feel; a loading spinner was added next to the search box (`#searchSpinner`) since `/api/search` calls Mesh for semantic re-ranking and can take a couple seconds, which looked like a UI freeze without feedback |
| Enter submits search immediately | ✅ Done | `templates/catalog.html` → keydown Enter calls `debouncedSearch.cancel()` + `runSearch()` |
| AbortController cancels stale search | ✅ Done | `templates/catalog.html` → `searchController.abort()` in `runSearch()` |
| Search event only when search fires | ✅ Done | `templates/catalog.html` → `SmartRecoTracker.track('search', …)` inside `runSearch()` after fetch |
| Client-side event batching (5 s) | ✅ Done | `static/js/tracker.js` → `setInterval(() => flush(false), 5000)` |
| Early flush at 10 events | ✅ Done | `static/js/tracker.js` → `MAX_QUEUE_BEFORE_EARLY_FLUSH = 10` |
| sendBeacon / keepalive on unload | ✅ Done | `static/js/tracker.js` → `visibilitychange` / `pagehide` → `flush(true)` with `navigator.sendBeacon` |
| Re-queue batch on fetch failure | ✅ Done | `static/js/tracker.js` → `.catch(() => queue.unshift(...batch))` |
| Tracker loaded for authenticated users | ✅ Done | `templates/base.html` → loads `tracker.js` when `{% if user %}` |
| Respect tracking toggle on client | ✅ Done | `templates/base.html` → `window.__SMARTRECO_TRACKING_ENABLED__`; checked in `tracker.js` → `trackingEnabled()` |
| `view` events | ✅ Done | `static/js/tracker.js` → page-load `view`; course-details sets `__SMARTRECO_PRODUCT_ID__` (no duplicate fetch) |
| `time_spent` / dwell | ✅ Done | `tracker.js` → `recordTimeSpent()` on tab hide; `templates/course-details.html` sets `window.__SMARTRECO_PRODUCT_ID__` |
| `click` events | ✅ Done | `static/js/tracker.js` delegated `[data-sr-track-click]`; wired in `catalog.html`, `dashboard.html`, `ai-insights.html`, `course-details.html` |
| `enroll` events | ✅ Done | `routers/products.py` → `api_enroll()` inserts `Event(event_type='enroll')` when tracking enabled |
| `dismiss` events | ✅ Done | `static/js/tracker.js` delegated `[data-sr-track-dismiss]`; buttons on `dashboard.html` + `ai-insights.html` |
| Throttle high-frequency sampling (1 Hz) | ⚠️ Partial | `static/js/debounce.js` → `throttle()` helper added; no scroll-depth signal wired yet |

---

## 4. Event Ingestion (Backend)

| Requirement | Status | Proof |
|-------------|--------|-------|
| Bulk POST `/api/events` | ✅ Done | `routers/events.py` → `receive_events()` |
| Alias `/api/track` | ✅ Done | `routers/events.py` → `receive_events_track_alias()` |
| Dumb/fast insert (no sync LLM) | ✅ Done | `bulk_save_objects` + `BackgroundTasks` → `_run_recommendation_check()` |
| Drop events when tracking OFF | ✅ Done | `receive_events()` returns early when `is_agent_tracking_enabled()` is false |
| Stamp `agent_eligible` per event | ✅ Done | `Event(agent_eligible=True)` when tracking on at ingest time |
| Tracking toggle API | ✅ Done | `routers/events.py` → `set_tracking_toggle()` persists to `UserProfile.agent_tracking_enabled` via `services/tracking_prefs.py` |
| `enroll` event type accepted | ✅ Done | `VALID_EVENT_TYPES` includes `enroll` |

---

## 5. Scoring Engine

Spec-aligned scoring lives in **`services/scoring_engine.py`** and is wired into the live recommendation path via **`services/retrieval.py` → `get_recommendation_candidates()`** and **`services/reasoning.py` → `build_recommendation_reasoning()`**. Tunables (`EVENT_BASE_WEIGHTS`, `EXPLICIT_INTEREST_BOOST`, `SIMILARITY_THRESHOLD`, etc.) are in **`services/scoring_weights.py`**.

A parallel legacy path remains in **`services/interest_profile.py`** (reviews, dismissals, category spreading) for catalog sort bias and extended profiling; retrieval/reasoning use the spec scoring engine for category selection.

### 5a. Prompt-spec functions — explicit audit

| Prompt function | Status | Actual location + live call site |
|-----------------|--------|-----------------------------------|
| `recency_weight()` | ✅ Done | `services/scoring_engine.py` → `recency_weight()`; used by `event_score()` → `compute_category_scores()`; `_decay_factor()` in `interest_profile.py` delegates here |
| `time_multiplier()` | ✅ Done | `services/scoring_engine.py` → `time_multiplier()` (spec tiers: &lt;5s→0.2, 5–30→1.0, 30–120→1.5, &gt;120→2.0); used by `event_score()`; `_confidence_weight_for_product()` in `interest_profile.py` delegates here |
| `frequency_boost()` | ✅ Done | `services/scoring_engine.py` → `frequency_boost()`; applied in `compute_category_scores()` |
| `remove_bot_noise()` | ✅ Done | `services/scoring_engine.py` → `remove_bot_noise()`; called in `get_recommendation_candidates()` before scoring |
| `diversify()` | ✅ Done | `services/scoring_engine.py` → `diversify()` + `get_last_shown_product_ids()`; called at end of `get_recommendation_candidates()` |
| `filter_already_owned()` | ✅ Done | `services/scoring_engine.py` → `filter_already_owned()`; called in `get_recommendation_candidates()` |
| Conflicting-interest (3× dominance) | ✅ Done | `services/scoring_engine.py` → `resolve_retrieval_categories()`; called in `get_recommendation_candidates()` and mirrored in `build_recommendation_reasoning()` |
| `EVENT_BASE_WEIGHTS` per event type | ✅ Done | `services/scoring_weights.py`; used by `scoring_engine.event_score()` |
| `EXPLICIT_INTEREST_BOOST` (1.5×) | ✅ Done | `services/scoring_weights.py`; applied in `compute_category_scores()` via `explicit_product_categories()` |
| `SIMILARITY_THRESHOLD` (0.65) | ✅ Done | `services/scoring_weights.py`; enforced in `product_service.semantic_search_products_scored()` → `_level_preferred_search()` in `retrieval.py`; low-confidence fallback via `_cold_start_result(..., low_confidence=True)` when &lt;2 candidates |

### 5b. Supporting pipeline functions

| Function | Status | Proof |
|----------|--------|-------|
| `event_score()` | ✅ Done | `services/scoring_engine.py`; used by `compute_category_scores()` |
| `compute_category_scores()` | ✅ Done | `services/scoring_engine.py`; called via `build_category_profile_for_retrieval()` |
| `fetch_scoring_events()` | ✅ Done | `services/scoring_engine.py`; normalizes DB events for scoring |
| `build_category_profile_for_retrieval()` | ✅ Done | `services/scoring_engine.py`; orchestrates fetch → bot filter → scores |
| Chroma similarity scores | ✅ Done | `database/chroma_client.py` → `semantic_search_with_scores()` |
| MIN_EVENTS cold-start gate | ✅ Done | `get_recommendation_candidates()` checks `count_personalization_events(cleaned) < MIN_EVENTS_FOR_PERSONALIZATION` |
| Legacy interest profile (reviews/spread) | ✅ Done | `services/interest_profile.py` → `build_category_profile()` (catalog sort, extended signals) |

---

## 6. RAG Retrieval

| Requirement | Status | Proof |
|-------------|--------|-------|
| Vector query grounded in catalog | ✅ Done | `product_service.semantic_search_products()` → `chroma_client.semantic_search()` |
| Category-constrained search | ✅ Done | `retrieval.py` → `_level_preferred_search()`, `_semantic_candidates_for_query()` |
| Level preference (soft) | ✅ Done | `_level_preferred_search()` tops up from category-only if level match sparse |
| Exclude already viewed (primary) | ✅ Done | `get_recommendation_candidates()` filters `already_viewed` |
| Exclude already enrolled | ✅ Done | `_enrolled_product_ids()` filter in `get_recommendation_candidates()` |
| Instructor search branch | ✅ Done | `_match_instructor()`, `_instructor_branch()` |
| Cross-field search-intent bridge | ✅ Done | `_search_intent_branch()` |
| Search history (1 pick per past search) | ✅ Done | `build_search_history()` → `{latest, sidebar}` |
| Search result re-ranking | ✅ Done | `_rerank_search_products()`, `_product_profile_overlap_score()` |
| In-memory search cache (30 s) | ✅ Done | `_search_recommendation_cache` in `retrieval.py`; TTL is `CACHE_TTL_SECONDS = 30` in `services/recommendation_cache.py` — corrected from a prior version of this table that said "24 h" |
| Low-confidence vector fallback | ✅ Done | `get_recommendation_candidates()` → `_cold_start_result(..., low_confidence=True)` when &lt;2 products pass similarity filter |
| Diversify / rotate recommendations | ✅ Done | `scoring_engine.diversify()` in `get_recommendation_candidates()` |

---

## 7. LLM Narrative Generation

| Requirement | Status | Proof |
|-------------|--------|-------|
| Mesh-only LLM client | ✅ Done | `services/llm_client.py` → `get_client()` hits `https://api.meshapi.ai/v1` only; no other provider exists in code |
| Catalog-grounded prompt (no invented courses) | ✅ Done | System rules in `generate_narrative()`; products passed as JSON |
| Dual narrative blocks (main + search intent) | ✅ Done | `services/agent.py` → `generate_and_save_recommendation()` |
| Fail-soft on LLM error | ✅ Done | `generate_narrative()` returns generic fallback string |
| Mesh API for submission | ✅ Done | `services/llm_client.py` and `database/chroma_client.py` both call Mesh exclusively for narrative generation AND embeddings; verified live against `api.meshapi.ai` |

> Note: an earlier version of this table incorrectly claimed a "Groq/Mesh provider abstraction" and marked Mesh usage as only "Partial". That was inaccurate — the code has never had a Groq code path; it has been corrected above to match `services/llm_client.py` as it actually exists.

---

## 8. Caching & Trigger Logic

| Requirement | Status | Proof |
|-------------|--------|-------|
| Regenerate only after N new events | ✅ Done | `services/trigger.py` → `should_regenerate()`; threshold `NEW_EVENTS_TRIGGER_THRESHOLD = 5` |
| No LLM on every page view | ✅ Done | Dashboard reads cached rec via `/api/recommendations`; generation gated by trigger |
| Auto-refresh on AI Insights when threshold met | ✅ Done | `routers/recommendations.py` → `ai_insights_page()` calls `should_regenerate()` then `generate_and_save_recommendation()` |
| Force refresh button | ✅ Done | `POST /api/recommendations/refresh`; wired in `templates/ai-insights.html` |
| Background trigger after event batch | ✅ Done | `routers/events.py` → `BackgroundTasks` → `_run_recommendation_check()` |
| Persist recommendation in DB | ✅ Done | `Recommendation` model; `agent.py` saves JSON narrative + product_ids |
| Structured `reasoning` persisted | ✅ Done | `services/agent_graph.py` → `generate()` calls `save_recommendation_explanations()`, writing rows to `recommendation_explanations` table (`database/models.py` → `RecommendationExplanation`) — corrected from a prior version of this table that said "Not implemented" |

---

## 9. UI Wiring

| Screen / element | Status | Proof |
|------------------|--------|-------|
| Dashboard — AI Insight match badge | ✅ Done | `templates/dashboard.html` → fetches `/api/recommendations`, sets `#recMatchBadge` from `reasoning.match_score` |
| Dashboard — category tags | ✅ Done | `#recTags` from `reasoning.top_categories` |
| Dashboard — narrative + link to top product | ✅ Done | `#recNarrative`, `#recStartBtn` from `data.main` / `data.search_intent` |
| Dashboard — Recent Activity | ✅ Done | `main.py` → `get_recent_activity()`; `services/activity.py` formats search/view/click labels |
| AI Insights — Key Factors cards | ✅ Done | `templates/ai-insights.html` renders `reasoning.interest_summary`, `reasoning.search_summary` |
| AI Insights — Tracked Interest Tags | ✅ Done | `reasoning.top_categories` in template |
| AI Insights — Engine Status % | ✅ Done | `reasoning.data_processing_pct` |
| AI Insights — Top 3 from latest search | ✅ Done | `routers/recommendations.py` → `picked_products` from `latest_search["products"][:3]` |
| AI Insights — Search History sidebar | ✅ Done | `sidebar_history` from `build_search_history()` |
| AI Insights — Refresh button | ✅ Done | `fetch('/api/recommendations/refresh')` in `ai-insights.html` |
| Profile — interests display | ✅ Done | `templates/profile.html` → `user.interests` |
| Profile — update interests link | ✅ Done | Link to `/onboarding/interests` |
| Settings — tracking toggle | ✅ Done | `templates/settings.html` → `/api/tracking-toggle`; persisted via `set_agent_tracking_enabled()` |
| Settings OFF → fallback messaging | ✅ Done | `reasoning.py` returns `tracking_disabled`; `agent.py` → `generate_and_save_recommendation()` short-circuits when tracking off |
| Enroll button tracking | ✅ Done | `api_enroll()` creates enroll event; `activity.py` → `Enrolled: {title}` label |
| Dashboard — dismiss recommendation | ✅ Done | `#recDismissBtn` with `data-sr-track-dismiss` in `dashboard.html` |
| AI Insights — dismiss pick cards | ✅ Done | Dismiss buttons on `.sr-pick-card` in `ai-insights.html` |

---

## 10. Edge Cases (Challenge Checklist)

| Edge case | Status | How handled (or not) |
|-----------|--------|----------------------|
| New user / no history | ✅ Done | `get_recommendation_candidates()` → cold-start trending products |
| &lt; MIN_EVENTS personalization | ✅ Done | `get_recommendation_candidates()` and `build_recommendation_reasoning()` gate on `MIN_EVENTS_FOR_PERSONALIZATION` |
| Conflicting interests (similar scores) | ✅ Done | `resolve_retrieval_categories()` returns top 2 categories when not 3× dominated; retrieval loops both |
| One category dominates (3×+) | ✅ Done | `resolve_retrieval_categories()` returns single category |
| Already enrolled filtered | ✅ Done | `filter_already_owned()` in `get_recommendation_candidates()` |
| Same rec shown repeatedly | ✅ Done | `diversify()` deprioritizes last recommendation's product IDs |
| Weak vector matches → fallback | ✅ Done | `semantic_search_products_scored()` + `_cold_start_result(low_confidence=True)` |
| Rapid-fire clicks filtered | ✅ Done | `remove_bot_noise()` in `get_recommendation_candidates()` |
| Recency decay for old interests | ✅ Done | `_decay_factor()` in interest profile |
| Multi-device same user | ✅ Done | Events keyed by `user_id`, not session |
| Tracking disabled | ✅ Done | Events dropped at ingest; client respects `__SMARTRECO_TRACKING_ENABLED__`; DB-persisted toggle; agent skips generation |
| Search debounce + cancel in-flight | ✅ Done | `catalog.html` + `debounce.js` |

---

## 11. Bonus Features

| Feature | Status | Proof |
|---------|--------|-------|
| LangGraph agent workflow | ✅ Done | `services/agent_graph.py` → `StateGraph` pipeline (`analyze` → `decide` → `retrieve` → `evaluate` → `refine` → `generate`) |
| APScheduler / daily digest job | ✅ Done | `services/scheduler.py` → `BackgroundScheduler` running daily digest job on startup |
| Email digest | ✅ Done | `services/scheduler.py` → `send_email_digest()` via SMTP credentials |
| Telegram digest | ✅ Done | `services/scheduler.py` → `send_telegram_digest()` via Telegram Bot HTTP API |
| LangSmith tracing | ✅ Done | `services/llm_client.py` → `@traceable` decorator & `.env.example` tracing config |
| Retrieval re-ranking (search & main) | ✅ Done | `services/retrieval.py` → `_rerank_search_products()` applied to search & main recommendation candidate sets |
| Recommendation conversion analytics | ✅ Done | `services/analytics.py`; enrollment marks `Recommendation.converted` in `api_enroll()` |
| Operational metrics | ✅ Done | `services/metrics.py`; `/metrics` endpoint |
| Recommendation Quality Hardening | ✅ Done | Stricter secondary similarity threshold delta fallback, hard level filtering on high event confidence, price-sensitivity heuristic, LLM title grounding check |


---

## 12. API Deliverable Checklist

| Endpoint | Status | Handler |
|----------|--------|---------|
| POST `/api/track` (bulk events) | ✅ Done | `routers/events.py` |
| GET `/api/search` | ✅ Done | `routers/products.py` → `search_products()` |
| GET `/api/recommendations` | ✅ Done | `routers/recommendations.py` → `get_recommendations()` |
| POST `/api/recommendations/refresh` | ✅ Done | `force_refresh_recommendation()` |
| Dual-write on product CRUD | ✅ Done | `services/product_service.py` |
| UI from real data | ✅ Done | Dashboard, AI Insights, activity feed, enroll/dismiss/click tracking wired |
| Admin analytics auth | ✅ Done | `routers/monitoring.py` → `admin_analytics()` requires real `role=="admin"` only — corrected here to match the current code and the README Security section; this row previously said "admin or instructor mode," which described the *pre-hardening* behavior and had gone stale after the privilege-escalation fix landed |

---

## Known Limitations / TODOs

1. **Dual profiling paths** — `scoring_engine.py` drives retrieval/reasoning; `interest_profile.py` still handles reviews, dismissals, and catalog sort bias separately. Intentional split, not a bug: `scoring_engine.py` owns the recommendation-affecting interest score, `interest_profile.py` owns lighter-weight UI concerns (catalog sort bias, review/dismissal bookkeeping) that don't need the full recency/decay math. Not consolidated because merging them would couple UI sort order to the same cooldown/threshold logic that gates LLM calls — not worth the coupling for a cosmetic feature.
2. **Scroll-depth events — fixed in this audit pass.** `static/js/tracker.js` throttles scroll and pushes `scroll_depth` events client-side; `routers/events.py` → `VALID_EVENT_TYPES` previously did not include `"scroll_depth"`, so these were silently discarded on ingest. This was a real, verified data-loss bug (confirmed by reading `tracker.js` against the old `VALID_EVENT_TYPES` set). Fixed directly in this pass — `scroll_depth` is now accepted and persisted. Remaining, still-open gap: it is not yet added to `services/scoring_weights.EVENT_BASE_WEIGHTS`, so it's stored but doesn't yet affect category scoring — that requires a product decision on its weight, not just a bug fix, so it's left as a deliberate follow-up.
3. **Reasoning IS persisted** — corrected from a prior version of this table that claimed otherwise. `services/agent_graph.py` → `generate()` calls `save_recommendation_explanations()`, which writes rows to the `recommendation_explanations` table (`database/models.py` → `RecommendationExplanation`, linked via `Recommendation.explanations`). Confirmed by reading the code path end-to-end; the earlier claim was stale documentation, not a redaction of a real gap.
4. **Level 6 bonuses ARE implemented** — corrected from a prior version of this table that claimed "not started." LangGraph (`services/agent_graph.py`, real `StateGraph` with 6 nodes), APScheduler daily digest + hourly vector reconcile (`services/scheduler.py`, started at boot in `main.py`), and LangSmith tracing (`@traceable` in `services/llm_client.py`) are all present and wired — see §11 for file-level proof. This summary table previously lagged behind §11 and the actual code; §11 was always correct.
5. **LangSmith tracing wiring not independently confirmed firing** — the `@traceable` decorator is correctly applied and gracefully no-ops if `LANGCHAIN_API_KEY`/`LANGCHAIN_TRACING_V2` aren't set (see `services/llm_client.py` import fallback), but nobody has yet run this with a live LangSmith key and confirmed a trace lands in the `smith.langchain.com` project dashboard. To close this gap: set `LANGCHAIN_TRACING_V2=true` + a real `LANGCHAIN_API_KEY` in `.env`, trigger a recommendation refresh, and screenshot the resulting trace in the `smartreco` project — see the Testing section below.
6. **Cache TTL — docs previously said 24h, code says 30s** — `services/recommendation_cache.py` → `CACHE_TTL_SECONDS = 30`. Corrected here; the README's "24h TTL" claim was wrong and has been fixed to match the code (see README Features section). 30s is intentional for a demo/hackathon context (cache invalidates fast enough to see fresh behavior while testing) — if this ships for real use, revisit the constant based on actual traffic patterns.
7. **Force refresh when tracking off** — Returns 403 `tracking_disabled` from `/api/recommendations/refresh`. Verified in code (`routers/recommendations.py` → `force_refresh_recommendation()`).
8. **Course-ownership check — tightened twice in this audit pass (new finding, not previously listed).** `routers/products.py` → `_can_manage_course()` originally matched `product.instructor_name` against the logged-in user's self-reported `user.name` using Python's `in` operator — a substring/containment check, not equality. Any user could sign up with a display name containing an existing instructor's name (e.g. "Andrew Ng Fan Page" contains "andrew ng") and pass the check for that instructor's real courses. **Live-exploited during this audit**: a spoofed account successfully overwrote another instructor's course title via `PUT /api/products/{id}` (200 OK, title changed in the DB). First fix used exact name equality instead of substring — closed the trivial exploit but was still theoretically spoofable by an exact-name collision, since `user.name` is self-reported. **Final fix**: the name-matching fallback has been removed entirely. Ownership is now decided ONLY by `product.instructor_id == user.id`, the real foreign key. Seed/demo catalog rows (created before real user accounts existed, `instructor_id IS NULL`, display-only `instructor_name` like "Andrew Ng") can now be edited or deleted **only by an admin** — no student or instructor account can claim them by name, exact or otherwise. Courses created through the app always get `instructor_id=user.id` stamped at creation (`api_create_product()`), so real instructors are unaffected and their own courses remain fully theirs. Re-verified live post-fix and via `tests/smoke_test.py` (25/25 still pass) — this was a pure permission-logic change; no product rows, embeddings, or Chroma vectors were touched or re-generated.

---

## Scoring Engine — Submission Confirmation

| Item | Verdict | File / function | Live call site |
|------|---------|-----------------|----------------|
| `recency_weight()` | ✅ Done | `services/scoring_engine.py` → `recency_weight()` | `event_score()` → `compute_category_scores()` → `get_recommendation_candidates()` |
| `time_multiplier()` | ✅ Done | `services/scoring_engine.py` → `time_multiplier()` | `event_score()`; also `_confidence_weight_for_product()` in `interest_profile.py` |
| `frequency_boost()` | ✅ Done | `services/scoring_engine.py` → `frequency_boost()` | `compute_category_scores()` |
| `remove_bot_noise()` | ✅ Done | `services/scoring_engine.py` → `remove_bot_noise()` | `get_recommendation_candidates()` |
| `diversify()` | ✅ Done | `services/scoring_engine.py` → `diversify()` | `get_recommendation_candidates()` |
| `filter_already_owned()` | ✅ Done | `services/scoring_engine.py` → `filter_already_owned()` | `get_recommendation_candidates()` |
| Conflicting-interest 3× rule | ✅ Done | `services/scoring_engine.py` → `resolve_retrieval_categories()` | `get_recommendation_candidates()` + `build_recommendation_reasoning()` |
| `SIMILARITY_THRESHOLD` | ✅ Done | `product_service.semantic_search_products_scored()` | `_level_preferred_search()` in `retrieval.py` |
| `EXPLICIT_INTEREST_BOOST` | ✅ Done | `compute_category_scores()` | `build_category_profile_for_retrieval()` |
| `EVENT_BASE_WEIGHTS` | ✅ Done | `event_score()` | `compute_category_scores()` |