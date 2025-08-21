"""Service layer for time travel functionality."""

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from polar.exceptions import BadRequest, Unauthorized
from polar.kit.db.postgres import AsyncSession
from polar.kit.utils import utc_now
from polar.logging import Logger
from polar.models import Organization, TimeTravelSetting, User
from polar.models.subscription import Subscription, SubscriptionStatus
from polar.subscription.repository import SubscriptionRepository
from polar.worker import enqueue_job

from .repository import TimeTravelRepository

log: Logger = structlog.get_logger()

# Context variable to store organization ID for time travel
_time_travel_org_context: ContextVar[uuid.UUID | None] = ContextVar(
    "time_travel_org", default=None
)

# Cache for time travel settings to avoid repeated DB queries
_time_travel_cache: dict[uuid.UUID, tuple[TimeTravelSetting | None, datetime]] = {}
CACHE_TTL_SECONDS = 60  # Cache for 1 minute

# Maximum time travel offset (365 days)
MAX_OFFSET_DAYS = 365
MAX_OFFSET_SECONDS = MAX_OFFSET_DAYS * 24 * 60 * 60


class TimeTravelService:
    """Service for managing time travel settings and operations."""

    async def get_setting(
        self, session: AsyncSession, organization_id: uuid.UUID
    ) -> TimeTravelSetting | None:
        """Get time travel settings for an organization."""
        repository = TimeTravelRepository.from_session(session)
        return await repository.get_by_organization(session, organization_id)

    async def get_active_setting(
        self, session: AsyncSession, organization_id: uuid.UUID
    ) -> TimeTravelSetting | None:
        """Get active time travel settings for an organization (cached)."""
        # Check cache first
        now = utc_now()
        if organization_id in _time_travel_cache:
            cached_setting, cached_at = _time_travel_cache[organization_id]
            if (now - cached_at).total_seconds() < CACHE_TTL_SECONDS:
                return (
                    cached_setting
                    if cached_setting and cached_setting.is_active
                    else None
                )

        # Not in cache or expired, fetch from DB
        repository = TimeTravelRepository.from_session(session)
        setting = await repository.get_active_by_organization(session, organization_id)

        # Update cache
        _time_travel_cache[organization_id] = (setting, now)

        return setting

    async def set_time_offset(
        self,
        session: AsyncSession,
        organization: Organization,
        user: User,
        offset_seconds: int,
        expires_in_hours: int = 24,
    ) -> TimeTravelSetting:
        """Set time travel offset for an organization."""
        # Validate user is admin
        if not user.is_admin:
            raise Unauthorized("Only admin users can set time travel")

        # Validate offset
        if abs(offset_seconds) > MAX_OFFSET_SECONDS:
            raise BadRequest(f"Time offset cannot exceed ±{MAX_OFFSET_DAYS} days")

        # Calculate expiry
        expires_at = utc_now() + timedelta(hours=expires_in_hours)

        # Create or update setting
        repository = TimeTravelRepository.from_session(session)
        setting = await repository.create_or_update(
            session=session,
            organization_id=organization.id,
            offset_seconds=offset_seconds,
            user_id=user.id,
            expires_at=expires_at,
        )

        # Clear cache for this organization
        if organization.id in _time_travel_cache:
            del _time_travel_cache[organization.id]

        log.info(
            "time_travel.set",
            organization_id=str(organization.id),
            user_id=str(user.id),
            offset_seconds=offset_seconds,
            expires_at=expires_at.isoformat(),
        )

        return setting

    async def clear_time_offset(
        self,
        session: AsyncSession,
        organization: Organization,
        user: User,
    ) -> None:
        """Clear time travel settings for an organization."""
        # Validate user is admin
        if not user.is_admin:
            raise Unauthorized("Only admin users can clear time travel")

        repository = TimeTravelRepository.from_session(session)
        setting = await repository.get_by_organization(session, organization.id)

        if setting:
            setting.enabled = False
            setting.set_deleted_at()
            await session.flush()

        # Clear cache
        if organization.id in _time_travel_cache:
            del _time_travel_cache[organization.id]

        log.info(
            "time_travel.clear",
            organization_id=str(organization.id),
            user_id=str(user.id),
        )

    async def advance_time_and_process(
        self,
        session: AsyncSession,
        organization: Organization,
        user: User,
        advance_seconds: int,
    ) -> dict[str, Any]:
        """Advance time and trigger time-sensitive operations."""
        # Validate user is admin
        if not user.is_admin:
            raise Unauthorized("Only admin users can advance time")

        # Get current setting
        repository = TimeTravelRepository.from_session(session)
        setting = await repository.get_by_organization(session, organization.id)

        if not setting or not setting.is_active:
            raise BadRequest("No active time travel settings for this organization")

        # Update offset
        new_offset = setting.offset_seconds + advance_seconds
        if abs(new_offset) > MAX_OFFSET_SECONDS:
            raise BadRequest(f"New offset would exceed ±{MAX_OFFSET_DAYS} days limit")

        setting.offset_seconds = new_offset
        setting.set_modified_at()
        await session.flush()

        # Clear cache
        if organization.id in _time_travel_cache:
            del _time_travel_cache[organization.id]

        # Calculate the simulated current time
        simulated_time = utc_now() + timedelta(seconds=new_offset)

        # Trigger time-sensitive operations
        tasks_triggered = await self._trigger_time_operations(
            session, organization, simulated_time
        )

        log.info(
            "time_travel.advance",
            organization_id=str(organization.id),
            user_id=str(user.id),
            advance_seconds=advance_seconds,
            new_offset_seconds=new_offset,
            simulated_time=simulated_time.isoformat(),
            tasks_triggered=tasks_triggered,
        )

        return {
            "new_offset_seconds": new_offset,
            "simulated_time": simulated_time.isoformat(),
            "tasks_triggered": tasks_triggered,
        }

    async def _trigger_time_operations(
        self,
        session: AsyncSession,
        organization: Organization,
        simulated_time: datetime,
    ) -> dict[str, int]:
        """Trigger time-sensitive operations based on simulated time."""
        tasks_triggered = {
            "subscription_cycles": 0,
            "order_retries": 0,
            "meter_updates": 0,
        }

        # Process subscriptions that need cycling
        subscription_repo = SubscriptionRepository.from_session(session)
        stmt = (
            select(Subscription)
            .join(Subscription.product)
            .where(
                Subscription.product.has(organization_id=organization.id),
                Subscription.status == SubscriptionStatus.active,
                Subscription.ends_at <= simulated_time,
                Subscription.deleted_at.is_(None),
            )
        )
        result = await session.execute(stmt)
        subscriptions = result.scalars().all()

        for subscription in subscriptions:
            enqueue_job("subscription.cycle", subscription_id=subscription.id)
            tasks_triggered["subscription_cycles"] += 1

        # TODO: Add order retry processing
        # TODO: Add meter credit expiration processing

        return tasks_triggered

    async def cleanup_expired(self, session: AsyncSession) -> int:
        """Clean up expired time travel settings."""
        repository = TimeTravelRepository.from_session(session)
        count = await repository.cleanup_expired(session)

        if count > 0:
            log.info("time_travel.cleanup", expired_count=count)

        return count

    def set_organization_context(self, organization_id: uuid.UUID | None) -> None:
        """Set the organization context for time travel."""
        _time_travel_org_context.set(organization_id)

    def get_organization_context(self) -> uuid.UUID | None:
        """Get the current organization context for time travel."""
        return _time_travel_org_context.get()

    def get_adjusted_time(self, session: AsyncSession | None = None) -> datetime:
        """Get the current time, adjusted for time travel if active.

        This is called by the modified utc_now() function.
        """
        # Get organization from context
        org_id = self.get_organization_context()
        if not org_id or not session:
            return datetime.now(UTC)

        # Check cache first (synchronous check)
        now = datetime.now(UTC)
        if org_id in _time_travel_cache:
            cached_setting, cached_at = _time_travel_cache[org_id]
            if (now - cached_at).total_seconds() < CACHE_TTL_SECONDS:
                if cached_setting and cached_setting.is_active:
                    return now + timedelta(seconds=cached_setting.offset_seconds)
                return now

        # If not cached, we can't do async DB lookup here, return real time
        # The cache will be populated on next async call
        return now


# Global service instance
time_travel = TimeTravelService()
