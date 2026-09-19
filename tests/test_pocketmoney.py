from datetime import datetime, timezone
from decimal import Decimal

import pytest
from lnbits.commands import migrate_databases
from lnbits.core.crud import create_wallet, get_wallet
from lnbits.core.services import create_user_account
from lnbits.core.services.payments import update_wallet_balance
from pocketmoney.crud import create_plan, get_executions, get_plan
from pocketmoney.models import CadenceType, ExecutionStatus, ItemCreate, PlanCreate
from pocketmoney.services import (
    _parse_cron_field,
    calculate_next_run,
    execute_plan,
    simulate_plan,
)


def test_parse_cron_field():
    assert _parse_cron_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}
    assert _parse_cron_field("1,3,5", 0, 5) == {1, 3, 5}
    assert _parse_cron_field("2-4", 0, 5) == {2, 3, 4}
    assert _parse_cron_field("*/2", 0, 5) == {0, 2, 4}
    assert _parse_cron_field("1-5/2", 0, 5) == {1, 3, 5}


def test_calculate_next_run_weekly():
    # Friday 2026-09-18 10:00 UTC
    ref_time = datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc)
    # Target: every Friday at 09:00 (since 10:00 is past 09:00, next should be next Friday Sep 25)
    next_run = calculate_next_run("weekly", "0 9 * * 5", "UTC", from_time=ref_time)
    assert next_run.weekday() == 4  # Friday
    assert next_run.hour == 9
    assert next_run.minute == 0
    assert next_run.day == 25


def test_calculate_next_run_daily():
    ref_time = datetime(2026, 9, 18, 8, 0, 0, tzinfo=timezone.utc)
    # Target: daily at 09:00 (should be today at 09:00)
    next_run = calculate_next_run("daily", "0 9 * * *", "UTC", from_time=ref_time)
    assert next_run.day == 18
    assert next_run.hour == 9
    assert next_run.minute == 0


def test_plan_create_model():
    plan_data = PlanCreate(
        name="Kids Pocket Money",
        description="Weekly allowance",
        cadence_type=CadenceType.WEEKLY,
        cron_expression="0 9 * * 5",
        low_balance_threshold=50000,
        items=[
            ItemCreate(label="Alisa", recipient="alisa@getalby.com", amount=Decimal("4.00"), currency="EUR"),
            ItemCreate(label="Felix", recipient="internal_wallet_1", amount=Decimal("1000"), currency="SAT"),
            ItemCreate(label="Valentina", recipient="lnurl1dp68gurn8ghj7mr0vd6...", amount=Decimal("4.00"), currency="EUR"),
        ],
    )
    assert plan_data.name == "Kids Pocket Money"
    assert len(plan_data.items) == 3
    assert plan_data.items[0].currency == "EUR"


@pytest.mark.anyio
async def test_e2e_plan_execution():
    # Ensure database is migrated
    await migrate_databases()

    # Create user and wallets
    user = await create_user_account()
    parent = await create_wallet(user_id=user.id, wallet_name="Parent Test")
    child1 = await create_wallet(user_id=user.id, wallet_name="Felix Test")
    child2 = await create_wallet(user_id=user.id, wallet_name="Alisa Test")

    # Fund parent wallet with 30,000 sats
    await update_wallet_balance(parent, 30000, memo="Deposit")
    p_check = await get_wallet(parent.id)
    assert p_check and p_check.balance == 30000

    # Create plan (Felix: 3000 sats, Alisa: 4000 sats)
    plan_data = PlanCreate(
        name="Test Kids Allowance",
        cadence_type=CadenceType.WEEKLY,
        cron_expression="0 9 * * 5",
        items=[
            ItemCreate(label="Felix", recipient=child1.id, amount=Decimal("3000"), currency="SAT"),
            ItemCreate(label="Alisa", recipient=child2.id, amount=Decimal("4000"), currency="SAT"),
        ],
    )
    next_run = calculate_next_run(plan_data.cadence_type.value, plan_data.cron_expression, plan_data.timezone)
    plan = await create_plan(parent.id, plan_data, next_run)

    # Dry-run simulation
    sim = await simulate_plan(plan.id, parent.id)
    assert sim.can_execute is True
    assert sim.total_sats == 7000
    assert sim.balance_after_sats == 23000

    # Execute plan
    execution = await execute_plan(plan.id, triggered_by="manual")
    assert execution.status == ExecutionStatus.SUCCESS.value
    assert execution.total_sats == 7000
    assert len(execution.details) == 2

    # Verify wallet balances after execution
    parent_after = await get_wallet(parent.id)
    child1_after = await get_wallet(child1.id)
    child2_after = await get_wallet(child2.id)

    assert parent_after and parent_after.balance == 23000
    assert child1_after and child1_after.balance == 3000
    assert child2_after and child2_after.balance == 4000

    # Verify audit logs & plan next_run_at
    executions = await get_executions(parent.id, plan_id=plan.id)
    assert len(executions) >= 1
    assert executions[0].id == execution.id

    updated_plan = await get_plan(plan.id)
    assert updated_plan and updated_plan.last_run_at is not None
    assert updated_plan.next_run_at is not None

    # Test fail-closed behavior on insufficient funds
    large_plan_data = PlanCreate(
        name="Excessive Plan",
        cadence_type=CadenceType.MONTHLY,
        cron_expression="0 0 1 * *",
        items=[
            ItemCreate(label="Big Target", recipient=child1.id, amount=Decimal("100000"), currency="SAT"),
        ],
    )
    large_next_run = calculate_next_run(large_plan_data.cadence_type.value, large_plan_data.cron_expression, large_plan_data.timezone)
    large_plan = await create_plan(parent.id, large_plan_data, large_next_run)
    failed_exec = await execute_plan(large_plan.id, triggered_by="manual")

    assert failed_exec.status == ExecutionStatus.SKIPPED_INSUFFICIENT_FUNDS.value
    assert failed_exec.total_sats == 0

    # Parent balance unchanged
    parent_untouched = await get_wallet(parent.id)
    assert parent_untouched and parent_untouched.balance == 23000

