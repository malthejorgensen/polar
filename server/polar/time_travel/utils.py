"""Service layer for time travel functionality."""

import uuid
from datetime import UTC, datetime

import structlog

from polar.kit.db.postgres import AsyncSession
from polar.logging import Logger

from .repository import TimeTravelRepository

log: Logger = structlog.get_logger()


async def get_current_time(
    session: AsyncSession, organization_id: uuid.UUID
) -> datetime:
    """Get current time -- simulated (if time travel is enabled) or real -- an organization."""
    repository = TimeTravelRepository.from_session(session)
    setting = await repository.get_by_organization(session, organization_id)

    if setting and setting.enabled:
        return setting.simulated_time
    else:
        return datetime.now(UTC)
