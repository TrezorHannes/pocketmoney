import asyncio
from datetime import datetime, timezone

from loguru import logger

from .crud import get_due_plans
from .models import TriggerType
from .services import execute_plan


async def run_pocketmoney_scheduler():
    """
    Background worker daemon running periodically inside LNbits.
    Polls the database for active plans where next_run_at <= now,
    and executes them.
    """
    logger.info("Starting PocketMoney background scheduler daemon...")
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            due_plans = await get_due_plans(now_utc)
            if due_plans:
                logger.info(f"PocketMoney: Found {len(due_plans)} due disbursement plan(s).")
                for plan in due_plans:
                    try:
                        logger.info(f"PocketMoney: Executing plan '{plan.name}' ({plan.id})...")
                        execution = await execute_plan(plan.id, triggered_by=TriggerType.DAEMON.value)
                        logger.info(
                            f"PocketMoney: Plan '{plan.name}' executed with status '{execution.status}', "
                            f"total spent: {execution.total_sats:,} sats."
                        )
                    except Exception as plan_exc:
                        logger.error(f"PocketMoney: Error executing plan '{plan.name}' ({plan.id}): {plan_exc}")
        except Exception as exc:
            logger.error(f"PocketMoney scheduler daemon error: {exc}")

        # Poll every 30 seconds
        await asyncio.sleep(30)
