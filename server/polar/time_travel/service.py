"""Service layer for time travel functionality."""

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from polar.exceptions import BadRequest, Unauthorized
from polar.kit.db.postgres import AsyncSession
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
        now = datetime.now(UTC)  # Use real time for cache checks
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

    async def set_simulated_time(
        self,
        session: AsyncSession,
        organization: Organization,
        user: User,
        new_simulated_time: datetime,
        expires_in_hours: int = 24,
    ) -> TimeTravelSetting:
        """Set absolute simulated time for an organization."""
        # Validate user is admin
        if not user.is_admin:
            raise Unauthorized("Only admin users can set time travel")

        # Validate simulated time isn't too far from real time
        real_time = datetime.now(UTC)
        offset_seconds = int((new_simulated_time - real_time).total_seconds())
        if abs(offset_seconds) > MAX_OFFSET_SECONDS:
            raise BadRequest(
                f"Simulated time cannot be more than ±{MAX_OFFSET_DAYS} days from real time"
            )

        # Calculate expiry
        expires_at = real_time + timedelta(hours=expires_in_hours)

        # Create or update setting
        repository = TimeTravelRepository.from_session(session)
        setting = await repository.get_by_organization(session, organization.id)

        if setting and setting.enabled:
            old_simulated_time = setting.simulated_time
        else:
            old_simulated_time = real_time

        if not setting:
            setting = await repository.create_or_update(
                session=session,
                organization_id=organization.id,
                simulated_time=new_simulated_time,
                user_id=user.id,
                expires_at=expires_at,
            )
        else:
            # Update simulated time
            setting.simulated_time = new_simulated_time
            setting.set_modified_at()
            await session.flush()

        # Clear cache for this organization
        if organization.id in _time_travel_cache:
            del _time_travel_cache[organization.id]

        # Trigger time-sensitive operations
        tasks_triggered = await self._trigger_time_operations(
            session, organization, new_simulated_time
        )

        log.info(
            "time_travel.set",
            organization_id=str(organization.id),
            user_id=str(user.id),
            simulated_time=new_simulated_time.isoformat(),
            real_time=real_time.isoformat(),
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
        setting = await repository.get_active_by_organization(session, organization.id)

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

        # TODO: Undo orders and payments if traveling back in time

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


# Global service instance
time_travel = TimeTravelService()
