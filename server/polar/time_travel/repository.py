"""Repository for time travel settings."""

import uuid
from datetime import UTC, datetime

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

        now = datetime.now(UTC)  # Use real time, not time-traveled time
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
