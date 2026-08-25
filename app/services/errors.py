"""
Domain exceptions for the service layer.

Services raise these instead of fastapi.HTTPException so they stay
framework-agnostic — callable from a background worker or a script with no
FastAPI in sight. app/main.py registers a handler for each one, translating
it to the right HTTP status; routers themselves don't need to catch or know
about status codes at all.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for errors raised by the service layer."""


class NotFoundError(ServiceError):
    """The requested resource doesn't exist, or isn't visible to the caller's tenant. -> 404"""


class ValidationError(ServiceError):
    """The request itself is invalid — bad input, wrong shape, a missing required field. -> 400"""


class ProcessingError(ServiceError):
    """The request was well-formed but couldn't be completed — e.g. Claude extraction failed. -> 422"""
