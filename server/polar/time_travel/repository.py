"""Repository for time travel settings."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from polar.kit.db.postgres import AsyncSession
from polar.kit.repository.base import RepositoryBase
from polar.kit.utils import utc_now
from polar.models import TimeTravelSetting


class TimeTravelRepository(RepositoryBase[TimeTravelSetting]):
    model = TimeTravelSetting

    async def get_by_organization(
        self, session: AsyncSession, organization_id: uuid.UUID
    ) -> TimeTravelSetting | None:
        """Get time travel settings for an organization."""
        stmt = (
            select(TimeTravelSetting)
            .where(
                TimeTravelSetting.organization_id == organization_id,
                TimeTravelSetting.deleted_at.is_(None),
            )
            .options(
                joinedload(TimeTravelSetting.organization),
                joinedload(TimeTravelSetting.set_by_user),
            )
        )
        result = await session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_active_by_organization(
        self, session: AsyncSession, organization_id: uuid.UUID
    ) -> TimeTravelSetting | None:
        """Get active time travel settings for an organization."""
        now = utc_now()
        stmt = (
            select(TimeTravelSetting)
            .where(
                TimeTravelSetting.organization_id == organization_id,
                TimeTravelSetting.enabled,
                TimeTravelSetting.expires_at > now,
                TimeTravelSetting.deleted_at.is_(None),
            )
            .options(
                joinedload(TimeTravelSetting.organization),
                joinedload(TimeTravelSetting.set_by_user),
            )
        )
        result = await session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def cleanup_expired(self, session: AsyncSession) -> int:
        """Delete expired time travel settings."""
        now = utc_now()
        stmt = select(TimeTravelSetting).where(
            TimeTravelSetting.expires_at <= now,
            TimeTravelSetting.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        expired_settings = result.scalars().all()

        count = 0
        for setting in expired_settings:
            setting.set_deleted_at()
            count += 1

        if count > 0:
            await session.flush()

        return count

    async def create_or_update(
        self,
        session: AsyncSession,
        organization_id: uuid.UUID,
        offset_seconds: int,
        user_id: uuid.UUID,
        expires_at: datetime | None = None,
    ) -> TimeTravelSetting:
        """Create or update time travel settings for an organization."""
        # First check for any existing record (including soft-deleted ones)
        stmt = (
            select(TimeTravelSetting)
            .where(TimeTravelSetting.organization_id == organization_id)
            .options(
                joinedload(TimeTravelSetting.organization),
                joinedload(TimeTravelSetting.set_by_user),
            )
        )
        result = await session.execute(stmt)
        existing = result.unique().scalar_one_or_none()

        if existing:
            # Update existing setting
            # Update existing setting (even if soft-deleted)
            existing.offset_seconds = offset_seconds
            existing.set_by_user_id = user_id
            existing.enabled = True
            existing.deleted_at = None  # Undelete if it was soft-deleted
            if expires_at:
                existing.expires_at = expires_at
            else:
                # Reset to default 24 hours
                from datetime import timedelta
                existing.expires_at = utc_now() + timedelta(hours=24)
            existing.set_modified_at()
            await session.flush()
            return existing
        else:
            # Create new setting
            setting = TimeTravelSetting(
                organization_id=organization_id,
                offset_seconds=offset_seconds,
                set_by_user_id=user_id,
                expires_at=expires_at if expires_at else None,
                enabled=True,
            )
            session.add(setting)
            await session.flush()
            await session.refresh(
                setting,
                ["organization", "set_by_user"],
            )
            return setting
