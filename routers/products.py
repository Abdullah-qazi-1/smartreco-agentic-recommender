import json
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import Product, Enrollment, Wishlist, User, Event, Recommendation
from routers.auth import get_current_user
from services import product_service
from services.interest_profile import get_dominant_categories
from services.tracking_prefs import is_agent_tracking_enabled

logger = logging.getLogger("smartreco.products")
router = APIRouter()
templates = Jinja2Templates(directory="templates")



@router.get("/catalog", response_class=HTMLResponse)
@router.get("/products", response_class=HTMLResponse)
def list_products(
    request: Request,
    category: Optional[str] = None,
    level: Optional[str] = None,
    price: Optional[str] = None,
    sort: Optional[str] = None,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    products = product_service.get_all_products(
        db, category=category, level=level, price_filter=price, sort_by=sort
    )
    categories = product_service.get_categories(db)
    wishlist_ids = product_service.get_user_wishlist_product_ids(db, user.id)

    if category is None or category == "All Categories":
        dominant = set(get_dominant_categories(db, user, top_n=2))
        if dominant:
            products = sorted(
                products,
                key=lambda p: 0 if p.category in dominant else 1,
            )

    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "user": user,
            "active_page": "catalog",
            "products": products,
            "categories": categories,
            "active_category": category or "All Categories",
            "active_level": level or "All Levels",
            "active_price": price or "Price: All",
            "active_sort": sort or "Sort: Recommended",
            "wishlist_ids": wishlist_ids,
        },
    )


@router.get("/course-details", response_class=HTMLResponse)
@router.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, product_id: Optional[int] = None, id: Optional[int] = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    target_id = product_id or id
    if not target_id:
        first_product = db.query(Product).order_by(Product.id).first()
        target_id = first_product.id if first_product else 1

    product = product_service.get_product(db, target_id)
    if not product:
        return RedirectResponse("/catalog", status_code=302)

    related = product_service.semantic_search_products(db, product.category, top_k=3)
    related = [r for r in related if r.id != product.id]

    wishlist_ids = product_service.get_user_wishlist_product_ids(db, user.id)
    is_saved = product.id in wishlist_ids

    enrolled = db.query(Enrollment).filter(
        Enrollment.user_id == user.id, Enrollment.product_id == product.id
    ).first()

    return templates.TemplateResponse(
        request,
        "course-details.html",
        {
            "user": user,
            "active_page": "catalog",
            "product": product,
            "related_products": related,
            "is_saved": is_saved,
            "is_enrolled": bool(enrolled),
        },
    )


@router.get("/api/search")
def search_products(q: str = "", request: Request = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not q.strip():
        products = product_service.get_all_products(db)
    else:
        products = product_service.semantic_search_products(db, q, user=user)

    return {"results": [p.to_dict() for p in products]}


@router.post("/api/wishlist/toggle")
def toggle_wishlist_route(
    request: Request,
    product_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    is_added = product_service.toggle_wishlist(db, user.id, product_id)
    return {"saved": is_added, "product_id": product_id}


# ---------------- Instructor / Creator Management ----------------

def _can_manage_course(user: User, product: Product) -> bool:
    if not user or not product:
        return False
    if user.role == "admin":
        return True
    if getattr(user, "active_mode", "student") == "instructor":
        if product.instructor_id == user.id:
            return True
        if product.instructor_name and (product.instructor_name.strip().lower() in (user.name or "").strip().lower() or product.instructor_name == user.email):
            return True
    return False


@router.get("/admin", response_class=HTMLResponse)
def instructor_panel(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # If user is in student mode, redirect to switch or admin
    if getattr(user, "active_mode", "student") != "instructor" and user.role != "admin":
        user.active_mode = "instructor"
        request.session["active_mode"] = "instructor"
        db.commit()

    if user.role == "admin":
        products = product_service.get_all_products(db)
    else:
        products = product_service.get_instructor_courses(db, user)

    categories = product_service.get_categories(db)

    total_users_cnt = db.query(User).count()
    total_courses_cnt = db.query(Product).count()
    total_recs_cnt = db.query(Recommendation).count()
    total_events_cnt = db.query(Event).count()

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "active_page": "admin",
            "products": products,
            "categories": categories,
            "total_users": total_users_cnt,
            "total_courses": total_courses_cnt,
            "total_recs": total_recs_cnt,
            "total_events": total_events_cnt,
        },
    )



@router.post("/api/products")
def api_create_product(
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    price: float = Form(0.0),
    level: str = Form("Beginner"),
    skills: str = Form(""),
    instructor_name: str = Form(""),
    rating: float = Form(None),
    num_ratings: int = Form(None),
    duration_hours: float = Form(None),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ins_name = instructor_name.strip() if instructor_name else (user.name or user.email.split("@")[0])

    product = product_service.create_product(
        db=db,
        title=title,
        description=description,
        category=category,
        price=price,
        level=level,
        skills=skills,
        instructor_id=user.id,
        instructor_name=ins_name,
        rating=rating,
        num_ratings=num_ratings,
        duration_hours=duration_hours,
        status="active",
    )

    return {"id": product.id, "title": product.title}


@router.put("/api/products/{product_id}")
def api_update_product(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(None),
    description: str = Form(None),
    category: str = Form(None),
    price: float = Form(None),
    level: str = Form(None),
    skills: str = Form(None),
    instructor_name: str = Form(None),
    rating: float = Form(None),
    num_ratings: int = Form(None),
    duration_hours: float = Form(None),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    product = product_service.get_product(db, product_id)
    if not product:
        return JSONResponse({"error": "not found"}, status_code=404)

    if not _can_manage_course(user, product):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    updated = product_service.update_product(
        db,
        product_id,
        title=title,
        description=description,
        category=category,
        price=price,
        level=level,
        skills=skills,
        instructor_name=instructor_name,
        rating=rating,
        num_ratings=num_ratings,
        duration_hours=duration_hours,
    )

    return {"id": updated.id}


@router.delete("/api/products/{product_id}")
def api_delete_product(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    product = product_service.get_product(db, product_id)
    if not product:
        return JSONResponse({"error": "not found"}, status_code=404)

    if not _can_manage_course(user, product):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    ok = product_service.delete_product(db, product_id)
    return {"deleted": ok}


@router.post("/api/enroll")
def api_enroll(
    request: Request,
    db: Session = Depends(get_db),
    product_id: int = Form(...),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    product = product_service.get_product(db, product_id)
    if not product:
        return JSONResponse({"error": "not found"}, status_code=404)

    existing = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == user.id, Enrollment.product_id == product_id)
        .first()
    )
    if existing:
        return {"already_enrolled": True}

    enrollment = Enrollment(user_id=user.id, product_id=product_id)
    db.add(enrollment)

    if is_agent_tracking_enabled(db, user, request):
        db.add(Event(
            user_id=user.id,
            event_type="enroll",
            product_id=product_id,
            event_metadata=json.dumps({"source": "course_details", "category": product.category}),
            agent_eligible=True,
        ))

    # Conversion tracking: check if product_id was in any of user's recommendations
    user_recs = db.query(Recommendation).filter(Recommendation.user_id == user.id).all()
    for rec in user_recs:
        try:
            pids = json.loads(rec.product_ids) if rec.product_ids else []
            if product_id in pids and not getattr(rec, "converted", False):
                rec.converted = True
                rec.converted_at = datetime.now(timezone.utc)
                logger.info("Recommendation conversion tracked: rec_id=%s user_id=%s product_id=%s", rec.id, user.id, product_id)
        except Exception:
            pass

    db.commit()
    logger.info("User enrolled: user_id=%s product_id=%s product_title=%r", user.id, product.id, product.title)

    return {"enrolled": True}