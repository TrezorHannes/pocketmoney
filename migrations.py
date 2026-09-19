from lnbits.db import Connection


async def m001_initial(db: Connection):
    """
    Initial database schema for the PocketMoney extension.
    Supports both SQLite and PostgreSQL.
    """
    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {db.references_schema}plans (
            id TEXT PRIMARY KEY,
            wallet_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            cadence_type TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            timezone TEXT DEFAULT 'UTC',
            is_active BOOLEAN DEFAULT TRUE,
            max_sat_limit INTEGER DEFAULT NULL,
            webhook_token TEXT UNIQUE,
            low_balance_threshold INTEGER DEFAULT 0,
            telegram_chat_id TEXT DEFAULT NULL,
            next_run_at TIMESTAMP NOT NULL,
            last_run_at TIMESTAMP DEFAULT NULL,
            created_at TIMESTAMP DEFAULT {db.timestamp_now}
        );
        """
    )

    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {db.references_schema}items (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            label TEXT NOT NULL,
            recipient TEXT NOT NULL,
            recipient_type TEXT NOT NULL,
            amount NUMERIC NOT NULL,
            currency TEXT NOT NULL,
            memo TEXT DEFAULT '',
            max_sat_limit INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT {db.timestamp_now}
        );
        """
    )

    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {db.references_schema}executions (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            wallet_id TEXT NOT NULL,
            triggered_by TEXT NOT NULL,
            status TEXT NOT NULL,
            total_sats INTEGER NOT NULL,
            total_fees_msat INTEGER DEFAULT 0,
            details TEXT DEFAULT '[]',
            error_message TEXT DEFAULT NULL,
            executed_at TIMESTAMP DEFAULT {db.timestamp_now}
        );
        """
    )

    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {db.references_schema}settings (
            wallet_id TEXT PRIMARY KEY,
            telegram_bot_token TEXT DEFAULT NULL,
            telegram_chat_id TEXT DEFAULT NULL,
            notify_on_success BOOLEAN DEFAULT TRUE,
            notify_on_failure BOOLEAN DEFAULT TRUE,
            notify_on_low_balance BOOLEAN DEFAULT TRUE
        );
        """
    )


async def m002_add_plan_is_running(db: Connection):
    """
    Add is_running boolean to plans table.

    Provides a DB-level atomic claim guard for multi-process / multi-worker
    LNbits deployments. Complements the in-process asyncio.Lock in services.py.
    On claim: UPDATE plans SET is_running = TRUE WHERE id = ? AND is_running = FALSE
    A rowcount of 0 means another worker claimed it first.
    """
    await db.execute(
        f"""
        ALTER TABLE {db.references_schema}plans
        ADD COLUMN is_running BOOLEAN DEFAULT FALSE;
        """
    )
