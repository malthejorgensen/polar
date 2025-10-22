from collections import namedtuple
from collections.abc import Generator
from datetime import datetime
from decimal import Decimal
from typing import TypedDict
from unittest.mock import AsyncMock, MagicMock

import freezegun
import pytest
import stripe as stripe_lib
from pytest_mock import MockerFixture

from polar.enums import (
    SubscriptionRecurringInterval,
)
from polar.event.repository import EventRepository
from polar.event.system import SystemEvent
from polar.integrations.stripe.service import StripeService
from polar.kit.utils import utc_now
from polar.models.subscription import SubscriptionStatus
from polar.postgres import AsyncSession
from polar.subscription.service import subscription as subscription_service
from polar.time_travel.service import time_travel as time_travel_service
from tests.fixtures.database import SaveFixture
from tests.fixtures.random_objects import (
    create_customer,
    create_organization,
    create_product,
    create_subscription,
    create_time_travel_setting,
)

Hooks = namedtuple("Hooks", "updated activated canceled uncanceled revoked")
HookNames = frozenset(Hooks._fields)


def assert_hooks_called_once(subscription_hooks: Hooks, called: set[str]) -> None:
    for hook in called:
        getattr(subscription_hooks, hook).assert_called_once()

    not_called = HookNames - called
    for hook in not_called:
        getattr(subscription_hooks, hook).assert_not_called()


def reset_hooks(subscription_hooks: Hooks) -> None:
    for hook in HookNames:
        getattr(subscription_hooks, hook).reset_mock()


def build_stripe_payment_intent(
    *,
    amount: int = 0,
    status: str = "succeeded",
    customer: str | None = "CUSTOMER_ID",
    payment_method: str | None = "PAYMENT_METHOD_ID",
) -> stripe_lib.PaymentIntent:
    return stripe_lib.PaymentIntent.construct_from(
        {
            "id": "STRIPE_PAYMENT_INTENT_ID",
            "amount": amount,
            "status": status,
            "customer": customer,
            "payment_method": payment_method,
        },
        None,
    )


@pytest.fixture
def subscription_hooks(mocker: MockerFixture) -> Hooks:
    updated = mocker.patch.object(subscription_service, "_on_subscription_updated")
    activated = mocker.patch.object(subscription_service, "_on_subscription_activated")
    canceled = mocker.patch.object(subscription_service, "_on_subscription_canceled")
    uncanceled = mocker.patch.object(
        subscription_service, "_on_subscription_uncanceled"
    )
    revoked = mocker.patch.object(subscription_service, "_on_subscription_revoked")
    return Hooks(
        updated=updated,
        activated=activated,
        canceled=canceled,
        uncanceled=uncanceled,
        revoked=revoked,
    )


@pytest.fixture(autouse=True)
def stripe_service_mock(mocker: MockerFixture) -> MagicMock:
    mock = MagicMock(spec=StripeService)
    mocker.patch("polar.subscription.service.stripe_service", new=mock)
    return mock


@pytest.fixture
def publish_checkout_event_mock(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("polar.subscription.service.publish_checkout_event")


@pytest.fixture
def enqueue_benefits_grants_mock(mocker: MockerFixture) -> MagicMock:
    return mocker.patch.object(subscription_service, "enqueue_benefits_grants")


@pytest.fixture
def enqueue_job_mock(mocker: MockerFixture) -> MagicMock:
    return mocker.patch("polar.subscription.service.enqueue_job")


@pytest.fixture
def frozen_time() -> Generator[datetime, None]:
    frozen_time = utc_now()
    with freezegun.freeze_time(frozen_time):
        yield frozen_time


class SubscriptionFixture(TypedDict):
    id: str
    expected_events: int
    time_travel: tuple[datetime, datetime]
    started_at: datetime
    current_period_start: tuple[datetime | None, datetime | None]
    current_period_end: tuple[datetime | None, datetime | None]
    canceled_at: tuple[datetime | None, datetime | None]
    cancel_at_period_end: tuple[bool, bool]
    ends_at: tuple[datetime | None, datetime | None]
    ended_at: tuple[datetime | None, datetime | None]


fixtures: list[SubscriptionFixture] = [
    {
        "id": "do-nothing",
        "expected_events": 0,
        "time_travel": (datetime(2025, 1, 1), datetime(2025, 1, 1)),
        "started_at": datetime(2025, 1, 1),
        "current_period_start": (datetime(2025, 1, 1), datetime(2025, 1, 1)),
        "current_period_end": (datetime(2025, 1, 8), datetime(2025, 1, 8)),
        "cancel_at_period_end": (False, False),
        "canceled_at": (None, None),
        "ends_at": (None, None),
        "ended_at": (None, None),
    },
    {
        "id": "forward-cycle-minus-one-hour",
        "expected_events": 0,
        "time_travel": (datetime(2025, 1, 1), datetime(2025, 1, 7, 23)),
        "started_at": datetime(2025, 1, 1),
        "current_period_start": (datetime(2025, 1, 1), datetime(2025, 1, 1)),
        "current_period_end": (datetime(2025, 1, 8), datetime(2025, 1, 8)),
        "cancel_at_period_end": (False, False),
        "canceled_at": (None, None),
        "ends_at": (None, None),
        "ended_at": (None, None),
    },
    {
        "id": "forward-cycle-plus-one-hour",
        "expected_events": 1,
        "time_travel": (datetime(2025, 1, 1), datetime(2025, 1, 8, 1)),
        "started_at": datetime(2025, 1, 1),
        "current_period_start": (datetime(2025, 1, 1), datetime(2025, 1, 8)),
        "current_period_end": (datetime(2025, 1, 8), datetime(2025, 1, 15)),
        "cancel_at_period_end": (False, False),
        "canceled_at": (None, None),
        "ends_at": (None, None),
        "ended_at": (None, None),
    },
    {
        "id": "forward-two-cycles",
        "expected_events": 2,
        "time_travel": (datetime(2025, 1, 1), datetime(2025, 1, 15, 1)),
        "started_at": datetime(2025, 1, 1),
        "current_period_start": (datetime(2025, 1, 1), datetime(2025, 1, 15)),
        "current_period_end": (datetime(2025, 1, 8), datetime(2025, 1, 22)),
        "cancel_at_period_end": (False, False),
        "canceled_at": (None, None),
        "ends_at": (None, None),
        "ended_at": (None, None),
    },
]


@pytest.mark.parametrize("setup", [pytest.param(f, id=f["id"]) for f in fixtures])
@pytest.mark.asyncio
async def test_trigger_time_operations(
    session: AsyncSession,
    enqueue_job_mock: MagicMock,
    save_fixture: SaveFixture,
    setup: SubscriptionFixture,
) -> None:
    # Create 2 organizations
    org = await create_organization(
        save_fixture,
    )
    other_org = await create_organization(
        save_fixture,
    )
    # 2 products
    product = await create_product(
        save_fixture,
        organization=org,
        recurring_interval=SubscriptionRecurringInterval.week,
        prices=[(Decimal(100),)],
    )
    other_product = await create_product(
        save_fixture,
        organization=other_org,
        recurring_interval=SubscriptionRecurringInterval.week,
        prices=[(Decimal(100),)],
    )
    # 2 customers
    customer = await create_customer(
        save_fixture,
        organization=org,
    )
    other_customer = await create_customer(
        save_fixture,
        organization=other_org,
    )

    #
    subscription = await create_subscription(
        save_fixture,
        product=product,
        customer=customer,
        status=SubscriptionStatus.active,
        started_at=setup["started_at"],
        current_period_start=setup["current_period_start"][0],
        current_period_end=setup["current_period_end"][0],
        canceled_at=setup["canceled_at"][0],
        cancel_at_period_end=setup["cancel_at_period_end"][0],
        ends_at=setup["ends_at"][0],
        ended_at=setup["ended_at"][0],
    )
    other_subscription = await create_subscription(
        save_fixture,
        product=other_product,
        customer=other_customer,
        status=SubscriptionStatus.active,
        started_at=datetime(2025, 1, 1),
        current_period_start=datetime(2025, 1, 1),
        current_period_end=datetime(2025, 2, 1),
        canceled_at=None,
        cancel_at_period_end=False,
        ends_at=None,
        ended_at=None,
    )

    old_simulated_time = setup["time_travel"][0]
    new_simulated_time = setup["time_travel"][1]

    setting = await create_time_travel_setting(
        save_fixture,
        organization_id=org.id,
        simulated_time=old_simulated_time,
        expires_at=None,
        set_by_user_id=None,
        enabled=False,
    )

    session.refresh(subscription)
    await time_travel_service._trigger_time_operations(
        session, org, setting, old_simulated_time, new_simulated_time
    )

    assert subscription.started_at == setup["started_at"]
    assert subscription.current_period_start == setup["current_period_start"][1]
    assert subscription.current_period_end == setup["current_period_end"][1]
    assert subscription.canceled_at == setup["canceled_at"][1]
    assert subscription.cancel_at_period_end == setup["cancel_at_period_end"][1]
    assert subscription.ends_at == setup["ends_at"][1]
    assert subscription.ended_at == setup["ended_at"][1]

    # Check that subscription outside of the org is always untouched
    session.refresh(other_subscription)
    assert other_subscription.started_at == datetime(2025, 1, 1)
    assert other_subscription.current_period_start == datetime(2025, 1, 1)
    assert other_subscription.current_period_end == datetime(2025, 2, 1)
    assert other_subscription.canceled_at is None
    assert other_subscription.cancel_at_period_end is False
    assert other_subscription.ends_at is None
    assert other_subscription.ended_at is None

    if setup["expected_events"] >= 1:
        event_repository = EventRepository.from_session(session)
        events = await event_repository.get_all_by_name(SystemEvent.subscription_cycled)
        assert len(events) == setup["expected_events"]
        event = events[0]
        assert event.user_metadata["subscription_id"] == str(subscription.id)
        assert event.customer_id == customer.id
        assert event.organization_id == customer.organization_id
