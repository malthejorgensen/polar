"""API endpoints for time travel functionality."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from polar.auth.models import is_user
from polar.kit.db.postgres import AsyncSession
from polar.kit.utils import utc_now
from polar.models import Organization
from polar.organization.repository import OrganizationRepository
from polar.postgres import get_db_session

from .auth import AdminUser
from .schemas import (
    TimeTravelSetRequest,
    TimeTravelSetting,
    TimeTravelStatus,
)
from .service import time_travel

router = APIRouter(prefix="/api/v1/time-travel", tags=["time_travel"])


async def get_organization(
    organization_id: uuid.UUID,
    session: AsyncSession,
    auth_subject: AdminUser,
) -> Organization:
    """Get organization and verify access."""
    org_repo = OrganizationRepository.from_session(session)
    organization = await org_repo.get_by_id(organization_id)

    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    return organization


@router.get("/status", response_model=TimeTravelStatus)
async def get_time_travel_status(
    auth_subject: AdminUser,
    organization_id: uuid.UUID = Query(..., description="Organization ID"),
    session: AsyncSession = Depends(get_db_session),
) -> TimeTravelStatus:
    """Get current time travel status for an organization."""
    organization = await get_organization(organization_id, session, auth_subject)

    setting = await time_travel.get_active_setting(session, organization.id)
    real_time = utc_now()  # This will be affected if time travel is active

    # Get real time without time travel
    from datetime import UTC, datetime

    actual_real_time = datetime.now(UTC)

    if setting and setting.is_active:
        return TimeTravelStatus(
            active=True,
            real_time=actual_real_time,
            simulated_time=setting.simulated_time,
            offset_seconds=setting.offset_seconds,
            setting=TimeTravelSetting.from_db(setting),
        )
    else:
        return TimeTravelStatus(
            active=False,
            real_time=actual_real_time,
            simulated_time=actual_real_time,
            offset_seconds=None,
            setting=None,
        )


@router.post("/set", response_model=TimeTravelSetting)
async def set_time_travel(
    organization_id: uuid.UUID,
    request: TimeTravelSetRequest,
    auth_subject: AdminUser,
    session: AsyncSession = Depends(get_db_session),
) -> TimeTravelSetting:
    """Set absolute simulated time for an organization."""
    organization = await get_organization(organization_id, session, auth_subject)

    if not is_user(auth_subject):
        raise HTTPException(status_code=403, detail="User authentication required")

    setting = await time_travel.set_simulated_time(
        session=session,
        organization=organization,
        user=auth_subject.subject,
        simulated_time=request.simulated_time,
        expires_in_hours=request.expires_in_hours,
    )

    await session.commit()

    return TimeTravelSetting.from_db(setting)


@router.delete("/clear")
async def clear_time_travel(
    auth_subject: AdminUser,
    organization_id: uuid.UUID = Query(..., description="Organization ID"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Clear time travel settings for an organization."""
    organization = await get_organization(organization_id, session, auth_subject)

    if not is_user(auth_subject):
        raise HTTPException(status_code=403, detail="User authentication required")

    await time_travel.clear_time_offset(
        session=session,
        organization=organization,
        user=auth_subject.subject,
    )

    await session.commit()

    return {"message": "Time travel settings cleared"}
