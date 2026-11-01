from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import certifi
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
    m005_add_plan_labels,
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
    build_payment_memo,
    calculate_next_run,
    ensure_account_labels,
    execute_plan,
    simulate_plan,
    verify_target_wallet,
)
from pocketmoney.views_api import (
    _validate_plan_items_not_self,
    api_create_plan,
    api_update_plan,
    api_webhook_trigger,
)


async def _migrate(conn):
    await m001_initial(conn)
    for migration in (
        m002_add_plan_is_running,
        m003_add_plan_running_since,
        m004_timestamps_in_utc,
        m005_add_plan_labels,
    ):
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


def test_pyproject_declares_hardened_certifi():
    """Ensure pyproject.toml pins certifi>=2026.10.19 against compromised eMudhra CAs."""
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    content = pyproject_path.read_text(encoding="utf-8")
    assert '"certifi>=2026.10.19"' in content
    installed_version = tuple(int(part) for part in certifi.__version__.split("."))
    assert installed_version >= (2026, 10, 19)


def test_build_payment_memo_tiers_and_utf8_limits():
    # 1. External Lightning Address (ledger memo: no redundant 'PocketMoney:' prefix)
    ext_memo = build_payment_memo(
        "Monthly Server Hosting Cost Plan",
        "Provider A",
        "billing@pay.example.com",
        include_destination=True,
        include_prefix=False,
    )
    assert ext_memo == "Monthly Server Hosting Cost Plan -> Provider A (billing@pay.example.com)"

    # 2. Same-user internal My Wallet (Option A: omit raw internal hex ID)
    own_memo = build_payment_memo(
        "Weekly Family Allowance",
        "Alice",
        "9f8e7d6c5b4a39281726354433221100",
        include_destination=False,
        include_prefix=False,
    )
    assert own_memo == "Weekly Family Allowance -> Alice"

    # 3. Deduplicate when label already matches destination (case-insensitive)
    dedup_memo = build_payment_memo(
        "Hosting Bill",
        "billing@pay.example.com",
        "Billing@Pay.Example.com",
        include_destination=True,
    )
    assert dedup_memo == "Hosting Bill -> billing@pay.example.com"

    # 4. Raw long LNURL (> 48 chars) is elided even in Tier 1
    long_lnurl = "lnurl1dp68gurn8ghj7mr0vdskc6r0wd6z7mrww4excttsv9un7um9wdekjmmw84jxywf5x43rvv35xgmr2enrxanr2cfcvsmnwe3jxcukvde48qukgdec89snwde3vfjxvepjxpjnjvtpxd3kvdnxx5crxwpjvyunsephsz36jf"
    lnurl_memo = build_payment_memo("Monthly Tip", "Creator", long_lnurl, include_destination=True)
    assert lnurl_memo == f"Monthly Tip -> Creator ({long_lnurl[:12]}...{long_lnurl[-6:]})"

    # 5. Control characters & newlines collapsed cleanly for CSV safety
    dirty_memo = build_payment_memo("Plan\nName\r\t", "Alice\x00\nBob", "alice@domain.com\n")
    assert "\n" not in dirty_memo and "\r" not in dirty_memo and "\x00" not in dirty_memo
    assert dirty_memo == "Plan Name -> Alice Bob (alice@domain.com)"

    # 6. Progressive fitting for remote LNURL commentAllowed (include_prefix=True)
    full_comment = build_payment_memo(
        "Monthly Server Hosting Cost Plan",
        "Provider A",
        "billing@pay.example.com",
        include_destination=True,
        include_prefix=True,
        max_chars=799,
    )
    assert full_comment == "PocketMoney: Monthly Server Hosting Cost Plan -> Provider A (billing@pay.example.com)"

    comment_50 = build_payment_memo(
        "Monthly Server Hosting Cost Plan",
        "Provider A",
        "billing@pay.example.com",
        include_destination=True,
        include_prefix=True,
        max_chars=50,
    )
    assert len(comment_50) <= 50
    assert comment_50 == "Monthly Server Hosting Cost Plan -> Provider A"

    comment_32 = build_payment_memo(
        "Monthly Server Hosting Cost Plan",
        "Provider A",
        "billing@pay.example.com",
        include_destination=True,
        include_prefix=True,
        max_chars=32,
    )
    assert len(comment_32) <= 32
    assert comment_32.endswith(" -> Provider A")

    comment_8 = build_payment_memo(
        "Monthly Server Hosting Cost Plan",
        "Provider A",
        "billing@pay.example.com",
        include_destination=True,
        include_prefix=True,
        max_chars=8,
    )
    assert 0 < len(comment_8) <= 8

    # 7. BOLT11 639-byte UTF-8 ceiling with 4-byte emojis (never splits code points)
    emoji_plan = "💶" * 300  # 300 chars, 1200 UTF-8 bytes
    emoji_memo = build_payment_memo(emoji_plan, "Alice", "", include_destination=False)
    assert len(emoji_memo.encode("utf-8")) <= 639
    assert emoji_memo.endswith(" -> Alice")

    custom_emoji = build_payment_memo(
        "Plan",
        "Alice",
        "alice@domain.com",
        custom_memo="⚡" * 400,  # 1200 UTF-8 bytes
    )
    assert len(custom_emoji.encode("utf-8")) <= 639


@pytest.mark.anyio
async def test_external_lnurl_execution_writes_memo_tag_extra_and_adapts_comment(monkeypatch):
    import hashlib

    from lnbits.core.crud.payments import get_payments_paginated, get_wallet_payment
    from lnbits.core.models.payments import PaymentFilters
    from lnbits.db import Filter, Filters
    from lnurl import LnurlErrorResponse, LnurlPayActionResponse, LnurlPayResponse
    from lnurl.exceptions import LnurlResponseException

    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user = await create_user_account()
    funding = await create_wallet(user_id=user.id, wallet_name="Main Operations")
    ext_sink = await create_wallet(user_id=user.id, wallet_name="External LNURL Sink")
    await update_wallet_balance(funding, 100000)

    # Stub fiat conversion deterministically: 10.00 USD -> 12000 sats, 25.00 USD -> 30000 sats
    async def fake_fiat_to_sats(amount: float, currency: str) -> int:
        assert currency == "USD"
        if abs(amount - 10.0) < 0.01:
            return 12000
        if abs(amount - 25.0) < 0.01:
            return 30000
        return 5000

    monkeypatch.setattr("pocketmoney.services.fiat_amount_as_satoshis", fake_fiat_to_sats)

    handled_lookups: list[str] = []
    received_comments: list[tuple[str, str | None]] = []
    comment_limits = {
        "billing@pay.example.com": 140,
        "ops@pay.example.com": 32,
        "nocomment@pay.example.com": 0,
        "flakycomment@pay.example.com": 100,
    }

    async def fake_lnurl_handle(lnurl_str: str, **kwargs):
        handled_lookups.append(lnurl_str)
        # Real lnurl.LnAddress rejects uppercase characters in user@domain
        assert lnurl_str == lnurl_str.lower(), f"Expected lowercase LN address, got {lnurl_str}"
        meta = '[["text/plain","Pay via LNURL"]]'
        return LnurlPayResponse.parse_obj(
            {
                "tag": "payRequest",
                "callback": f"https://pay.example.com/lnurlp/{lnurl_str.split('@')[0]}/cb",
                "minSendable": 1000,
                "maxSendable": 1000000000,
                "metadata": meta,
                "commentAllowed": comment_limits[lnurl_str],
            }
        )

    async def fake_fetch_lnurl_pay_request(data, wallet=None):
        res = data.res
        cb_str = str(res.callback)
        if "flakycomment" in cb_str and data.comment:
            raise LnurlResponseException("Remote server rejected comment parameter")
        if res.commentAllowed and data.comment:
            assert len(data.comment) <= res.commentAllowed
        else:
            assert data.comment is None

        received_comments.append((cb_str, data.comment))
        # Generate a real LUD-06 invoice WITH description_hash (so invoice.description is None!)
        from lnbits.core.services import create_invoice as core_create_invoice

        inv = await core_create_invoice(
            wallet_id=ext_sink.id,
            amount=data.amount // 1000,
            memo="",
            description_hash=hashlib.sha256(b"lnurl-metadata").digest(),
            internal=True,
        )
        action_res = LnurlPayActionResponse.parse_obj({"pr": inv.bolt11, "routes": []})
        return res, action_res

    monkeypatch.setattr("pocketmoney.services.lnurl_handle", fake_lnurl_handle)
    monkeypatch.setattr("pocketmoney.services.fetch_lnurl_pay_request", fake_fetch_lnurl_pay_request)
    _ = LnurlErrorResponse  # ensure import sanity

    plan_data = PlanCreate(
        name="Monthly Server Hosting",
        cadence_type=CadenceType.MONTHLY,
        cron_expression="0 0 1 * *",
        items=[
            ItemCreate(label="Provider A", recipient="billing@pay.example.com", amount=Decimal("10.00"), currency="USD"),
            ItemCreate(label="Provider B", recipient="Ops@Pay.Example.com", amount=Decimal("25.00"), currency="USD"),
            ItemCreate(label="Strict Node", recipient="nocomment@pay.example.com", amount=Decimal("1000"), currency="SAT"),
            ItemCreate(label="Flaky Server", recipient="flakycomment@pay.example.com", amount=Decimal("1500"), currency="SAT"),
        ],
    )
    next_run = calculate_next_run(plan_data.cron_expression, plan_data.timezone)
    plan = await create_plan(funding.id, plan_data, next_run)

    execution = await execute_plan(plan.id, triggered_by="manual")
    assert execution is not None
    assert execution.status == ExecutionStatus.SUCCESS.value
    assert execution.total_sats == 12000 + 30000 + 1000 + 1500

    # Mixed-case Ops@Pay.Example.com was normalized to lowercase for lnurl_handle
    assert "ops@pay.example.com" in handled_lookups

    # Verify LNURL comments respected commentAllowed & retried cleanly on flakycomment
    prov_a_comment = next(c for cb, c in received_comments if "/billing/" in cb)
    assert prov_a_comment == "PocketMoney: Monthly Server Hosting -> Provider A (billing@pay.example.com)"
    prov_b_comment = next(c for cb, c in received_comments if "/ops/" in cb)
    assert prov_b_comment is not None and len(prov_b_comment) <= 32
    assert prov_b_comment.endswith(" -> Provider B")
    nocomment_cb = next(c for cb, c in received_comments if "/nocomment/" in cb)
    assert nocomment_cb is None
    flaky_cb = next(c for cb, c in received_comments if "/flakycomment/" in cb)
    assert flaky_cb is None

    # Verify sender ledger records in apipayments!
    prov_a_detail = next(d for d in execution.details if d["label"] == "Provider A")
    assert prov_a_detail["memo"] == "Monthly Server Hosting -> Provider A (billing@pay.example.com)"
    prov_a_pay = await get_wallet_payment(funding.id, prov_a_detail["payment_hash"])
    assert prov_a_pay is not None
    assert prov_a_pay.memo == "Monthly Server Hosting -> Provider A (billing@pay.example.com)"
    assert prov_a_pay.tag == "pocketmoney"
    assert prov_a_pay.extra.get("tag") == "pocketmoney"
    assert prov_a_pay.extra.get("app") == "PocketMoney"
    assert prov_a_pay.extra.get("plan_id") == plan.id
    assert prov_a_pay.extra.get("plan_name") == "Monthly Server Hosting"
    assert prov_a_pay.extra.get("recipient_label") == "Provider A"
    assert prov_a_pay.extra.get("destination") == "billing@pay.example.com"
    assert prov_a_pay.extra.get("fiat_currency") == "USD"
    assert prov_a_pay.extra.get("fiat_amount") == 10.0
    assert prov_a_pay.extra.get("comment") == prov_a_comment

    # Even when remote server had commentAllowed=0, local LNbits ledger still has full memo!
    strict_detail = next(d for d in execution.details if d["label"] == "Strict Node")
    strict_pay = await get_wallet_payment(funding.id, strict_detail["payment_hash"])
    assert strict_pay is not None
    assert strict_pay.memo == "Monthly Server Hosting -> Strict Node (nocomment@pay.example.com)"

    # Verify LNbits API search & tag filtering find all 4 PocketMoney payments!
    search_page = await get_payments_paginated(
        wallet_id=funding.id,
        outgoing=True,
        filters=Filters(search="pocketmoney", model=PaymentFilters),
    )
    assert search_page.total == 4

    tag_filter = Filter.parse_query("tag", ["pocketmoney"], model=PaymentFilters)
    tag_page = await get_payments_paginated(
        wallet_id=funding.id,
        outgoing=True,
        filters=Filters(filters=[tag_filter], model=PaymentFilters),
    )
    assert tag_page.total == 4


@pytest.mark.anyio
async def test_internal_wallet_memos_option_a_and_639_byte_fakewallet_safety():
    from lnbits.core.crud.payments import get_wallet_payment

    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user_parent = await create_user_account()
    user_peer = await create_user_account()

    parent_wallet = await create_wallet(user_id=user_parent.id, wallet_name="Parent Wallet")
    child_wallet = await create_wallet(user_id=user_parent.id, wallet_name="Alice Wallet")
    peer_wallet = await create_wallet(user_id=user_peer.id, wallet_name="External CoUser")
    await update_wallet_balance(parent_wallet, 50000)

    # 1. Option A: Own wallet omits hex ID; cross-user Peer Wallet includes destination on sender side
    plan_data = PlanCreate(
        name="Weekly Family Allowance",
        cadence_type=CadenceType.WEEKLY,
        cron_expression="0 9 * * 5",
        items=[
            ItemCreate(label="Alice", recipient=child_wallet.id, amount=Decimal("2000"), currency="SAT"),
            ItemCreate(label="Peer Contractor", recipient=peer_wallet.id, amount=Decimal("3000"), currency="SAT"),
        ],
    )
    plan = await create_plan(
        parent_wallet.id,
        plan_data,
        calculate_next_run(plan_data.cron_expression, plan_data.timezone),
    )
    execution = await execute_plan(plan.id, triggered_by="manual")
    assert execution and execution.status == ExecutionStatus.SUCCESS.value

    child_detail = next(d for d in execution.details if d["label"] == "Alice")
    child_out = await get_wallet_payment(parent_wallet.id, child_detail["payment_hash"])
    child_in = await get_wallet_payment(child_wallet.id, child_detail["payment_hash"])
    assert child_out and child_out.memo == "Weekly Family Allowance -> Alice"
    assert child_out.tag == "pocketmoney"
    assert child_out.extra.get("app") == "PocketMoney"
    assert child_out.extra.get("destination") == child_wallet.id
    assert child_in and child_in.memo == "Weekly Family Allowance -> Alice"

    peer_detail = next(d for d in execution.details if d["label"] == "Peer Contractor")
    peer_out = await get_wallet_payment(parent_wallet.id, peer_detail["payment_hash"])
    peer_in = await get_wallet_payment(peer_wallet.id, peer_detail["payment_hash"])
    assert peer_out and peer_out.memo == f"Weekly Family Allowance -> Peer Contractor ({peer_wallet.id})"
    assert peer_out.tag == "pocketmoney"
    # Receiver invoice memo stays clean without leaking sender's internal plan_id in extra
    assert peer_in and peer_in.memo == "Weekly Family Allowance -> Peer Contractor"
    assert "plan_id" not in (peer_in.extra or {})

    # 2. Multi-byte UTF-8 > 640 bytes must never crash FakeWallet BOLT11 encoding
    huge_emoji_plan = PlanCreate(
        name="💶" * 250,  # 1,000 UTF-8 bytes
        cadence_type=CadenceType.DAILY,
        cron_expression="0 9 * * *",
        items=[
            ItemCreate(label="Alice", recipient=child_wallet.id, amount=Decimal("1000"), currency="SAT"),
        ],
    )
    huge_plan = await create_plan(
        parent_wallet.id,
        huge_emoji_plan,
        calculate_next_run(huge_emoji_plan.cron_expression, huge_emoji_plan.timezone),
    )
    huge_exec = await execute_plan(huge_plan.id, triggered_by="manual")
    assert huge_exec and huge_exec.status == ExecutionStatus.SUCCESS.value
    huge_pay = await get_wallet_payment(parent_wallet.id, huge_exec.details[0]["payment_hash"])
    assert huge_pay and len(huge_pay.memo.encode("utf-8")) <= 639
    assert huge_pay.memo.endswith(" -> Alice")


@pytest.mark.anyio
async def test_plan_labels_validation_and_persistence():
    from pydantic import ValidationError

    # 1. Trimming, empty removal, order-preserving deduplication
    p_valid = PlanCreate(
        name="Labeled Plan",
        labels=[" vps ", "infra-2026.1", "vps", "", "  ", "Team Ops_EU"],
        items=[],
    )
    assert p_valid.labels == ["vps", "infra-2026.1", "Team Ops_EU"]

    # 2. Reject invalid label characters (must match LNbits ^[A-Za-z0-9 ._-]{1,100}$)
    for bad in [["#vps"], ["slash/bad"], ["a" * 101]]:
        with pytest.raises(ValidationError):
            PlanCreate(name="Bad Label", labels=bad, items=[])

    # 3. Reject > 20 labels per plan
    with pytest.raises(ValidationError):
        PlanCreate(name="Too Many Labels", labels=[f"lbl-{i}" for i in range(21)], items=[])

    # 4. Persist & partial-update labels in DB
    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user = await create_user_account()
    wallet = await create_wallet(user_id=user.id, wallet_name="Label CRUD Wallet")
    next_run = calculate_next_run("0 9 * * 5", "UTC")
    created = await create_plan(wallet.id, p_valid, next_run)
    assert created.labels == ["vps", "infra-2026.1", "Team Ops_EU"]

    # Toggling is_active alone must NOT wipe labels
    paused = await update_plan(created.id, PlanUpdate(is_active=False))
    assert paused and paused.is_active is False
    assert paused.labels == ["vps", "infra-2026.1", "Team Ops_EU"]

    # Updating labels replaces them; setting [] clears them
    retagged = await update_plan(created.id, PlanUpdate(labels=["accounting"]))
    assert retagged and retagged.labels == ["accounting"]

    cleared = await update_plan(created.id, PlanUpdate(labels=[]))
    assert cleared and cleared.labels == []


@pytest.mark.anyio
async def test_plan_labels_account_registration_and_payment_propagation():
    from lnbits.core.crud import get_account
    from lnbits.core.crud.payments import get_payments_paginated, get_wallet_payment
    from lnbits.core.models import KeyType, WalletTypeInfo
    from lnbits.core.models.payments import PaymentFilters
    from lnbits.core.models.users import UserLabel
    from lnbits.core.services.users import update_user_account
    from lnbits.db import Filters

    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user_owner = await create_user_account()
    user_peer = await create_user_account()

    funding = await create_wallet(user_id=user_owner.id, wallet_name="Owner Funding")
    own_child = await create_wallet(user_id=user_owner.id, wallet_name="Owner Child")
    peer_wallet = await create_wallet(user_id=user_peer.id, wallet_name="Peer Wallet")
    await update_wallet_balance(funding, 40000)

    # Pre-populate one existing label on owner account to verify color/description preservation
    owner_acc = await get_account(user_owner.id)
    assert owner_acc is not None
    owner_acc.extra.labels.append(
        UserLabel(name="allowance", color="#112233", description="Existing custom label")
    )
    await update_user_account(owner_acc)

    # Create plan via API handler to verify immediate Account.extra.labels sync on save
    wallet_info = WalletTypeInfo(KeyType.admin, funding)
    created_plan = await api_create_plan(
        PlanCreate(
            name="Kids & Contractor",
            cadence_type=CadenceType.WEEKLY,
            cron_expression="0 9 * * 5",
            labels=["allowance", "family-budget"],
            items=[
                ItemCreate(label="Child", recipient=own_child.id, amount=Decimal("2000"), currency="SAT"),
                ItemCreate(label="Contractor", recipient=peer_wallet.id, amount=Decimal("3000"), currency="SAT"),
            ],
        ),
        wallet=wallet_info,
    )
    assert created_plan.labels == ["allowance", "family-budget"]

    owner_acc_after_create = await get_account(user_owner.id)
    assert owner_acc_after_create is not None
    by_name = {lbl.name: lbl for lbl in owner_acc_after_create.extra.labels}
    assert set(by_name.keys()) == {"allowance", "family-budget"}
    # Existing label preserved verbatim
    assert by_name["allowance"].color == "#112233"
    assert by_name["allowance"].description == "Existing custom label"
    # Newly registered label gets valid #RRGGBB color
    assert by_name["family-budget"].color is not None
    assert len(by_name["family-budget"].color) == 7 and by_name["family-budget"].color.startswith("#")

    # Update plan via API handler to add a third label
    updated_plan = await api_update_plan(
        created_plan.id,
        PlanUpdate(labels=["allowance", "family-budget", "monthly"]),
        wallet=wallet_info,
    )
    assert updated_plan.labels == ["allowance", "family-budget", "monthly"]
    owner_acc_after_update = await get_account(user_owner.id)
    assert owner_acc_after_update is not None
    assert {lbl.name for lbl in owner_acc_after_update.extra.labels} == {
        "allowance",
        "family-budget",
        "monthly",
    }

    # Execute plan and verify label propagation + cross-user privacy
    execution = await execute_plan(created_plan.id, triggered_by="manual")
    assert execution and execution.status == ExecutionStatus.SUCCESS.value

    child_hash = next(d["payment_hash"] for d in execution.details if d["label"] == "Child")
    peer_hash = next(d["payment_hash"] for d in execution.details if d["label"] == "Contractor")

    # Funding wallet outgoing payments carry plan labels
    out_child = await get_wallet_payment(funding.id, child_hash)
    out_peer = await get_wallet_payment(funding.id, peer_hash)
    assert out_child and out_child.labels == ["allowance", "family-budget", "monthly"]
    assert out_peer and out_peer.labels == ["allowance", "family-budget", "monthly"]

    # Same-user child wallet receives labels too (shares user_owner.extra.labels)
    in_child = await get_wallet_payment(own_child.id, child_hash)
    assert in_child and in_child.labels == ["allowance", "family-budget", "monthly"]

    # Cross-user Peer Wallet NEVER receives sender's private category labels!
    in_peer = await get_wallet_payment(peer_wallet.id, peer_hash)
    assert in_peer and in_peer.labels == []
    peer_acc = await get_account(user_peer.id)
    assert peer_acc and peer_acc.extra.labels == []

    # Search by label in LNbits paginated API finds both outgoing payments
    label_search = await get_payments_paginated(
        wallet_id=funding.id,
        outgoing=True,
        filters=Filters(search="family-budget", model=PaymentFilters),
    )
    assert label_search.total == 2

    # Idempotency check on ensure_account_labels
    synced = await ensure_account_labels(user_owner.id, ["allowance", "monthly"])
    assert len(synced) == 3


@pytest.mark.anyio
async def test_update_plan_nullable_max_sat_limit_and_partial_toggle():
    await migrate_databases()
    async with db.connect() as conn:
        await _migrate(conn)

    user = await create_user_account()
    wallet = await create_wallet(user_id=user.id, wallet_name="Nullable Update Wallet")
    created = await create_plan(
        wallet.id,
        PlanCreate(
            name="Capped Plan",
            max_sat_limit=50000,
            telegram_chat_id="-100999888",
            items=[],
        ),
        calculate_next_run("0 9 * * 5", "UTC"),
    )
    assert created.max_sat_limit == 50000
    assert created.telegram_chat_id == "-100999888"

    # Partial toggle (is_active only) preserves max_sat_limit & telegram_chat_id
    toggled = await update_plan(created.id, PlanUpdate(is_active=False))
    assert toggled is not None
    assert toggled.is_active is False
    assert toggled.max_sat_limit == 50000
    assert toggled.telegram_chat_id == "-100999888"

    # Explicit None clears nullable fields back to NULL
    cleared = await update_plan(
        created.id,
        PlanUpdate(max_sat_limit=None, telegram_chat_id=None),
    )
    assert cleared is not None
    assert cleared.max_sat_limit is None
    assert cleared.telegram_chat_id is None


def test_frontend_schedule_preset_hydration_and_labels_ui_via_node():
    import json
    import shutil
    import subprocess

    from pocketmoney.services import default_label_color

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not available")

    js_path = Path(__file__).resolve().parent.parent / "static" / "js" / "index.js"
    expected_color = default_label_color("vps-ops")

    harness = f"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const code = fs.readFileSync({json.dumps(str(js_path))}, 'utf8');
let appConfig = null;
const apiCalls = [];
const notifications = [];

const sandbox = {{
  console,
  Intl,
  Date,
  Math,
  Number,
  String,
  Boolean,
  Array,
  Object,
  RegExp,
  Promise,
  encodeURIComponent,
  parseInt,
  parseFloat,
  isNaN,
  windowMixin: {{}},
  window: {{
    location: {{ origin: 'https://lnbits.example.com' }},
    g: {{
      user: {{
        wallets: [{{ id: 'w-main', name: 'Main Wallet', adminkey: 'adm-123', balance_msat: 50000000 }}],
        extra: {{ labels: [{{ name: 'existing-lbl', color: '#112233', description: '' }}] }}
      }}
    }}
  }},
  Vue: {{
    createApp(cfg) {{
      appConfig = cfg;
      return cfg;
    }}
  }},
  LNbits: {{
    api: {{
      async request(method, url, key, payload) {{
        apiCalls.push({{ method, url, key, payload: payload ? JSON.parse(JSON.stringify(payload)) : null }});
        if (url.endsWith('/currencies')) return {{ data: [{{ code: 'SAT', name: 'Satoshis' }}] }};
        if (url.endsWith('/plans') && method === 'GET') return {{ data: [] }};
        return {{ data: payload || {{}} }};
      }}
    }},
    utils: {{
      notifyApiError(err) {{ throw err; }},
      copyText() {{}}
    }}
  }},
  Quasar: {{
    Notify: {{
      create(n) {{ notifications.push(n); }}
    }},
    Dialog: {{ create() {{ return {{ onOk() {{}} }}; }} }},
    Loading: {{ show() {{}}, hide() {{}} }},
    date: {{ formatDate(d) {{ return d.toISOString(); }} }}
  }}
}};

vm.runInNewContext(code, sandbox);

// Instantiate component-like VM binding computed & methods onto data()
const state = appConfig.data.call({{}});
const ctx = new Proxy(state, {{
  get(target, prop) {{
    if (prop in target) return target[prop];
    if (appConfig.methods && prop in appConfig.methods) return appConfig.methods[prop].bind(ctx);
    if (appConfig.computed && prop in appConfig.computed) return appConfig.computed[prop].call(ctx);
    return undefined;
  }},
  set(target, prop, value) {{
    target[prop] = value;
    return true;
  }}
}});
ctx.selectedWallet = 'w-main';

(async () => {{
  // 1. Issue #15: Editing a Monthly plan ('0 0 1 * *') must hydrate Monthly presets and preserve cron on Save!
  ctx.openPlanDialog({{
    id: 'plan-monthly-1',
    name: 'Monthly Server Hosting',
    description: 'Monthly servers',
    cadence_type: 'monthly',
    cron_expression: '0 0 1 * *',
    timezone: 'UTC',
    max_sat_limit: 50000,
    low_balance_threshold: 10000,
    telegram_chat_id: null,
    labels: ['vps', 'infra'],
    items: [{{ label: 'Provider A', recipient: 'billing@pay.example.com', recipient_type: 'lightning_address', amount: 10.0, currency: 'USD', memo: '' }}]
  }});
  assert.strictEqual(ctx.planDialog.advancedCron, false);
  assert.strictEqual(ctx.planDialog.presetCadence, 'monthly');
  assert.strictEqual(ctx.planDialog.presetMonthDay, 1);
  assert.strictEqual(ctx.planDialog.presetTime, '00:00');
  assert.deepStrictEqual(Array.from(ctx.planDialog.data.labels), ['vps', 'infra']);

  await ctx.submitPlanForm();
  const putMonthly = apiCalls.find(c => c.method === 'PUT' && c.url.endsWith('/plan-monthly-1'));
  assert(putMonthly, 'Expected PUT call for monthly plan');
  assert.strictEqual(putMonthly.payload.cadence_type, 'monthly');
  assert.strictEqual(putMonthly.payload.cron_expression, '0 0 1 * *');
  assert.deepStrictEqual(putMonthly.payload.labels, ['vps', 'infra']);

  // 2. Monthly Day 15 at 18:30 round-trip + clearing max_sat_limit ('') -> null
  apiCalls.length = 0;
  ctx.openPlanDialog({{
    id: 'plan-monthly-15',
    name: 'Mid-Month Payroll',
    cadence_type: 'monthly',
    cron_expression: '30 18 15 * *',
    timezone: 'UTC',
    max_sat_limit: 25000,
    low_balance_threshold: 5000,
    labels: [],
    items: []
  }});
  assert.strictEqual(ctx.planDialog.presetCadence, 'monthly');
  assert.strictEqual(ctx.planDialog.presetMonthDay, 15);
  assert.strictEqual(ctx.planDialog.presetTime, '18:30');
  // Simulate Quasar clearing optional numeric inputs to ''
  ctx.planDialog.data.max_sat_limit = '';
  ctx.planDialog.data.low_balance_threshold = '';
  await ctx.submitPlanForm();
  const put15 = apiCalls.find(c => c.method === 'PUT' && c.url.endsWith('/plan-monthly-15'));
  assert.strictEqual(put15.payload.cron_expression, '30 18 15 * *');
  assert.strictEqual(put15.payload.max_sat_limit, null);
  assert.strictEqual(put15.payload.low_balance_threshold, 0);

  // 3. Weekly Sunday '7' normalizes to '0' and formatCadence renders 'Sun' (not 'Fri')
  ctx.openPlanDialog({{
    id: 'plan-sun',
    name: 'Sunday Allowance',
    cadence_type: 'weekly',
    cron_expression: '5 8 * * 7',
    timezone: 'UTC',
    items: []
  }});
  assert.strictEqual(ctx.planDialog.advancedCron, false);
  assert.strictEqual(ctx.planDialog.presetCadence, 'weekly');
  assert.strictEqual(ctx.planDialog.presetWeekday, '0');
  assert.strictEqual(ctx.planDialog.presetTime, '08:05');
  const sunBadge = ctx.formatCadence({{ cadence_type: 'weekly', cron_expression: '5 8 * * 7', timezone: 'UTC' }});
  assert(sunBadge.includes('Weekly (Sun 08:05)'), 'Unexpected Sunday badge: ' + sunBadge);

  // 4. Complex multi-day cron ('0 9 1,15 * *') switches to advancedCron=true so 1,15 is never overwritten
  apiCalls.length = 0;
  ctx.openPlanDialog({{
    id: 'plan-bimonthly',
    name: 'Twice Monthly',
    cadence_type: 'monthly',
    cron_expression: '0 9 1,15 * *',
    timezone: 'UTC',
    items: []
  }});
  assert.strictEqual(ctx.planDialog.advancedCron, true);
  const biBadge = ctx.formatCadence({{ cadence_type: 'monthly', cron_expression: '0 9 1,15 * *', timezone: 'UTC' }});
  assert(biBadge.startsWith('Cron: 0 9 1,15 * *'), 'Unexpected complex cron badge: ' + biBadge);
  await ctx.submitPlanForm();
  const putBi = apiCalls.find(c => c.method === 'PUT' && c.url.endsWith('/plan-bimonthly'));
  assert.strictEqual(putBi.payload.cron_expression, '0 9 1,15 * *');

  // 5. Opening '+ New Plan' resets presetMonthDay = 1, presetCadence = 'weekly', and labels = []
  ctx.openPlanDialog();
  assert.strictEqual(ctx.planDialog.advancedCron, false);
  assert.strictEqual(ctx.planDialog.presetCadence, 'weekly');
  assert.strictEqual(ctx.planDialog.presetWeekday, '5');
  assert.strictEqual(ctx.planDialog.presetMonthDay, 1);
  assert.strictEqual(ctx.planDialog.presetTime, '09:00');
  assert.deepStrictEqual(Array.from(ctx.planDialog.data.labels), []);

  // 6. Label input validation & g.user.extra.labels color sync
  let addedVal = null;
  ctx.onCreatePlanLabel('  vps-ops  ', (val, mode) => {{ addedVal = [val, mode]; }});
  assert.deepStrictEqual(addedVal, ['vps-ops', 'add-unique']);

  addedVal = null;
  const prevNotifs = notifications.length;
  ctx.onCreatePlanLabel('invalid/label!', (val, mode) => {{ addedVal = [val, mode]; }});
  assert.strictEqual(addedVal, null);
  assert(notifications.length > prevNotifs, 'Expected warning notification on invalid label');

  ctx.planDialog.data.name = 'New Ops Plan';
  ctx.planDialog.data.labels = ['existing-lbl', 'vps-ops'];
  ctx.planDialog.data.items = [];
  await ctx.submitPlanForm();
  const syncedLabel = ctx.g.user.extra.labels.find(l => l.name === 'vps-ops');
  assert(syncedLabel, 'Expected vps-ops synced into g.user.extra.labels');
  assert.strictEqual(syncedLabel.color, {json.dumps(expected_color)});
}})().catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
    proc = subprocess.run([node_bin, "-e", harness], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Frontend JS harness failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"





