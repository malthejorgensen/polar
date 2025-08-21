"""Time travel settings model for testing and debugging."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import TIMESTAMP, Boolean, ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from polar.kit.db.models.base import RecordModel
from polar.kit.utils import utc_now

if TYPE_CHECKING:
    from polar.models.organization import Organization
    from polar.models.user import User


def get_default_expiry() -> datetime:
    """Default expiry time is 24 hours from now."""
    from datetime import timedelta

    return utc_now() + timedelta(hours=24)


class TimeTravelSetting(RecordModel):
    __tablename__ = "time_travel_settings"
    __table_args__ = (UniqueConstraint("organization_id"),)

    organization_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, index=True, default=get_default_expiry
    )

    set_by_user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @declared_attr
    def organization(cls) -> Mapped["Organization"]:
        from polar.models.organization import Organization

        return relationship(Organization, lazy="joined")

    @declared_attr
    def set_by_user(cls) -> Mapped["User | None"]:
        from polar.models.user import User

        return relationship(User, lazy="joined")

    @property
    def is_expired(self) -> bool:
        """Check if the time travel setting has expired."""
        return utc_now() > self.expires_at

    @property
    def is_active(self) -> bool:
        """Check if the time travel setting is currently active."""
        return self.enabled and not self.is_expired
