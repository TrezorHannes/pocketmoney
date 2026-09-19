from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class CadenceType(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CRON = "cron"


class RecipientType(str, Enum):
    INTERNAL = "internal"
    LIGHTNING_ADDRESS = "lightning_address"
    LNURL = "lnurl"


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED_INSUFFICIENT_FUNDS = "skipped_insufficient_funds"
    SKIPPED_SLIPPAGE = "skipped_slippage"


class TriggerType(str, Enum):
    DAEMON = "daemon"
    MANUAL = "manual"
    WEBHOOK = "webhook"


class ItemCreate(BaseModel):
    label: str = Field(..., description="Recipient label (e.g. Alice, Hetzner VPS)")
    recipient: str = Field(..., description="Wallet ID, Lightning Address, or LNURL-pay")
    amount: Decimal = Field(..., gt=0, description="Amount in specified currency")
    currency: str = Field(default="sat", description="Currency ticker (sat, EUR, USD, etc.)")
    memo: str = Field(default="", description="Optional payment memo")
    max_sat_limit: Optional[int] = Field(default=None, description="Optional sat ceiling for fiat items")


class Item(BaseModel):
    id: str
    plan_id: str
    label: str
    recipient: str
    recipient_type: str
    amount: Decimal
    currency: str
    memo: str = ""
    max_sat_limit: Optional[int] = None
    created_at: Optional[datetime] = None


class PlanCreate(BaseModel):
    name: str = Field(..., description="Plan title (e.g. Kids Pocket Money)")
    description: str = Field(default="", description="Optional plan notes")
    cadence_type: CadenceType = Field(default=CadenceType.WEEKLY)
    cron_expression: str = Field(default="0 9 * * 5", description="Standard 5-field cron")
    timezone: str = Field(default="UTC", description="Timezone name")
    max_sat_limit: Optional[int] = Field(default=None, description="Max total sat budget per execution")
    low_balance_threshold: int = Field(default=0, description="Low balance warning threshold in sats")
    telegram_chat_id: Optional[str] = Field(default=None, description="Optional plan-specific chat ID")
    items: List[ItemCreate] = Field(default_factory=list)


class PlanUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    cadence_type: Optional[CadenceType] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    is_active: Optional[bool] = None
    max_sat_limit: Optional[int] = None
    low_balance_threshold: Optional[int] = None
    telegram_chat_id: Optional[str] = None
    items: Optional[List[ItemCreate]] = None


class Plan(BaseModel):
    id: str
    wallet_id: str
    name: str
    description: str = ""
    cadence_type: str
    cron_expression: str
    timezone: str = "UTC"
    is_active: bool = True
    max_sat_limit: Optional[int] = None
    webhook_token: str
    low_balance_threshold: int = 0
    telegram_chat_id: Optional[str] = None
    next_run_at: datetime
    last_run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    items: List[Item] = Field(default_factory=list)


class Execution(BaseModel):
    id: str
    plan_id: str
    wallet_id: str
    triggered_by: str
    status: str
    total_sats: int
    total_fees_msat: int = 0
    details: List[dict[str, Any]] = Field(default_factory=list)
    error_message: Optional[str] = None
    executed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Execution":
        d = dict(row)
        if isinstance(d.get("details"), str):
            try:
                d["details"] = json.loads(d["details"])
            except Exception:
                d["details"] = []
        return cls(**d)


class PocketMoneySettings(BaseModel):
    wallet_id: str
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    notify_on_success: bool = True
    notify_on_failure: bool = True
    notify_on_low_balance: bool = True


class SettingsUpdate(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    notify_on_success: Optional[bool] = None
    notify_on_failure: Optional[bool] = None
    notify_on_low_balance: Optional[bool] = None


class TestTelegramRequest(BaseModel):
    telegram_bot_token: str
    telegram_chat_id: str


class SimulateItemResult(BaseModel):
    label: str
    recipient: str
    recipient_type: str
    amount: Decimal
    currency: str
    estimated_sats: int
    valid: bool
    warning: Optional[str] = None


class SimulatePlanResponse(BaseModel):
    can_execute: bool
    total_sats: int
    current_balance_sats: int
    balance_after_sats: int
    items: List[SimulateItemResult]
    warnings: List[str] = Field(default_factory=list)
