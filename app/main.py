"""
FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload

Routers for auth, documents, loads, reviews, etc. get included here as they're
built (see app/api/) — this file stays a thin composition root, not a place
for business logic.
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.services.errors import NotFoundError, ProcessingError, ValidationError

app = FastAPI(
    title=settings.app_name,
    description="AI-powered carrier invoice audit for freight brokerages.",
    version="0.1.0",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["system"])
def health_check() -> dict:
    """Liveness/readiness probe — no auth, no DB hit, just confirms the app is up."""
    return {"status": "ok", "app": settings.app_name, "env": settings.env}


# --- Service-layer exception -> HTTP translation ---
#
# app/services/*.py raise these instead of HTTPException so the service
# layer stays framework-agnostic (see app/services/errors.py). This is the
# one place that knows what HTTP status each one maps to; routers never
# need a try/except for them.

@app.exception_handler(NotFoundError)
def _handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
def _handle_validation_error(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ProcessingError)
def _handle_processing_error(request: Request, exc: ProcessingError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# --- Routers ---
from app.api.auth import router as auth_router
from app.api.company import router as company_router
from app.api.documents import router as documents_router
from app.api.email import router as email_router
from app.api.loads import router as loads_router
from app.api.reviews import router as reviews_router

app.include_router(auth_router, prefix=f"{settings.api_v1_prefix}/auth", tags=["auth"])
app.include_router(company_router, prefix=f"{settings.api_v1_prefix}/company", tags=["company"])
app.include_router(loads_router, prefix=f"{settings.api_v1_prefix}/loads", tags=["loads"])
app.include_router(documents_router, prefix=f"{settings.api_v1_prefix}/documents", tags=["documents"])
app.include_router(reviews_router, prefix=f"{settings.api_v1_prefix}/reviews", tags=["reviews"])
app.include_router(email_router, prefix=f"{settings.api_v1_prefix}/email", tags=["email"])
