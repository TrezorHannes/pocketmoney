from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional
from uuid import uuid4

from lnbits.db import Database
from lnbits.helpers import urlsafe_short_hash

from .models import (
    Execution,
    Item,
    ItemCreate,
    Plan,
    PlanCreate,
    PlanUpdate,
    PocketMoneySettings,
    RecipientType,
    SettingsUpdate,
)

db = Database("ext_pocketmoney")


# ---------------------------------------------------------------------------
# Plans CRUD
# ---------------------------------------------------------------------------

async def create_plan(wallet_id: str, data: PlanCreate, next_run_at: datetime) -> Plan:
    plan_id = urlsafe_short_hash()
    webhook_token = uuid4().hex

    await db.execute(
        f"""
        INSERT INTO {db.references_schema}plans (
            id, wallet_id, name, description, cadence_type, cron_expression,
            timezone, is_active, max_sat_limit, webhook_token,
            low_balance_threshold, telegram_chat_id, next_run_at
        ) VALUES (
            :id, :wallet_id, :name, :description, :cadence_type, :cron_expression,
            :timezone, :is_active, :max_sat_limit, :webhook_token,
            :low_balance_threshold, :telegram_chat_id, :next_run_at
        )
        """,
        {
            "id": plan_id,
            "wallet_id": wallet_id,
            "name": data.name,
            "description": data.description,
            "cadence_type": str(data.cadence_type.value if hasattr(data.cadence_type, "value") else data.cadence_type),
            "cron_expression": data.cron_expression,
            "timezone": data.timezone,
            "is_active": True,
            "max_sat_limit": data.max_sat_limit,
            "webhook_token": webhook_token,
            "low_balance_threshold": data.low_balance_threshold,
            "telegram_chat_id": data.telegram_chat_id,
            "next_run_at": next_run_at,
        },
    )

    # Insert items
    items: List[Item] = []
    for item_data in data.items:
        item = await create_item(plan_id, item_data)
        items.append(item)

    plan = await get_plan(plan_id)
    assert plan, "Plan was not created"
    plan.items = items
    return plan


async def get_plan(plan_id: str, wallet_id: Optional[str] = None) -> Optional[Plan]:
    query = f"SELECT * FROM {db.references_schema}plans WHERE id = :id"
    values: dict[str, Any] = {"id": plan_id}
    if wallet_id:
        query += " AND wallet_id = :wallet_id"
        values["wallet_id"] = wallet_id

    plan = await db.fetchone(query, values, model=Plan)
    if not plan:
        return None

    plan.items = await get_items_for_plan(plan.id)
    return plan


async def get_plan_by_webhook(webhook_token: str) -> Optional[Plan]:
    plan = await db.fetchone(
        f"SELECT * FROM {db.references_schema}plans WHERE webhook_token = :token",
        {"token": webhook_token},
        model=Plan,
    )
    if not plan:
        return None
    plan.items = await get_items_for_plan(plan.id)
    return plan


async def get_plans(wallet_id: str) -> List[Plan]:
    plans = await db.fetchall(
        f"SELECT * FROM {db.references_schema}plans WHERE wallet_id = :wallet_id ORDER BY created_at DESC",
        {"wallet_id": wallet_id},
        model=Plan,
    )
    for plan in plans:
        plan.items = await get_items_for_plan(plan.id)
    return plans


async def get_due_plans(now: datetime) -> List[Plan]:
    rows = await db.fetchall(
        f"""
        SELECT * FROM {db.references_schema}plans
        WHERE is_active = TRUE AND next_run_at <= :now
        ORDER BY next_run_at ASC
        """,
        {"now": now},
        model=Plan,
    )
    for plan in plans:
        plan.items = await get_items_for_plan(plan.id)
    return plans


async def update_plan(plan_id: str, data: PlanUpdate, next_run_at: Optional[datetime] = None) -> Optional[Plan]:
    existing = await get_plan(plan_id)
    if not existing:
        return None

    fields: dict[str, Any] = {}
    if data.name is not None:
        fields["name"] = data.name
    if data.description is not None:
        fields["description"] = data.description
    if data.cadence_type is not None:
        fields["cadence_type"] = str(data.cadence_type.value if hasattr(data.cadence_type, "value") else data.cadence_type)
    if data.cron_expression is not None:
        fields["cron_expression"] = data.cron_expression
    if data.timezone is not None:
        fields["timezone"] = data.timezone
    if data.is_active is not None:
        fields["is_active"] = data.is_active
    if data.max_sat_limit is not None:
        fields["max_sat_limit"] = data.max_sat_limit
    if data.low_balance_threshold is not None:
        fields["low_balance_threshold"] = data.low_balance_threshold
    if data.telegram_chat_id is not None:
        fields["telegram_chat_id"] = data.telegram_chat_id
    if next_run_at is not None:
        fields["next_run_at"] = next_run_at

    if fields:
        set_clause = ", ".join(f"{k} = :{k}" for k in fields)
        fields["id"] = plan_id
        await db.execute(
            f"UPDATE {db.references_schema}plans SET {set_clause} WHERE id = :id",
            fields,
        )

    if data.items is not None:
        await delete_items_for_plan(plan_id)
        for item_data in data.items:
            await create_item(plan_id, item_data)

    return await get_plan(plan_id)


async def update_plan_execution(plan_id: str, last_run_at: datetime, next_run_at: datetime) -> None:
    await db.execute(
        f"""
        UPDATE {db.references_schema}plans
        SET last_run_at = :last_run_at, next_run_at = :next_run_at
        WHERE id = :id
        """,
        {
            "id": plan_id,
            "last_run_at": last_run_at,
            "next_run_at": next_run_at,
        },
    )


async def delete_plan(plan_id: str, wallet_id: str) -> bool:
    await delete_items_for_plan(plan_id)
    await db.execute(
        f"DELETE FROM {db.references_schema}plans WHERE id = :id AND wallet_id = :wallet_id",
        {"id": plan_id, "wallet_id": wallet_id},
    )
    return True


async def claim_plan_running(plan_id: str) -> bool:
    """
    Atomically claim a plan for execution by setting is_running = TRUE.

    Only succeeds when is_running is currently FALSE, preventing concurrent
    execution across multiple LNbits worker processes.

    Returns True if the claim succeeded (this process owns the lock),
    False if another process already claimed it.
    """
    result = await db.execute(
        f"""
        UPDATE {db.references_schema}plans
        SET is_running = TRUE
        WHERE id = :id AND is_running = FALSE
        """,
        {"id": plan_id},
    )
    # rowcount == 1 means we won the race; 0 means another worker claimed it
    return (getattr(result, "rowcount", None) or 0) >= 1


async def release_plan_running(plan_id: str) -> None:
    """Release the is_running claim so subsequent executions can proceed."""
    await db.execute(
        f"UPDATE {db.references_schema}plans SET is_running = FALSE WHERE id = :id",
        {"id": plan_id},
    )



# ---------------------------------------------------------------------------
# Items CRUD
# ---------------------------------------------------------------------------

async def create_item(plan_id: str, data: ItemCreate) -> Item:
    item_id = urlsafe_short_hash()

    # Simple destination sniffing
    target = data.recipient.strip()
    if "@" in target and not target.startswith("lnurl"):
        rec_type = RecipientType.LIGHTNING_ADDRESS.value
    elif target.lower().startswith("lnurl"):
        rec_type = RecipientType.LNURL.value
    else:
        rec_type = RecipientType.INTERNAL.value

    await db.execute(
        f"""
        INSERT INTO {db.references_schema}items (
            id, plan_id, label, recipient, recipient_type, amount, currency, memo, max_sat_limit
        ) VALUES (
            :id, :plan_id, :label, :recipient, :recipient_type, :amount, :currency, :memo, :max_sat_limit
        )
        """,
        {
            "id": item_id,
            "plan_id": plan_id,
            "label": data.label,
            "recipient": target,
            "recipient_type": rec_type,
            # str() keeps NUMERIC exact on Postgres; aiosqlite cannot bind Decimal.
            "amount": str(data.amount),
            "currency": data.currency.upper(),
            "memo": data.memo,
            "max_sat_limit": data.max_sat_limit,
        },
    )

    row = await db.fetchone(
        f"SELECT * FROM {db.references_schema}items WHERE id = :id",
        {"id": item_id},
    )
    assert row, "Item creation failed"
    return Item(**dict(row))


async def get_items_for_plan(plan_id: str) -> List[Item]:
    rows = await db.fetchall(
        f"SELECT * FROM {db.references_schema}items WHERE plan_id = :plan_id ORDER BY created_at ASC",
        {"plan_id": plan_id},
    )
    return [Item(**dict(r)) for r in rows]


async def delete_items_for_plan(plan_id: str) -> None:
    await db.execute(
        f"DELETE FROM {db.references_schema}items WHERE plan_id = :plan_id",
        {"plan_id": plan_id},
    )


# ---------------------------------------------------------------------------
# Executions Audit CRUD
# ---------------------------------------------------------------------------

async def create_execution(
    wallet_id: str,
    plan_id: str,
    triggered_by: str,
    status: str,
    total_sats: int,
    total_fees_msat: int,
    details: List[dict[str, Any]],
    error_message: Optional[str] = None,
) -> Execution:
    exec_id = urlsafe_short_hash()
    details_json = json.dumps(details)

    await db.execute(
        f"""
        INSERT INTO {db.references_schema}executions (
            id, plan_id, wallet_id, triggered_by, status, total_sats, total_fees_msat, details, error_message
        ) VALUES (
            :id, :plan_id, :wallet_id, :triggered_by, :status, :total_sats, :total_fees_msat, :details, :error_message
        )
        """,
        {
            "id": exec_id,
            "plan_id": plan_id,
            "wallet_id": wallet_id,
            "triggered_by": triggered_by,
            "status": status,
            "total_sats": total_sats,
            "total_fees_msat": total_fees_msat,
            "details": details_json,
            "error_message": error_message,
        },
    )

    execution = await db.fetchone(
        f"SELECT * FROM {db.references_schema}executions WHERE id = :id",
        {"id": exec_id},
        model=Execution,
    )
    assert execution, "Execution log failed"
    return execution


async def get_executions(wallet_id: str, plan_id: Optional[str] = None, limit: int = 50) -> List[Execution]:
    query = f"SELECT * FROM {db.references_schema}executions WHERE wallet_id = :wallet_id"
    values: dict[str, Any] = {"wallet_id": wallet_id}
    if plan_id:
        query += " AND plan_id = :plan_id"
        values["plan_id"] = plan_id

    query += " ORDER BY executed_at DESC LIMIT :limit"
    values["limit"] = limit

    return await db.fetchall(query, values, model=Execution)


# ---------------------------------------------------------------------------
# Settings CRUD
# ---------------------------------------------------------------------------

async def get_settings(wallet_id: str) -> PocketMoneySettings:
    row = await db.fetchone(
        f"SELECT * FROM {db.references_schema}settings WHERE wallet_id = :wallet_id",
        {"wallet_id": wallet_id},
    )
    if not row:
        return PocketMoneySettings(wallet_id=wallet_id)
    return PocketMoneySettings(**dict(row))


async def update_settings(wallet_id: str, data: SettingsUpdate) -> PocketMoneySettings:
    current = await get_settings(wallet_id)
    bot_token = data.telegram_bot_token if data.telegram_bot_token is not None else current.telegram_bot_token
    chat_id = data.telegram_chat_id if data.telegram_chat_id is not None else current.telegram_chat_id
    notif_succ = data.notify_on_success if data.notify_on_success is not None else current.notify_on_success
    notif_fail = data.notify_on_failure if data.notify_on_failure is not None else current.notify_on_failure
    notif_low = data.notify_on_low_balance if data.notify_on_low_balance is not None else current.notify_on_low_balance

    await db.execute(
        f"""
        INSERT INTO {db.references_schema}settings (
            wallet_id, telegram_bot_token, telegram_chat_id, notify_on_success, notify_on_failure, notify_on_low_balance
        ) VALUES (
            :wallet_id, :token, :chat_id, :succ, :fail, :low
        )
        ON CONFLICT(wallet_id) DO UPDATE SET
            telegram_bot_token = :token,
            telegram_chat_id = :chat_id,
            notify_on_success = :succ,
            notify_on_failure = :fail,
            notify_on_low_balance = :low
        """,
        {
            "wallet_id": wallet_id,
            "token": bot_token,
            "chat_id": chat_id,
            "succ": notif_succ,
            "fail": notif_fail,
            "low": notif_low,
        },
    )

    return await get_settings(wallet_id)
