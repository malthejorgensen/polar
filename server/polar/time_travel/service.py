"""Service layer for time travel functionality."""

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import structlog
from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, delete, or_, select, update

from polar.exceptions import BadRequest, Unauthorized
from polar.kit.db.postgres import AsyncSession
from polar.logging import Logger
from polar.models import (
    BillingEntry,
    Customer,
    Event,
    Order,
    Organization,
    Payment,
    Product,
    TimeTravelSetting,
    User,
)
from polar.models.subscription import Subscription, SubscriptionStatus
from polar.subscription.repository import SubscriptionRepository
from polar.subscription.service import subscription as subscription_service

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

        if setting:
            if setting.enabled:
                old_simulated_time = setting.simulated_time
            else:
                old_simulated_time = real_time
                setting = await repository.update(
                    setting,
                    update_dict={
                        "enabled": True,
                        "simulated_time": new_simulated_time,
                    },
                    flush=True,
                )
        else:
            setting = await repository.create(
                TimeTravelSetting(
                    organization_id=organization.id,
                    simulated_time=new_simulated_time,
                    set_by_user_id=user.id,
                    expires_at=expires_at,
                ),
                flush=True,
            )
            old_simulated_time = real_time

        # Trigger time-sensitive operations
        tasks_triggered = await self._trigger_time_operations(
            session,
            organization,
            user,
            setting,
            old_simulated_time,
            new_simulated_time,
        )

        # Update simulated time
        setting.simulated_time = new_simulated_time
        setting.set_modified_at()
        await session.flush()

        # Clear cache for this organization
        if organization.id in _time_travel_cache:
            del _time_travel_cache[organization.id]

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
            real_now = datetime.now(UTC)
            old_simulated_time = setting.simulated_time
            new_simulated_time = real_now

            tasks_triggered = await self._trigger_time_operations(
                session,
                organization,
                setting,
                old_simulated_time,
                new_simulated_time,
            )

            setting.simulated = real_now
            setting.enabled = False
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
        user: User,
        setting: TimeTravelSetting,
        old_simulated_time: datetime,
        new_simulated_time: datetime,
    ) -> dict[str, int]:
        """Trigger time-sensitive operations based on simulated time."""
        tasks_triggered = {
            "subscription_cycles": 0,
            "order_retries": 0,
            "meter_updates": 0,
        }

        async def run_jobs(at_time: datetime) -> None:
            # Process subscriptions that need cycling
            subscription_repo = SubscriptionRepository.from_session(session)
            stmt = (
                select(Subscription)
                .join(Subscription.product)
                .where(
                    Subscription.product.has(organization_id=organization.id),
                    Subscription.status == SubscriptionStatus.active,
                    Subscription.deleted_at.is_(None),
                    Subscription.current_period_end <= at_time,
                    # Subscription.ends_at <= at_time,
                )
                .order_by(Subscription.current_period_end.asc())
                .options(*subscription_repo.get_eager_options())
            )
            result = await session.execute(stmt)
            subscriptions = result.scalars().all()
            for subscription in subscriptions:
                await session.refresh(subscription, {"product"})

            # Update organization time
            # FIXME: This doesn't work right now because we just schedule the jobs, and
            # after we exit `_trigger_time_operations()` we immediately set the simulated
            # time to the "end date" which means the simulated that the jobs see is that
            # "end date" rather than the intermediate time we're trying to set here.
            repository = TimeTravelRepository.from_session(session)
            await repository.update(
                setting, update_dict={"simulated_time": at_time}, flush=True
            )

            for subscription in subscriptions:
                await subscription_service.cycle(session, subscription=subscription)
                tasks_triggered["subscription_cycles"] += 1

        if new_simulated_time >= old_simulated_time:
            # Step forward in time, in steps of one day
            intermediate_simulated_time = old_simulated_time
            limit = range(365)
            for i_day in limit:
                await run_jobs(intermediate_simulated_time)

                intermediate_simulated_time += timedelta(days=1)
                if intermediate_simulated_time >= new_simulated_time:
                    break

            # Run one last time
            await run_jobs(new_simulated_time)
        else:
            # Delete Payments, Orders, BillingEntries etc. that are after new_simulated_time
            stmt = delete(Payment).where(
                Payment.organization_id == organization.id,
                Payment.created_at.between(new_simulated_time, old_simulated_time),
            )
            result = await session.execute(stmt)

            stmt = delete(BillingEntry).where(
                BillingEntry.customer_id == Customer.id,
                Customer.organization_id == organization.id,
                BillingEntry.created_at.between(new_simulated_time, old_simulated_time),
            )
            result = await session.execute(stmt)

            stmt = delete(Order).where(
                Order.customer_id == Customer.id,
                Customer.organization_id == organization.id,
                Order.created_at.between(new_simulated_time, old_simulated_time),
            )
            result = await session.execute(stmt)

            stmt = delete(Event).where(
                Event.organization_id == organization.id,
                Event.timestamp.between(new_simulated_time, old_simulated_time),
                # Event.source == EventSource.system
            )
            result = await session.execute(stmt)

            # Delete subscriptions created after the simulation date
            # MAYBE:
            # - Don't delete subscriptions started before `real_time`
            # - Don't delete subscriptions ever? (even if they're in the future)
            subscription_repo = SubscriptionRepository.from_session(session)
            stmt = delete(Subscription).where(
                Subscription.product_id == Product.id,
                Product.organization_id == organization.id,
                Subscription.deleted_at.is_(None),
                Subscription.started_at >= new_simulated_time,
            )
            result = await session.execute(stmt)

            # Uncancel subscriptions canceled after the simulation date
            subscription_repo = SubscriptionRepository.from_session(session)
            stmt = (
                update(Subscription)
                .where(
                    Subscription.product_id == Product.id,
                    Product.organization_id == organization.id,
                    Subscription.deleted_at.is_(None),
                    Subscription.canceled_at >= new_simulated_time,
                )
                .values(
                    {
                        Subscription.status: SubscriptionStatus.active,
                        Subscription.canceled_at: None,
                        Subscription.cancel_at_period_end: None,
                        Subscription.ends_at: None,
                        Subscription.ended_at: None,
                    }
                )
            )
            result = await session.execute(stmt)

            # Reset subscription cycles to new simulated date
            subscription_repo = SubscriptionRepository.from_session(session)
            stmt = (
                select(Subscription)
                .join(Subscription.product)
                .where(
                    Subscription.product.has(organization_id=organization.id),
                    Subscription.deleted_at.is_(None),
                    or_(
                        and_(
                            Subscription.current_period_start >= new_simulated_time,
                            Subscription.status == SubscriptionStatus.active,
                        ),
                        and_(
                            Subscription.ended_at >= new_simulated_time,
                            Subscription.status == SubscriptionStatus.canceled,
                        ),
                    ),
                    # Subscription.ends_at <= at_time,
                )
                .order_by(Subscription.current_period_end.asc())
                .options(*subscription_repo.get_eager_options())
            )
            result = await session.execute(stmt)
            subscriptions = result.scalars().all()

            for subscription in subscriptions:
                t_diff = relativedelta(new_simulated_time, old_simulated_time)
                # days, weeks, months, years
                str_interval = str(subscription.recurring_interval) + "s"
                num_intervals = getattr(t_diff, str_interval)
                new_period_start = subscription.current_period_start + relativedelta(
                    **{str_interval: num_intervals}
                )
                new_period_end = subscription.current_period_end + relativedelta(
                    **{str_interval: num_intervals}
                )

                uncancel = {}
                if (
                    subscription.canceled_at
                    and subscription.canceled_at >= new_simulated_time
                ):
                    uncancel = {
                        "canceled_at": None,
                        "ends_at": None,
                        "cancel_at_period_end": None,
                    }
                if (
                    subscription.ended_at
                    and subscription.ended_at >= new_simulated_time
                ):
                    uncancel = {
                        "status": SubscriptionStatus.active,
                        "ended_at": None,
                        "cancel_at_period_end": None,
                    }

                await subscription_repo.update(
                    subscription,
                    update_dict={
                        "current_period_start": new_period_start,
                        "current_period_end": new_period_end,
                        **uncancel,
                    },
                    flush=False,
                )

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
