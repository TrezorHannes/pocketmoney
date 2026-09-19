from __future__ import annotations

from http import HTTPStatus
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from lnbits.decorators import WalletTypeInfo, require_admin_key
from lnbits.settings import settings
from lnbits.utils.exchange_rates import currencies
from loguru import logger

from .crud import (
    create_plan,
    delete_plan,
    get_executions,
    get_plan,
    get_plan_by_webhook,
    get_plans,
    get_settings,
    update_plan,
    update_settings,
)
from .models import (
    Execution,
    Plan,
    PlanCreate,
    PlanUpdate,
    PocketMoneySettings,
    SettingsUpdate,
    SimulatePlanResponse,
    TestTelegramRequest,
    TriggerType,
)
from .services import (
    calculate_next_run,
    execute_plan,
    send_telegram_alert,
    simulate_plan,
)

pocketmoney_api_router = APIRouter()


# ---------------------------------------------------------------------------
# Supported Currencies Endpoint
# ---------------------------------------------------------------------------

@pocketmoney_api_router.get(
    "/api/v1/currencies",
    description="Get allowed and available currencies for PocketMoney disbursements",
)
async def api_get_currencies() -> list[dict[str, str]]:
    allowed = settings.lnbits_allowed_currencies
    res = [{"code": "SAT", "name": "Satoshis"}]

    if allowed:
        for code in allowed:
            c = code.upper()
            if c != "SAT":
                res.append({"code": c, "name": currencies.get(c, c)})
    else:
        # If no specific currencies restricted by admin, provide common ones
        common = ["EUR", "USD", "GBP", "CHF", "CAD", "AUD", "JPY", "BRL"]
        for c in common:
            res.append({"code": c, "name": currencies.get(c, c)})

    return res


# ---------------------------------------------------------------------------
# Plans CRUD Endpoints
# ---------------------------------------------------------------------------

@pocketmoney_api_router.get(
    "/api/v1/plans",
    response_model=List[Plan],
    description="List all disbursement plans for the current wallet",
)
async def api_get_plans(
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> List[Plan]:
    return await get_plans(wallet.wallet.id)


@pocketmoney_api_router.post(
    "/api/v1/plans",
    response_model=Plan,
    status_code=HTTPStatus.CREATED,
    description="Create a new disbursement plan",
)
async def api_create_plan(
    data: PlanCreate,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> Plan:
    try:
        next_run = calculate_next_run(
            str(data.cadence_type.value if hasattr(data.cadence_type, "value") else data.cadence_type),
            data.cron_expression,
            data.timezone,
        )
        return await create_plan(wallet.wallet.id, data, next_run)
    except Exception as exc:
        logger.error(f"Failed to create plan: {exc}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"Could not create plan: {exc}",
        )


@pocketmoney_api_router.get(
    "/api/v1/plans/{plan_id}",
    response_model=Plan,
    description="Get a single disbursement plan",
)
async def api_get_plan(
    plan_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> Plan:
    plan = await get_plan(plan_id, wallet.wallet.id)
    if not plan:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Plan not found")
    return plan


@pocketmoney_api_router.put(
    "/api/v1/plans/{plan_id}",
    response_model=Plan,
    description="Update an existing disbursement plan",
)
async def api_update_plan(
    plan_id: str,
    data: PlanUpdate,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> Plan:
    plan = await get_plan(plan_id, wallet.wallet.id)
    if not plan:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Plan not found")

    next_run = None
    if data.cadence_type or data.cron_expression or data.timezone:
        cadence = data.cadence_type or plan.cadence_type
        cron_expr = data.cron_expression or plan.cron_expression
        tz = data.timezone or plan.timezone
        next_run = calculate_next_run(
            str(cadence.value if hasattr(cadence, "value") else cadence),
            cron_expr,
            tz,
        )

    updated = await update_plan(plan_id, data, next_run)
    assert updated, "Update failed"
    return updated


@pocketmoney_api_router.delete(
    "/api/v1/plans/{plan_id}",
    status_code=HTTPStatus.OK,
    description="Delete a disbursement plan",
)
async def api_delete_plan(
    plan_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> dict[str, bool]:
    plan = await get_plan(plan_id, wallet.wallet.id)
    if not plan:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Plan not found")

    await delete_plan(plan_id, wallet.wallet.id)
    return {"success": True}


# ---------------------------------------------------------------------------
# Plan Execution & Dry-Run Simulation
# ---------------------------------------------------------------------------

@pocketmoney_api_router.post(
    "/api/v1/plans/{plan_id}/run",
    response_model=Execution,
    description="Manually trigger an immediate execution of a plan ('Pay Now')",
)
async def api_run_plan(
    plan_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> Execution:
    plan = await get_plan(plan_id, wallet.wallet.id)
    if not plan:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Plan not found")

    try:
        return await execute_plan(plan_id, triggered_by=TriggerType.MANUAL.value)
    except Exception as exc:
        logger.error(f"Manual plan execution error: {exc}")
        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            detail=f"Execution error: {exc}",
        )


@pocketmoney_api_router.post(
    "/api/v1/plans/{plan_id}/simulate",
    response_model=SimulatePlanResponse,
    description="Dry-run simulation of plan amounts and recipient validation",
)
async def api_simulate_plan(
    plan_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> SimulatePlanResponse:
    try:
        return await simulate_plan(plan_id, wallet.wallet.id)
    except Exception as exc:
        logger.error(f"Simulation error: {exc}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"Simulation error: {exc}",
        )


# ---------------------------------------------------------------------------
# History & Audit Logs
# ---------------------------------------------------------------------------

@pocketmoney_api_router.get(
    "/api/v1/history",
    response_model=List[Execution],
    description="Get execution audit history for the current wallet",
)
async def api_get_history(
    plan_id: Optional[str] = Query(None, description="Optional plan ID filter"),
    limit: int = Query(50, ge=1, le=200),
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> List[Execution]:
    return await get_executions(wallet.wallet.id, plan_id=plan_id, limit=limit)


# ---------------------------------------------------------------------------
# Settings & Telegram Alerts
# ---------------------------------------------------------------------------

@pocketmoney_api_router.get(
    "/api/v1/settings",
    response_model=PocketMoneySettings,
    description="Get PocketMoney settings for current wallet",
)
async def api_get_settings(
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> PocketMoneySettings:
    return await get_settings(wallet.wallet.id)


@pocketmoney_api_router.put(
    "/api/v1/settings",
    response_model=PocketMoneySettings,
    description="Update PocketMoney settings for current wallet",
)
async def api_update_settings(
    data: SettingsUpdate,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> PocketMoneySettings:
    return await update_settings(wallet.wallet.id, data)


@pocketmoney_api_router.post(
    "/api/v1/settings/test-telegram",
    description="Send a test message via Telegram to verify bot configuration",
)
async def api_test_telegram(
    data: TestTelegramRequest,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> dict[str, Any]:
    msg = (
        "🤖 <b>PocketMoney Extension:</b> Telegram Alert Test\n"
        f"Connected successfully for wallet <code>{wallet.wallet.name}</code>!"
    )
    success = await send_telegram_alert(data.telegram_bot_token, data.telegram_chat_id, msg)
    if not success:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="Failed to send Telegram message. Please check your bot token and chat ID.",
        )
    return {"success": True, "message": "Telegram message delivered successfully!"}


# ---------------------------------------------------------------------------
# Unauthenticated Webhook Trigger Endpoint
# ---------------------------------------------------------------------------

@pocketmoney_api_router.post(
    "/api/v1/webhook/{webhook_token}",
    description="External unauthenticated trigger endpoint using secret token",
)
async def api_webhook_trigger(webhook_token: str) -> dict[str, Any]:
    plan = await get_plan_by_webhook(webhook_token)
    if not plan:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Invalid or unknown webhook token.",
        )

    if not plan.is_active:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Disbursement plan is currently paused/inactive.",
        )

    execution = await execute_plan(plan.id, triggered_by=TriggerType.WEBHOOK.value)
    return {
        "status": execution.status,
        "plan": plan.name,
        "total_sats": execution.total_sats,
        "error": execution.error_message,
        "executed_at": execution.executed_at,
    }
