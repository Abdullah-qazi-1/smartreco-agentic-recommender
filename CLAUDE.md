# SmartReco — Developer & Project Context (CLAUDE.md)

SmartReco is a behavioral AI recommendation platform built with FastAPI, SQLite, ChromaDB, and Groq/Mesh LLM.

## Project Status

- ✅ **Level 1 — Core Authentication & Catalog** Complete
- ✅ **Level 2 — Admin CRUD & SQLite/Chroma Dual-Write** Complete
- ✅ **Level 3 — Behavioral Event Tracking & Flush Pipeline** Complete
- ✅ **Level 4 — AI Recommendation Agent, Profiling & Retrieval** Complete
- ✅ **Search Debounce Optimization** Complete (450ms debounce with AbortController)
- ✅ **Pre-Level 5 Production Cleanup & Refactoring** Complete

---

## Architectural Principles & Conventions

1. **Jinja2 Shared Base Template (`templates/base.html`)**
   - Standard navigation bar, profile menu dropdown, fonts, and global CSS are centralized in `base.html`.
   - Page templates (`products.html`, `dashboard.html`, `admin.html`, `product_detail.html`, `onboarding_interests.html`, `login.html`) extend `base.html`.

2. **Dual-Write Consistency**
   - All catalog write operations (`create_product`, `update_product`, `delete_product`) write to both SQLite and ChromaDB synchronously via `services/product_service.py` to prevent vector index drift.

3. **Canonical Product Serialization**
   - Use `Product.to_dict()` defined in `database/models.py` as the single canonical serializer for Product models.

4. **Configurable Tuning & Centralized Constants**
   - Scoring weights, decay half-life (`DECAY_HALF_LIFE_DAYS`), lookback window (`EVENT_LOOKBACK_DAYS`), and retrieval thresholds live in `services/scoring_weights.py`.

5. **LLM Provider Abstraction**
   - `services/llm_client.py` handles provider switching between Groq (dev) and Mesh (prod) via the `LLM_PROVIDER` environment variable.

6. **Trigger-Gated Recommendation Generation**
   - Recommendations regenerate only when `should_regenerate(db, user)` evaluates to `True` (minimum 5 new agent-eligible signal events since last recommendation), avoiding expensive LLM calls on every request.

7. **Error Boundaries & Logging**
   - Application boundaries (FastAPI event handlers, LLM calls) catch exceptions explicitly and log details via Python's standard `logging` module (`smartreco` logger namespace).

---

## Development Workflow & Verification Commands

- **Run Dev Server:**
  ```bash
  uvicorn main:app --reload
  ```
- **Run Event & Recommendation Pipeline Test:**
  ```bash
  py check_events.py
  ```
- **Test LLM Connectivity:**
  ```bash
  py test_api.py
  ```
- **Run Test Suite:**
  ```bash
  py -m pytest
  ```
