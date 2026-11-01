from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field, validator

LABEL_PATTERN = re.compile(r"^[A-Za-z0-9 ._-]{1,100}$")
MAX_PLAN_LABELS = 20


def normalize_plan_labels(
    raw: Any,
    *,
    allow_none: bool = False,
    validate_chars: bool = True,
) -> Optional[List[str]]:
    if raw is None:
        return None if allow_none else []
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError("labels must be valid JSON array") from exc
        if isinstance(parsed, dict) and not parsed:
            return []
        raw = parsed
    if not isinstance(raw, (list, tuple)):
        raise ValueError("labels must be a list of strings")

    cleaned: List[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError("Each label must be a string")
        name = entry.strip()
        if not name:
            continue
        if validate_chars and not LABEL_PATTERN.fullmatch(name):
            raise ValueError(
                f"Invalid label '{name}'. Use letters, digits, spaces, '.', '_', or '-' (max 100 chars)."
            )
        if name not in seen:
            seen.add(name)
            cleaned.append(name)

    if validate_chars and len(cleaned) > MAX_PLAN_LABELS:
        raise ValueError(f"At most {MAX_PLAN_LABELS} labels are allowed per plan.")

    return cleaned


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
    PARTIAL = "partial"  # Some recipients paid, some failed — do NOT re-run carelessly
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
    labels: List[str] = Field(default_factory=list, description="Optional LNbits payment labels applied to all plan payouts")
    items: List[ItemCreate] = Field(default_factory=list)

    @validator("labels", pre=True)
    def _validate_create_labels(cls, v: Any) -> List[str]:
        res = normalize_plan_labels(v, allow_none=False, validate_chars=True)
        return res or []


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
    labels: Optional[List[str]] = None
    items: Optional[List[ItemCreate]] = None

    @validator("labels", pre=True)
    def _validate_update_labels(cls, v: Any) -> Optional[List[str]]:
        return normalize_plan_labels(v, allow_none=True, validate_chars=True)


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
    labels: List[str] = Field(default_factory=list)
    next_run_at: datetime
    last_run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    items: List[Item] = Field(default_factory=list)

    @validator("labels", pre=True)
    def _validate_db_labels(cls, v: Any) -> List[str]:
        res = normalize_plan_labels(v, allow_none=False, validate_chars=False)
        return res or []


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

    @validator("details", pre=True)
    def _validate_db_details(cls, v: Any) -> List[dict[str, Any]]:
        if v is None or v == "" or isinstance(v, dict):
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except ValueError:
                return []
            return parsed if isinstance(parsed, list) else []
        return list(v) if isinstance(v, (list, tuple)) else []


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


class WalletVerifyResponse(BaseModel):
    valid: bool
    exists: bool
    is_self: bool = False
    is_same_user: bool = False
    reason: Optional[str] = None

