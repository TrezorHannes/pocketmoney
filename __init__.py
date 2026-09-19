import asyncio

from fastapi import APIRouter
from loguru import logger

from .crud import db
from .tasks import run_pocketmoney_scheduler
from .views import pocketmoney_generic_router
from .views_api import pocketmoney_api_router

pocketmoney_ext: APIRouter = APIRouter(prefix="/pocketmoney", tags=["pocketmoney"])
pocketmoney_ext.include_router(pocketmoney_generic_router)
pocketmoney_ext.include_router(pocketmoney_api_router)

pocketmoney_static_files = [
    {
        "path": "/pocketmoney/static",
        "name": "pocketmoney_static",
    }
]

scheduled_tasks: list[asyncio.Task] = []


def pocketmoney_stop():
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(f"Error cancelling PocketMoney task: {ex}")


def pocketmoney_start():
    from lnbits.tasks import create_permanent_unique_task

    task = create_permanent_unique_task("ext_pocketmoney_scheduler", run_pocketmoney_scheduler)
    scheduled_tasks.append(task)


__all__ = [
    "db",
    "pocketmoney_ext",
    "pocketmoney_start",
    "pocketmoney_static_files",
    "pocketmoney_stop",
]
