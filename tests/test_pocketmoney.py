from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from lnbits.commands import migrate_databases
from lnbits.core.crud import create_wallet, get_wallet
from lnbits.core.services import create_user_account
from lnbits.core.services.payments import update_wallet_balance
from pocketmoney.crud import (
    _utc_ts,
    claim_plan_running,
    create_plan,
    db,
    get_due_plans,
    get_executions,
    get_plan,
    release_plan_running,
    update_plan,
    update_plan_execution,
)
from pocketmoney.migrations import (
    m001_initial,
    m002_add_plan_is_running,
    m003_add_plan_running_since,
    m004_timestamps_in_utc,
)
from pocketmoney.models import (
    CadenceType,
    Execution,
    ExecutionStatus,
    ItemCreate,
    Plan,
    PlanCreate,
    PlanUpdate,
    TriggerType,
)
from pocketmoney.services import (
    _parse_cron_field,
    calculate_next_run,
    execute_plan,
    simulate_plan,
    verify_target_wallet,
)
from pocketmoney.views_api import (
    _validate_plan_items_not_self,
    api_webhook_trigger,
)


async def _migrate(conn):
    await m001_initial(conn)
    for migration in (m002_add_plan_is_running, m003_add_plan_running_since, m004_timestamps_in_utc):
        try:
            await migration(conn)
        except Exception:
            pass  # Column may already exist from a prior test run


def test_parse_cron_field():
    assert _parse_cron_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}
    assert _parse_cron_field("1,3,5", 0, 5) == {1, 3, 5}
    assert _parse_cron_field("2-4", 0, 5) == {2, 3, 4}
    assert _parse_cron_field("*/2", 0, 5) == {0, 2, 4}
    assert _parse_cron_field("1-5/2", 0, 5) == {1, 3, 5}


def test_utc_timestamp_placeholder():
    """SQLite stores epochs (UTC already); other databases pin to UTC."""
    assert _utc_ts("now") == ":now"
    original_type = db.type
    try:
        db.type = "COCKROACH"
        expected = f"({db.timestamp_placeholder('now')} AT TIME ZONE 'UTC')"
        assert _utc_ts("now") == expected
    finally:
        db.type = original_type


def test_calculate_next_run_weekly():
    ref_time = datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc)
    next_run = calculate_next_run("0 9 * * 5", "UTC", from_time=ref_time)
    assert next_run.weekday() == 4
    assert next_run.hour == 9
    assert next_run.minute == 0
    assert next_run.day == 25


def test_calculate_next_run_daily():
    ref_time = datetime(2026, 9, 18, 8, 0, 0, tzinfo=timezone.utc)
    next_run = calculate_next_run("0 9 * * *", "UTC", from_time=ref_time)
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
            ItemCreate(label="Alice", recipient="alice@getalby.com", amount=Decimal("4.00"), currency="EUR"),
            ItemCreate(label="Bob", recipient="internal_wallet_1", amount=Decimal("1000"), currency="SAT"),
            ItemCreate(label="Charlie", recipient="lnurl1dp68gurn8ghj7mr0vd6...", amount=Decimal("4.00"), currency="EUR"),
        ],
    )
    assert plan_data.name == "Kids Pocket Money"
    assert len(plan_data.items) == 3
    assert plan_data.items[0].currency == "EUR"


@pytest.mark.anyio
async def test_e2e_plan_execution():
    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user = await create_user_account()
    parent = await create_wallet(user_id=user.id, wallet_name="Parent Test")
    child1 = await create_wallet(user_id=user.id, wallet_name="Bob Test")
    child2 = await create_wallet(user_id=user.id, wallet_name="Alice Test")

    await update_wallet_balance(parent, 30000)
    p_check = await get_wallet(parent.id)
    assert p_check and p_check.balance == 30000

    plan_data = PlanCreate(
        name="Test Kids Allowance",
        cadence_type=CadenceType.WEEKLY,
        cron_expression="0 9 * * 5",
        items=[
            ItemCreate(label="Bob", recipient=child1.id, amount=Decimal("3000"), currency="SAT"),
            ItemCreate(label="Alice", recipient=child2.id, amount=Decimal("4000"), currency="SAT"),
        ],
    )
    next_run = calculate_next_run(plan_data.cron_expression, plan_data.timezone)
    plan = await create_plan(parent.id, plan_data, next_run)

    sim = await simulate_plan(plan.id, parent.id)
    assert sim.can_execute is True
    assert sim.total_sats == 7000
    assert sim.balance_after_sats == 23000

    execution = await execute_plan(plan.id, triggered_by="manual")
    assert execution.status == ExecutionStatus.SUCCESS.value
    assert execution.total_sats == 7000
    assert len(execution.details) == 2

    parent_after = await get_wallet(parent.id)
    child1_after = await get_wallet(child1.id)
    child2_after = await get_wallet(child2.id)

    assert parent_after and parent_after.balance == 23000
    assert child1_after and child1_after.balance == 3000
    assert child2_after and child2_after.balance == 4000

    executions = await get_executions(parent.id, plan_id=plan.id)
    assert len(executions) >= 1
    assert executions[0].id == execution.id

    updated_plan = await get_plan(plan.id)
    assert updated_plan and updated_plan.last_run_at is not None
    assert updated_plan.next_run_at is not None

    large_plan_data = PlanCreate(
        name="Excessive Plan",
        cadence_type=CadenceType.MONTHLY,
        cron_expression="0 0 1 * *",
        items=[
            ItemCreate(label="Big Target", recipient=child1.id, amount=Decimal("100000"), currency="SAT"),
        ],
    )
    large_next_run = calculate_next_run(large_plan_data.cron_expression, large_plan_data.timezone)
    large_plan = await create_plan(parent.id, large_plan_data, large_next_run)
    failed_exec = await execute_plan(large_plan.id, triggered_by="manual")

    assert failed_exec.status == ExecutionStatus.SKIPPED_INSUFFICIENT_FUNDS.value
    assert failed_exec.total_sats == 0

    parent_untouched = await get_wallet(parent.id)
    assert parent_untouched and parent_untouched.balance == 23000

    past_due = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db.execute(
        f"UPDATE {db.references_schema}plans SET next_run_at = {db.timestamp_placeholder('past_due')} WHERE id = :id",
        {"past_due": past_due, "id": large_plan.id},
    )
    daemon_exec = await execute_plan(large_plan.id, triggered_by=TriggerType.DAEMON.value)
    assert daemon_exec.status == ExecutionStatus.SKIPPED_INSUFFICIENT_FUNDS.value
    plan_after_daemon = await get_plan(large_plan.id)
    assert plan_after_daemon and plan_after_daemon.next_run_at > past_due


@pytest.mark.anyio
async def test_partial_failure_status():
    """
    When one recipient succeeds and another fails (e.g. deleted wallet),
    the execution status must be PARTIAL — not FAILED — so the user knows
    which payments went through and does not accidentally double-pay on retry.
    """
    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user = await create_user_account()
    parent = await create_wallet(user_id=user.id, wallet_name="Parent Partial Test")
    good_child = await create_wallet(user_id=user.id, wallet_name="Good Child")

    await update_wallet_balance(parent, 10000)

    # Use a non-existent wallet ID as the bad recipient — payment will fail
    bad_recipient = "nonexistent-wallet-id-that-does-not-exist"

    plan_data = PlanCreate(
        name="Partial Failure Plan",
        cadence_type=CadenceType.WEEKLY,
        cron_expression="0 9 * * 5",
        items=[
            ItemCreate(label="GoodKid", recipient=good_child.id, amount=Decimal("1000"), currency="SAT"),
            ItemCreate(label="BadKid", recipient=bad_recipient, amount=Decimal("500"), currency="SAT"),
        ],
    )
    next_run = calculate_next_run(plan_data.cron_expression, plan_data.timezone)
    plan = await create_plan(parent.id, plan_data, next_run)

    execution = await execute_plan(plan.id, triggered_by="manual")

    # Status must be PARTIAL, not FAILED
    assert execution.status == ExecutionStatus.PARTIAL.value, (
        f"Expected PARTIAL but got {execution.status}. "
        "A misleading FAILED status could cause accidental double-pays on retry."
    )

    # Per-item detail records must reflect individual outcomes
    assert len(execution.details) == 2
    good_detail = next(d for d in execution.details if d["label"] == "GoodKid")
    bad_detail = next(d for d in execution.details if d["label"] == "BadKid")
    assert good_detail["status"] == "success"
    assert bad_detail["status"] == "failed"

    # error_message must warn about the partial nature
    assert execution.error_message is not None
    assert "Partial failure" in execution.error_message
    assert "GoodKid" in execution.error_message
    assert "BadKid" in execution.error_message
    assert "do not re-run all recipients" in execution.error_message

    # GoodKid's wallet received the payment
    good_after = await get_wallet(good_child.id)
    assert good_after and good_after.balance == 1000


@pytest.mark.anyio
async def test_db_claim_guard():
    """
    claim_plan_running atomically claims the is_running flag.
    A second concurrent claim must return False. After release, a new claim succeeds.
    """
    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user = await create_user_account()
    wallet = await create_wallet(user_id=user.id, wallet_name="Claim Test")
    plan_data = PlanCreate(
        name="Claim Guard Test Plan",
        cadence_type=CadenceType.WEEKLY,
        cron_expression="0 9 * * 5",
        items=[],
    )
    next_run = calculate_next_run(plan_data.cron_expression, plan_data.timezone)
    plan = await create_plan(wallet.id, plan_data, next_run)

    # First claim should succeed
    claimed_first = await claim_plan_running(plan.id)
    assert claimed_first is True, "First claim must succeed"

    # Expected scheduler collisions must not create audit failures.
    assert await execute_plan(plan.id, triggered_by=TriggerType.DAEMON.value) is None
    assert await get_executions(wallet.id, plan_id=plan.id) == []

    # Second claim (simulating a concurrent worker) must fail
    claimed_second = await claim_plan_running(plan.id)
    assert claimed_second is False, "Concurrent claim must be rejected — plan is already running"

    # Release the claim
    await release_plan_running(plan.id)
    claim = await db.fetchone(
        f"SELECT is_running, running_since FROM {db.references_schema}plans WHERE id = :id",
        {"id": plan.id},
    )
    assert claim and not claim["is_running"] and claim["running_since"] is None

    # After release, a new claim must succeed
    claimed_after_release = await claim_plan_running(plan.id)
    assert claimed_after_release is True, "Claim after release must succeed"

    # A claim left behind by a worker that died must not wedge the plan forever
    stale_since = datetime.now(timezone.utc) - timedelta(minutes=30)
    await db.execute(
        f"UPDATE {db.references_schema}plans "
        f"SET running_since = {db.timestamp_placeholder('stale_since')} WHERE id = :id",
        {"stale_since": stale_since, "id": plan.id},
    )
    reclaimed = await claim_plan_running(plan.id)
    assert reclaimed is True, "Stale claim must be reclaimable"

    # Cleanup
    await release_plan_running(plan.id)


@pytest.mark.anyio
async def test_postgres_datetime_binds_use_timestamp_placeholder(monkeypatch):
    """
    LNbits converts every datetime bind into a float epoch, which asyncpg
    rejects for TIMESTAMP columns unless the placeholder is wrapped as
    to_timestamp(:param). SQLite is unaffected by the wrapper.
    """
    captured: list[str] = []
    now = datetime.now(timezone.utc)

    async def capture_execute(query, values=None):
        captured.append(query)

    async def capture_fetchall(query, values=None, model=None):
        captured.append(query)
        return []

    async def fake_get_plan(*args, **kwargs):
        return Plan(
            id="plan-id",
            wallet_id="wallet-id",
            name="Postgres bind guard",
            cadence_type="daily",
            cron_expression="0 9 * * *",
            webhook_token="token",
            next_run_at=now,
        )

    monkeypatch.setattr("lnbits.db.DB_TYPE", "POSTGRES")
    monkeypatch.setattr(db, "type", "POSTGRES")
    monkeypatch.setattr(db, "execute", capture_execute)
    monkeypatch.setattr(db, "fetchall", capture_fetchall)
    monkeypatch.setattr("pocketmoney.crud.get_plan", fake_get_plan)

    await get_due_plans(now)
    await update_plan_execution("plan-id", now, now)
    await update_plan("plan-id", PlanUpdate(name="Renamed"), next_run_at=now)
    await create_plan("wallet-id", PlanCreate(name="New plan", items=[]), now)

    sql = "\n".join(captured)
    assert "next_run_at <= (to_timestamp(:now) AT TIME ZONE 'UTC')" in sql
    assert "last_run_at = (to_timestamp(:last_run_at) AT TIME ZONE 'UTC')" in sql
    assert "next_run_at = (to_timestamp(:next_run_at) AT TIME ZONE 'UTC')" in sql


@pytest.mark.anyio
async def test_postgres_timestamp_migration_uses_each_row_timezone():
    statements: list[str] = []

    class CaptureDB:
        type = "POSTGRES"
        references_schema = "pocketmoney."

        async def execute(self, query):
            statements.append(query)

    await m004_timestamps_in_utc(CaptureDB())

    assert len(statements) == 2
    assert "next_run_at AT TIME ZONE current_setting('TimeZone')" in statements[0]
    assert "executed_at AT TIME ZONE current_setting('TimeZone')" in statements[1]


@pytest.mark.anyio
async def test_webhook_can_trigger_a_plan_before_its_next_schedule(monkeypatch):
    now = datetime.now(timezone.utc)
    plan = Plan(
        id="plan-id",
        wallet_id="wallet-id",
        name="External event",
        cadence_type="daily",
        cron_expression="0 9 * * *",
        webhook_token="secret",
        next_run_at=now + timedelta(days=1),
    )
    execution = Execution(
        id="execution-id",
        plan_id=plan.id,
        wallet_id=plan.wallet_id,
        triggered_by=TriggerType.WEBHOOK.value,
        status=ExecutionStatus.SUCCESS.value,
        total_sats=1,
    )
    calls = []

    async def fake_get_plan_by_webhook(token):
        assert token == plan.webhook_token
        return plan

    async def fake_execute_plan(plan_id, triggered_by):
        calls.append((plan_id, triggered_by))
        return execution

    monkeypatch.setattr("pocketmoney.views_api.get_plan_by_webhook", fake_get_plan_by_webhook)
    monkeypatch.setattr("pocketmoney.views_api.execute_plan", fake_execute_plan)

    result = await api_webhook_trigger(plan.webhook_token)

    assert calls == [(plan.id, TriggerType.WEBHOOK.value)]
    assert result["status"] == ExecutionStatus.SUCCESS.value


@pytest.mark.anyio
async def test_cross_user_internal_wallet_execution_and_simulation():
    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user_a = await create_user_account()
    user_b = await create_user_account()
    assert user_a.id != user_b.id

    wallet_a = await create_wallet(user_id=user_a.id, wallet_name="User A Funding")
    wallet_b = await create_wallet(user_id=user_b.id, wallet_name="User B Peer")

    await update_wallet_balance(wallet_a, 10000)

    plan_data = PlanCreate(
        name="Cross-User Allowance",
        cadence_type=CadenceType.WEEKLY,
        cron_expression="0 9 * * 5",
        items=[
            ItemCreate(
                label="Peer B",
                recipient=wallet_b.id,
                amount=Decimal("2500"),
                currency="SAT",
            ),
        ],
    )
    next_run = calculate_next_run(plan_data.cron_expression, plan_data.timezone)
    plan = await create_plan(wallet_a.id, plan_data, next_run)

    assert len(plan.items) == 1
    assert plan.items[0].recipient_type == "internal"

    sim = await simulate_plan(plan.id, wallet_a.id)
    assert sim.can_execute is True
    assert sim.total_sats == 2500
    assert sim.balance_after_sats == 7500

    execution = await execute_plan(plan.id, triggered_by="manual")
    assert execution is not None
    assert execution.status == ExecutionStatus.SUCCESS.value
    assert execution.total_sats == 2500
    assert execution.total_fees_msat == 0

    wallet_a_after = await get_wallet(wallet_a.id)
    wallet_b_after = await get_wallet(wallet_b.id)
    assert wallet_a_after and wallet_a_after.balance == 7500
    assert wallet_b_after and wallet_b_after.balance == 2500


@pytest.mark.anyio
async def test_verify_wallet_endpoint_privacy_and_self_guard():
    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user_a = await create_user_account()
    user_b = await create_user_account()

    wallet_a = await create_wallet(user_id=user_a.id, wallet_name="Secret Funding Wallet")
    wallet_a_secondary = await create_wallet(user_id=user_a.id, wallet_name="Own Child Wallet")
    wallet_b = await create_wallet(user_id=user_b.id, wallet_name="Private Peer Wallet")
    await update_wallet_balance(wallet_b, 99999)

    # 1. Cross-user valid wallet: must not leak name, balance, user_id, or keys
    res_peer = await verify_target_wallet(wallet_b.id, wallet_a.id, user_a.id)
    assert res_peer.valid is True
    assert res_peer.exists is True
    assert res_peer.is_self is False
    assert res_peer.is_same_user is False
    dumped = res_peer.model_dump() if hasattr(res_peer, "model_dump") else res_peer.dict()
    assert set(dumped.keys()) == {"valid", "exists", "is_self", "is_same_user", "reason"}
    assert "Private Peer Wallet" not in str(dumped)
    assert "99999" not in str(dumped)
    assert user_b.id not in str(dumped)

    # 2. Same-user secondary wallet
    res_own = await verify_target_wallet(wallet_a_secondary.id, wallet_a.id, user_a.id)
    assert res_own.valid is True
    assert res_own.exists is True
    assert res_own.is_self is False
    assert res_own.is_same_user is True

    # 3. Self-funding wallet guard
    res_self = await verify_target_wallet(wallet_a.id, wallet_a.id, user_a.id)
    assert res_self.valid is False
    assert res_self.exists is True
    assert res_self.is_self is True
    assert res_self.is_same_user is True
    assert res_self.reason and "same as the funding wallet" in res_self.reason

    # 4. Non-existent wallet ID
    res_missing = await verify_target_wallet("nonexistent-wallet-id-0000", wallet_a.id, user_a.id)
    assert res_missing.valid is False
    assert res_missing.exists is False
    assert res_missing.is_self is False
    assert res_missing.is_same_user is False


@pytest.mark.anyio
async def test_self_payment_rejected_in_simulation_and_execution():
    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user_a = await create_user_account()
    wallet_a = await create_wallet(user_id=user_a.id, wallet_name="Self Guard Wallet")
    await update_wallet_balance(wallet_a, 5000)

    self_item = ItemCreate(
        label="Myself",
        recipient=wallet_a.id,
        amount=Decimal("1000"),
        currency="SAT",
    )

    with pytest.raises(HTTPException) as exc_info:
        _validate_plan_items_not_self([self_item], wallet_a.id)
    assert exc_info.value.status_code == 400
    assert "same as the funding wallet" in str(exc_info.value.detail)

    plan_data = PlanCreate(
        name="Self Loop Plan",
        cadence_type=CadenceType.WEEKLY,
        cron_expression="0 9 * * 5",
        items=[self_item],
    )
    next_run = calculate_next_run(plan_data.cron_expression, plan_data.timezone)
    plan = await create_plan(wallet_a.id, plan_data, next_run)

    sim = await simulate_plan(plan.id, wallet_a.id)
    assert sim.can_execute is False
    assert sim.items[0].valid is False
    assert any("same as the funding wallet" in w for w in sim.warnings)

    execution = await execute_plan(plan.id, triggered_by="manual")
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.total_sats == 0

    wallet_after = await get_wallet(wallet_a.id)
    assert wallet_after and wallet_after.balance == 5000

