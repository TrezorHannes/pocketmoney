from __future__ import annotations

import asyncio
import zoneinfo
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Optional

import httpx
from lnbits.core.crud import get_wallet
from lnbits.core.services import create_invoice, get_pr_from_lnurl, pay_invoice
from lnbits.utils.exchange_rates import fiat_amount_as_satoshis
from loguru import logger

from .crud import (
    create_execution,
    get_items_for_plan,
    get_plan,
    get_settings,
    update_plan_execution,
)
from .models import (
    Execution,
    ExecutionStatus,
    Item,
    SimulateItemResult,
    SimulatePlanResponse,
    TriggerType,
)

# Concurrency locks per plan ID to prevent duplicate executions
_plan_locks: dict[str, asyncio.Lock] = {}


def _get_plan_lock(plan_id: str) -> asyncio.Lock:
    if plan_id not in _plan_locks:
        _plan_locks[plan_id] = asyncio.Lock()
    return _plan_locks[plan_id]


# ---------------------------------------------------------------------------
# Cron & Recurrence Calculation (Zero-Dependency)
# ---------------------------------------------------------------------------

def _parse_cron_field(field: str, min_val: int, max_val: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if "/" in part:
            sub, step_str = part.split("/", 1)
            step = max(1, int(step_str))
            if sub == "*":
                start, end = min_val, max_val
            elif "-" in sub:
                start_str, end_str = sub.split("-", 1)
                start, end = int(start_str), int(end_str)
            else:
                start, end = int(sub), max_val
            for v in range(start, end + 1, step):
                values.add(v)
        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            for v in range(int(start_str), int(end_str) + 1):
                values.add(v)
        elif part == "*":
            for v in range(min_val, max_val + 1):
                values.add(v)
        else:
            values.add(int(part))
    return values


def calculate_next_run(
    cadence_type: str,
    cron_expression: str,
    tz_name: str = "UTC",
    from_time: Optional[datetime] = None,
) -> datetime:
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc

    now = from_time or datetime.now(timezone.utc)
    now_local = now.astimezone(tz)

    parts = cron_expression.strip().split()
    if len(parts) != 5:
        # Fallback to weekly Friday 09:00 if invalid
        parts = ["0", "9", "*", "*", "5"]

    mins = _parse_cron_field(parts[0], 0, 59)
    hours = _parse_cron_field(parts[1], 0, 23)
    days = _parse_cron_field(parts[2], 1, 31)
    months = _parse_cron_field(parts[3], 1, 12)

    # In standard cron: 0=Sun, 1=Mon, ..., 5=Fri, 6=Sat, 7=Sun
    # Map to Python weekday (0=Mon ... 6=Sun)
    cron_weekdays = _parse_cron_field(parts[4], 0, 7)
    py_weekdays: set[int] = set()
    for d in cron_weekdays:
        if d == 0 or d == 7:
            py_weekdays.add(6)  # Sunday
        else:
            py_weekdays.add(d - 1)

    cur = now_local.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # Search up to 366 days ahead
    for _ in range(60 * 24 * 366):
        if (
            cur.month in months
            and cur.day in days
            and cur.weekday() in py_weekdays
            and cur.hour in hours
            and cur.minute in mins
        ):
            return cur.astimezone(timezone.utc)
        cur += timedelta(minutes=1)

    # Fallback to 7 days ahead
    return (now + timedelta(days=7)).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Currency Conversion
# ---------------------------------------------------------------------------

async def amount_to_satoshis(amount: Decimal, currency: str) -> int:
    curr = currency.upper().strip()
    if curr in {"SAT", "SATS", "SATOSHI", "SATOSHIS"}:
        return int(amount)

    try:
        sats = await fiat_amount_as_satoshis(float(amount), curr)
        return max(1, int(sats))
    except Exception as exc:
        logger.error(f"Error converting {amount} {curr} to sats: {exc}")
        raise ValueError(f"Could not convert {amount} {curr} to satoshis: {exc}") from exc


# ---------------------------------------------------------------------------
# Telegram Notifications
# ---------------------------------------------------------------------------

async def send_telegram_alert(
    bot_token: Optional[str],
    chat_id: Optional[str],
    message: str,
) -> bool:
    if not bot_token or not chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
            return resp.status_code == 200
    except Exception as exc:
        logger.warning(f"Failed to dispatch Telegram message: {exc}")
        return False


# ---------------------------------------------------------------------------
# Simulation / Dry Run
# ---------------------------------------------------------------------------

async def simulate_plan(plan_id: str, wallet_id: str) -> SimulatePlanResponse:
    plan = await get_plan(plan_id, wallet_id)
    if not plan:
        raise ValueError("Plan not found")

    wallet = await get_wallet(wallet_id)
    if not wallet:
        raise ValueError("Source wallet not found")

    current_balance_sats = wallet.balance_msat // 1000
    items = await get_items_for_plan(plan.id)

    sim_items: List[SimulateItemResult] = []
    total_sats = 0
    warnings: List[str] = []

    for item in items:
        valid = True
        warning = None
        sats = 0
        try:
            sats = await amount_to_satoshis(item.amount, item.currency)
            total_sats += sats

            if item.max_sat_limit and sats > item.max_sat_limit:
                warning = f"Exceeds max sat limit ({sats} > {item.max_sat_limit} sats)"
                valid = False
                warnings.append(f"{item.label}: {warning}")

            # Check recipient validity
            target = item.recipient.strip()
            dest_wallet = await get_wallet(target)
            if not dest_wallet and not ("@" in target or target.lower().startswith("lnurl")):
                warning = "Recipient is not an internal wallet or valid Lightning Address"
                valid = False
                warnings.append(f"{item.label}: {warning}")

        except Exception as e:
            valid = False
            warning = str(e)
            warnings.append(f"{item.label}: {warning}")

        sim_items.append(
            SimulateItemResult(
                label=item.label,
                recipient=item.recipient,
                recipient_type=item.recipient_type,
                amount=item.amount,
                currency=item.currency,
                estimated_sats=sats,
                valid=valid,
                warning=warning,
            )
        )

    if plan.max_sat_limit and total_sats > plan.max_sat_limit:
        warnings.append(
            f"Plan total ({total_sats} sats) exceeds plan budget ceiling ({plan.max_sat_limit} sats)"
        )

    balance_after = current_balance_sats - total_sats
    if balance_after < 0:
        warnings.append(
            f"Insufficient wallet balance! Needed: {total_sats} sats, Available: {current_balance_sats} sats"
        )

    can_execute = len(warnings) == 0 and all(i.valid for i in sim_items)

    return SimulatePlanResponse(
        can_execute=can_execute,
        total_sats=total_sats,
        current_balance_sats=current_balance_sats,
        balance_after_sats=balance_after,
        items=sim_items,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Core Execution Engine
# ---------------------------------------------------------------------------

async def execute_plan(
    plan_id: str,
    triggered_by: str = TriggerType.DAEMON.value,
) -> Execution:
    lock = _get_plan_lock(plan_id)
    if lock.locked():
        logger.warning(f"Plan {plan_id} is already executing, skipping concurrent run.")
        return await create_execution(
            wallet_id="unknown",
            plan_id=plan_id,
            triggered_by=triggered_by,
            status=ExecutionStatus.FAILED.value,
            total_sats=0,
            total_fees_msat=0,
            details=[],
            error_message="Plan is already running concurrently.",
        )

    async with lock:
        plan = await get_plan(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")

        wallet = await get_wallet(plan.wallet_id)
        if not wallet:
            raise ValueError(f"Funding wallet {plan.wallet_id} not found")

        current_balance_sats = wallet.balance_msat // 1000
        settings = await get_settings(plan.wallet_id)
        bot_token = settings.telegram_bot_token
        chat_id = plan.telegram_chat_id or settings.telegram_chat_id

        items = await get_items_for_plan(plan.id)
        if not items:
            logger.info(f"Plan {plan.name} ({plan_id}) has no line items.")
            # Advance next run time
            next_run = calculate_next_run(plan.cadence_type, plan.cron_expression, plan.timezone)
            await update_plan_execution(plan.id, datetime.now(timezone.utc), next_run)
            return await create_execution(
                wallet_id=plan.wallet_id,
                plan_id=plan.id,
                triggered_by=triggered_by,
                status=ExecutionStatus.SUCCESS.value,
                total_sats=0,
                total_fees_msat=0,
                details=[],
                error_message="No line items in plan.",
            )

        # Stage 1: Pre-calculate satoshi costs and verify safety limits
        item_conversions: list[tuple[Item, int]] = []
        total_sats_needed = 0

        for item in items:
            try:
                sats = await amount_to_satoshis(item.amount, item.currency)
                if item.max_sat_limit and sats > item.max_sat_limit:
                    err = f"Item {item.label} calculated {sats} sats, exceeding limit of {item.max_sat_limit}"
                    logger.warning(err)
                    if settings.notify_on_failure:
                        await send_telegram_alert(bot_token, chat_id, f"⚠️ <b>PocketMoney Skipped:</b> {err}")
                    return await create_execution(
                        wallet_id=plan.wallet_id,
                        plan_id=plan.id,
                        triggered_by=triggered_by,
                        status=ExecutionStatus.SKIPPED_SLIPPAGE.value,
                        total_sats=0,
                        total_fees_msat=0,
                        details=[],
                        error_message=err,
                    )
                item_conversions.append((item, sats))
                total_sats_needed += sats
            except Exception as e:
                err = f"Conversion error for {item.label}: {e}"
                logger.error(err)
                if settings.notify_on_failure:
                    await send_telegram_alert(bot_token, chat_id, f"❌ <b>PocketMoney Failed:</b> {err}")
                return await create_execution(
                    wallet_id=plan.wallet_id,
                    plan_id=plan.id,
                    triggered_by=triggered_by,
                    status=ExecutionStatus.FAILED.value,
                    total_sats=0,
                    total_fees_msat=0,
                    details=[],
                    error_message=err,
                )

        if plan.max_sat_limit and total_sats_needed > plan.max_sat_limit:
            err = f"Plan total {total_sats_needed} sats exceeds plan ceiling {plan.max_sat_limit}"
            logger.warning(err)
            if settings.notify_on_failure:
                await send_telegram_alert(bot_token, chat_id, f"⚠️ <b>PocketMoney Skipped:</b> {err}")
            return await create_execution(
                wallet_id=plan.wallet_id,
                plan_id=plan.id,
                triggered_by=triggered_by,
                status=ExecutionStatus.SKIPPED_SLIPPAGE.value,
                total_sats=0,
                total_fees_msat=0,
                details=[],
                error_message=err,
            )

        # Stage 2: Verify balance sufficiency (Fail-Closed / All-or-Nothing)
        if current_balance_sats < total_sats_needed:
            err = (
                f"Insufficient balance in wallet! Needed: {total_sats_needed} sats, "
                f"Available: {current_balance_sats} sats."
            )
            logger.warning(f"Plan {plan.name} skipped: {err}")
            if settings.notify_on_low_balance or settings.notify_on_failure:
                await send_telegram_alert(
                    bot_token,
                    chat_id,
                    f"⚠️ <b>PocketMoney Low Balance:</b> {err} (Plan: {plan.name})",
                )
            return await create_execution(
                wallet_id=plan.wallet_id,
                plan_id=plan.id,
                triggered_by=triggered_by,
                status=ExecutionStatus.SKIPPED_INSUFFICIENT_FUNDS.value,
                total_sats=0,
                total_fees_msat=0,
                details=[],
                error_message=err,
            )

        # Stage 3: Execute payments
        details: list[dict[str, Any]] = []
        total_spent_sats = 0
        total_fees_msat = 0
        all_succeeded = True
        first_error: Optional[str] = None

        telegram_lines: list[str] = [
            f"💶 🤑 <b>PocketMoney Executed:</b> {plan.name}",
            f"<b>Source Wallet:</b> {wallet.name}",
        ]

        for item, sats in item_conversions:
            item_detail: dict[str, Any] = {
                "item_id": item.id,
                "label": item.label,
                "recipient": item.recipient,
                "amount_fiat": float(item.amount),
                "currency": item.currency,
                "amount_sats": sats,
                "fee_msat": 0,
                "payment_hash": None,
                "status": "pending",
                "error": None,
            }

            memo = item.memo or f"PocketMoney: {plan.name} -> {item.label}"
            target = item.recipient.strip()

            try:
                # Check if target is an internal wallet
                dest_wallet = await get_wallet(target)
                if dest_wallet:
                    # Internal zero-fee instant transfer
                    logger.debug(f"Executing internal transfer to {item.label} ({target}) for {sats} sats")
                    inv_res = await create_invoice(
                        wallet_id=target,
                        amount=sats,
                        memo=memo,
                    )
                    bolt11 = inv_res.bolt11 if hasattr(inv_res, "bolt11") else inv_res[1]
                    payment = await pay_invoice(
                        wallet_id=plan.wallet_id,
                        payment_request=bolt11,
                    )
                    payment_hash = payment.payment_hash if hasattr(payment, "payment_hash") else str(payment)
                    fee_msat = getattr(payment, "fee", 0) or 0
                    total_fees_msat += fee_msat
                    item_detail["status"] = "success"
                    item_detail["payment_hash"] = payment_hash
                    item_detail["fee_msat"] = fee_msat
                    total_spent_sats += sats
                    telegram_lines.append(
                        f"• Transferred <b>{item.amount} {item.currency}</b> ({sats:,} sats) to <b>{item.label}</b> (internal)"
                    )
                else:
                    # External Lightning Address or LNURL-pay
                    logger.debug(f"Resolving external payment to {item.label} ({target}) for {sats} sats")
                    bolt11 = await get_pr_from_lnurl(
                        target,
                        amount_msat=sats * 1000,
                        comment=memo,
                    )
                    payment = await pay_invoice(
                        wallet_id=plan.wallet_id,
                        payment_request=bolt11,
                    )
                    payment_hash = payment.payment_hash if hasattr(payment, "payment_hash") else str(payment)
                    fee_msat = getattr(payment, "fee", 0) or 0
                    total_fees_msat += fee_msat
                    item_detail["status"] = "success"
                    item_detail["payment_hash"] = payment_hash
                    item_detail["fee_msat"] = fee_msat
                    total_spent_sats += sats
                    telegram_lines.append(
                        f"• Sent <b>{item.amount} {item.currency}</b> ({sats:,} sats) to <b>{item.label}</b> ({target})"
                    )

            except Exception as pay_err:
                logger.error(f"Payment failed for item {item.label} ({target}): {pay_err}")
                item_detail["status"] = "failed"
                item_detail["error"] = str(pay_err)
                all_succeeded = False
                if not first_error:
                    first_error = str(pay_err)
                telegram_lines.append(
                    f"• ❌ Failed <b>{item.label}</b>: {pay_err}"
                )

            details.append(item_detail)

        # Stage 4: Post-execution accounting & next run scheduling
        now_utc = datetime.now(timezone.utc)
        next_run = calculate_next_run(plan.cadence_type, plan.cron_expression, plan.timezone)
        await update_plan_execution(plan.id, now_utc, next_run)

        exec_status = ExecutionStatus.SUCCESS.value if all_succeeded else ExecutionStatus.FAILED.value
        execution = await create_execution(
            wallet_id=plan.wallet_id,
            plan_id=plan.id,
            triggered_by=triggered_by,
            status=exec_status,
            total_sats=total_spent_sats,
            total_fees_msat=total_fees_msat,
            details=details,
            error_message=first_error,
        )

        # Stage 5: Check low-balance threshold warning after payments
        updated_wallet = await get_wallet(plan.wallet_id)
        if updated_wallet:
            remaining_sats = updated_wallet.balance_msat // 1000
            if plan.low_balance_threshold > 0 and remaining_sats < plan.low_balance_threshold:
                low_msg = (
                    f"⚠️ <b>Low Balance Alert:</b> Wallet balance ({remaining_sats:,} sats) "
                    f"is below threshold ({plan.low_balance_threshold:,} sats)!"
                )
                telegram_lines.append(f"\n{low_msg}")

        # Send Telegram notification
        if (all_succeeded and settings.notify_on_success) or (not all_succeeded and settings.notify_on_failure):
            await send_telegram_alert(bot_token, chat_id, "\n".join(telegram_lines))

        return execution
