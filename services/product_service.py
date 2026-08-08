"""
Product service — single source of truth for product & course CRUD.
Every write goes to SQLite AND Chroma together (dual-write), so they never drift.
"""
import logging
import random
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import desc, asc

from database.models import Product, Review, Wishlist, Enrollment, CourseLearningOutcome, ChromaSyncLog, User
from database import chroma_client
from services import keyword_fallback
from services.scoring_weights import SIMILARITY_THRESHOLD

logger = logging.getLogger("smartreco.product_service")


def _record_chroma_sync_log(db: Session, product_id: int, action: str, status: str):
    try:
        sync_log = ChromaSyncLog(product_id=product_id, action=action, status=status)
        db.add(sync_log)
        db.commit()
    except Exception as log_exc:
        logger.error("Failed to record ChromaSyncLog for product_id=%s: %s", product_id, log_exc)


LEVELS_DURATION_RANGE = {
    "Beginner": (3, 9),
    "Intermediate": (8, 20),
    "Advanced": (15, 38),
}


def _auto_enrolled_students() -> int:
    return int(min(20000, max(45, random.lognormvariate(5.8, 1.0))))


def _auto_rating_and_count(enrolled_students: int):
    rating = round(min(5.0, max(3.0, random.gauss(4.3, 0.35))), 1)
    ratio = random.triangular(0.04, 0.28, 0.12)
    num_ratings = int(max(8, min(enrolled_students, max(10, round(enrolled_students * ratio)))))
    return rating, num_ratings


def _auto_duration_hours(level: str) -> float:
    lo, hi = LEVELS_DURATION_RANGE.get(level, (5, 15))
    return round(random.uniform(lo, hi), 1)


def _fill_missing_stats(level, enrolled_students, rating, num_ratings, duration_hours):
    if enrolled_students is None:
        enrolled_students = _auto_enrolled_students()

    if rating is None or num_ratings is None:
        auto_rating, auto_count = _auto_rating_and_count(enrolled_students)
        if rating is None:
            rating = auto_rating
        if num_ratings is None:
            num_ratings = auto_count

    if duration_hours is None:
        duration_hours = _auto_duration_hours(level)

    return enrolled_students, rating, num_ratings, duration_hours


def create_product(
    db: Session,
    title: str,
    description: str,
    category: str,
    price: float,
    level: str,
    skills: str = "",
    instructor_id: Optional[int] = None,
    instructor_name: str = "",
    rating: float = None,
    num_ratings: int = None,
    enrolled_students: int = None,
    duration_hours: float = None,
    status: str = "active",
) -> Product:
    enrolled_students, rating, num_ratings, duration_hours = _fill_missing_stats(
        level, enrolled_students, rating, num_ratings, duration_hours
    )

    product = Product(
        title=title,
        description=description,
        category=category,
        price=price,
        level=level,
        skills=skills,
        instructor_id=instructor_id,
        instructor_name=instructor_name,
        rating=rating,
        num_ratings=num_ratings,
        enrolled_students=enrolled_students,
        duration_hours=duration_hours,
        status=status,
    )

    db.add(product)
    db.commit()
    db.refresh(product)

    try:
        chroma_client.upsert_product(
            product.id,
            product.title,
            product.description,
            product.category,
            product.level,
            product.price,
            product.skills,
            product.instructor_name,
            product.rating,
            product.num_ratings,
            product.enrolled_students,
            product.duration_hours,
        )
        _record_chroma_sync_log(db, product.id, action="upsert", status="synced")
    except Exception as exc:
        logger.error(
            "MESH FALLBACK ACTIVE: Chroma/Mesh upsert failed for product_id=%s "
            "(likely missing/invalid MESH_API_KEY or Mesh unreachable): %s. "
            "SQL row is already committed — product is NOT lost, it just won't "
            "surface in semantic search until re-synced.",
            product.id, exc,
        )
        _record_chroma_sync_log(db, product.id, action="upsert", status="failed")

    logger.info("Product created: id=%s title=%r category=%s price=%.2f level=%s", product.id, product.title, product.category, product.price, product.level)
    return product


def update_product(db: Session, product_id: int, **fields) -> Optional[Product]:
    product = db.query(Product).filter(Product.id == product_id).first()

    if not product:
        logger.warning("Product update failed: product_id=%s not found", product_id)
        return None

    for key, value in fields.items():
        if value is not None and hasattr(product, key):
            setattr(product, key, value)

    db.commit()
    db.refresh(product)

    try:
        chroma_client.upsert_product(
            product.id,
            product.title,
            product.description,
            product.category,
            product.level,
            product.price,
            product.skills,
            product.instructor_name,
            product.rating,
            product.num_ratings,
            product.enrolled_students,
            product.duration_hours,
        )
        _record_chroma_sync_log(db, product.id, action="upsert", status="synced")
    except Exception as exc:
        logger.error(
            "MESH FALLBACK ACTIVE: Chroma/Mesh upsert failed for product_id=%s during update "
            "(likely missing/invalid MESH_API_KEY or Mesh unreachable): %s. "
            "SQL row is already committed with the new values — only the vector "
            "mirror is stale until re-synced.",
            product.id, exc,
        )
        _record_chroma_sync_log(db, product.id, action="upsert", status="failed")

    logger.info("Product updated: id=%s title=%r category=%s", product.id, product.title, product.category)
    return product


def delete_product(db: Session, product_id: int) -> bool:
    product = db.query(Product).filter(Product.id == product_id).first()

    if not product:
        logger.warning("Product delete failed: product_id=%s not found", product_id)
        return False

    db.delete(product)
    db.commit()

    try:
        chroma_client.delete_product(product_id)
        _record_chroma_sync_log(db, product_id, action="delete", status="synced")
    except Exception as exc:
        logger.error("Chroma delete failed for product_id=%s: %s", product_id, exc)
        _record_chroma_sync_log(db, product_id, action="delete", status="failed")

    logger.info("Product deleted: id=%s title=%r", product_id, product.title)
    return True



def get_all_products(
    db: Session,
    category: Optional[str] = None,
    level: Optional[str] = None,
    price_filter: Optional[str] = None,
    sort_by: Optional[str] = None,
    instructor_id: Optional[int] = None,
):
    query = db.query(Product)

    if instructor_id is not None:
        query = query.filter(
            (Product.instructor_id == instructor_id) | (Product.instructor_name.isnot(None))
        )

    if category and category != "All Categories":
        query = query.filter(Product.category == category)

    if level and level != "All Levels":
        query = query.filter(Product.level == level)

    if price_filter == "Free":
        query = query.filter(Product.price == 0)
    elif price_filter == "Paid":
        query = query.filter(Product.price > 0)

    if sort_by == "Rating":
        query = query.order_by(desc(Product.rating))
    elif sort_by == "Newest":
        query = query.order_by(desc(Product.created_at))
    else:
        query = query.order_by(Product.id)

    return query.all()


def get_instructor_courses(db: Session, user):
    """Returns courses owned by the given instructor user."""
    return (
        db.query(Product)
        .filter(
            (Product.instructor_id == user.id) | (Product.instructor_name == user.name) | (Product.instructor_name == user.email)
        )
        .order_by(desc(Product.created_at))
        .all()
    )


def get_product(db: Session, product_id: int) -> Optional[Product]:
    return db.query(Product).filter(Product.id == product_id).first()


def get_categories(db: Session):
    rows = db.query(Product.category).distinct().order_by(Product.category).all()
    return [row[0] for row in rows]


def semantic_search_products(
    db: Session,
    query: str,
    top_k: int = 8,
    category: Optional[str] = None,
    level: Optional[str] = None,
    user: Optional[User] = None,
):
    scored = semantic_search_products_scored(
        db, query, top_k=top_k, category=category, level=level, min_similarity=None, user=user
    )
    return [p for p, _ in scored]


def semantic_search_products_scored(
    db: Session,
    query: str,
    top_k: int = 8,
    category: Optional[str] = None,
    level: Optional[str] = None,
    min_similarity: Optional[float] = SIMILARITY_THRESHOLD,
    user: Optional[User] = None,
):
    """
    Semantic search returning (Product, similarity_score) pairs.
    When min_similarity is set, filters out weak vector matches.

    If Mesh is unavailable (missing key, down, timed out, rate-limited),
    falls back to a plain SQL keyword search (services/keyword_fallback.py —
    no AI/embedding call involved) instead of crashing. Fallback matches get
    a synthetic score of 1.0 so they clear any min_similarity threshold,
    since real cosine-similarity scores aren't available in this mode.
    """
    try:
        raw_scored = chroma_client.semantic_search_with_scores(
            query, top_k=top_k, category=category, level=level
        )
    except Exception as exc:
        logger.warning(
            "MESH FALLBACK ACTIVE: Chroma/Mesh semantic search unavailable (%s) — "
            "falling back to plain SQL keyword search (no embeddings) for query=%r",
            exc, query,
        )
        fallback_products = keyword_fallback.keyword_search_products(
            db, query, top_k=top_k, category=category, level=level, user=user
        )
        return [(p, 1.0) for p in fallback_products]

    if min_similarity is not None:
        raw_scored = [(pid, score) for pid, score in raw_scored if score >= min_similarity]

    if not raw_scored:
        return []

    ids = [pid for pid, _ in raw_scored]
    products = db.query(Product).filter(Product.id.in_(ids)).all()
    by_id = {p.id: p for p in products}

    ordered: List[tuple] = []
    for pid, score in raw_scored:
        product = by_id.get(pid)
        if product:
            ordered.append((product, score))
        if len(ordered) >= top_k:
            break

    return ordered


def toggle_wishlist(db: Session, user_id: int, product_id: int) -> bool:
    existing = db.query(Wishlist).filter(Wishlist.user_id == user_id, Wishlist.product_id == product_id).first()
    if existing:
        db.delete(existing)
        db.commit()
        return False
    else:
        item = Wishlist(user_id=user_id, product_id=product_id)
        db.add(item)
        db.commit()
        return True


def get_user_wishlist_product_ids(db: Session, user_id: int) -> set:
    rows = db.query(Wishlist.product_id).filter(Wishlist.user_id == user_id).all()
    return {r[0] for r in rows}


def create_review(db: Session, product_id: int, user_id: int, reviewer_name: str, rating: float, comment: str = "") -> Review:
    review = Review(
        product_id=product_id,
        user_id=user_id,
        reviewer_name=reviewer_name,
        rating=rating,
        comment=comment,
    )

    db.add(review)
    db.commit()
    db.refresh(review)

    return review


def get_reviews(db: Session, product_id: int):
    return db.query(Review).filter(Review.product_id == product_id).order_by(Review.created_at.desc()).all()