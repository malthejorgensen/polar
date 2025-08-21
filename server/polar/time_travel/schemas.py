"""Schemas for time travel API endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from polar.kit.schemas import Schema


class TimeTravelSetRequest(BaseModel):
    """Request to set absolute simulated time."""

    simulated_time: datetime = Field(
        ...,
        description="The absolute datetime to simulate (must be UTC)",
    )
    expires_in_hours: int = Field(
        default=24,
        description="Hours until the time travel setting expires",
        ge=1,
        le=168,  # Max 1 week
    )


class TimeTravelSetting(Schema):
    """Time travel setting response."""

    id: str
    organization_id: str
    simulated_time: datetime
    offset_seconds: int  # Calculated field for convenience
    expires_at: datetime
    enabled: bool
    is_active: bool
    set_by_user_id: str | None
    set_by_user_email: str | None = None
    created_at: datetime
    modified_at: datetime | None

    @classmethod
    def from_db(cls, setting: Any) -> "TimeTravelSetting":
        """Create from database model."""
        return cls(
            id=str(setting.id),
            organization_id=str(setting.organization_id),
            simulated_time=setting.simulated_time,
            offset_seconds=setting.offset_seconds,
            expires_at=setting.expires_at,
            enabled=setting.enabled,
            is_active=setting.is_active,
            set_by_user_id=str(setting.set_by_user_id)
            if setting.set_by_user_id
            else None,
            set_by_user_email=setting.set_by_user.email
            if setting.set_by_user
            else None,
            created_at=setting.created_at,
            modified_at=setting.modified_at,
        )


class TimeTravelStatus(Schema):
    """Current time travel status."""

    active: bool
    real_time: datetime
    simulated_time: datetime
    offset_seconds: int | None
    setting: TimeTravelSetting | None
