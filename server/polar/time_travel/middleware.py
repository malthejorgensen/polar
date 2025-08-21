"""Middleware for time travel context management."""

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from polar.auth.models import AuthSubject, is_organization, is_user
from polar.kit.db.postgres import create_async_sessionmaker
from polar.postgres import create_async_engine

from .service import time_travel

# Context variable to store time travel offset for the current request
time_travel_offset_context: ContextVar[int | None] = ContextVar(
    "time_travel_offset", default=None
)


def get_time_with_travel_offset() -> datetime:
    """Get current time with time travel offset applied if set."""
    offset = time_travel_offset_context.get()
    if offset is not None:
        return datetime.now(UTC) + timedelta(seconds=offset)
    return datetime.now(UTC)


class TimeTravelMiddleware(BaseHTTPMiddleware):
    """Middleware to set time travel context for requests."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore
        # Initialize offset as None
        time_travel_offset_context.set(None)

        # Try to get organization from auth context
        organization_id: uuid.UUID | None = None

        # Check if we have auth_subject in request state
        if hasattr(request.state, "auth_subject"):
            auth_subject: AuthSubject[Any] = request.state.auth_subject

            # Get organization ID based on auth subject type
            if is_organization(auth_subject):
                organization_id = auth_subject.subject.id
            elif is_user(auth_subject):
                # For user auth, check if they have an organization context
                # This could be from a header or query parameter
                org_id_str = request.headers.get("X-Polar-Organization-ID")
                if not org_id_str:
                    org_id_str = request.query_params.get("organization_id")

                if org_id_str:
                    try:
                        organization_id = uuid.UUID(org_id_str)
                    except ValueError:
                        pass

        # If we have an organization, check for time travel settings
        if organization_id:
            async_engine = create_async_engine("app")
            async_sessionmaker = create_async_sessionmaker(async_engine)
            async with async_sessionmaker() as session:
                setting = await time_travel.get_active_setting(session, organization_id)
                if setting and setting.is_active:
                    time_travel_offset_context.set(setting.offset_seconds)

        # Continue with request
        response = await call_next(request)

        # Add header if time travel is active
        offset = time_travel_offset_context.get()
        if offset is not None:
            response.headers["X-Time-Travel-Active"] = "true"
            response.headers["X-Time-Travel-Offset"] = str(offset)
            simulated_time = get_time_with_travel_offset()
            response.headers["X-Time-Travel-Current"] = simulated_time.isoformat()
            response.headers["X-Time-Travel-Real"] = datetime.now(UTC).isoformat()

        return response
