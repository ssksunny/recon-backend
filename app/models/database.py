"""
Database engine, session factory, and declarative base.

Every request gets its own SQLAlchemy session via the `get_db` dependency,
which is closed automatically when the request finishes.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,   # drop stale connections instead of erroring on them
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


class Base(DeclarativeBase):
    """Base class every ORM model inherits from."""
    pass


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a database session and guarantees it is
    closed after the request, even if an exception is raised.

    Usage:
        @router.get("/things")
        def list_things(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
