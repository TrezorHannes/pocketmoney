# PocketMoney LNbits Extension

<p align="center">
  <img src="static/image/tile.png" width="180" alt="PocketMoney Logo" />
</p>

**PocketMoney** is a modular, secure, and production-ready LNbits extension designed for recurring allowances, automated contractor payroll, and monthly server hosting payments.

---

## Features

- **Grouped Disbursement Batches**: Group multiple recipients (e.g. Alisa, Felix, Valentina) into a single recurring plan executed on a unified schedule.
- **Universal Recipient Support**: Send to internal LNbits wallets (instant zero-fee ledger transfers without needing child invoice keys) or external Lightning Addresses (`user@domain.com`) and LNURL-pay endpoints.
- **Multi-Currency & JIT Exchange Rates**: Schedule payments in satoshis or any fiat currency allowed by the server admin (EUR, USD, GBP, etc.). Fiat conversions are calculated just-in-time at the second of execution using LNbits' exchange rate providers.
- **Fail-Safe & Volatility Controls**: Optional maximum satoshi ceiling (`max_sat_limit`) per recipient and plan. All-or-nothing batch execution prevents partial payments if wallet balance is insufficient.
- **Dual-Mode Scheduling**: Friendly presets (Daily, Weekly with day picker, Monthly) and full 5-field cron syntax with live next-run previews.
- **Background Worker Daemon**: Runs natively within LNbits via `create_permanent_unique_task` (zero host/systemd setup required).
- **On-Demand & Webhook Triggers**: "Pay Now" test button in the UI, dry-run simulation mode, and a secret webhook trigger URL for smart home automations.
- **Multi-Channel Alerts**: Built-in Telegram bot alerts with low-balance threshold warnings.

---

## Installation & Setup

### For Local LNbits Development
1. Symlink this folder into your LNbits `extensions` directory:
   ```bash
   ln -s /path/to/pocketmoney /path/to/lnbits/lnbits/extensions/pocketmoney
   ```
2. Start LNbits:
   ```bash
   uv run lnbits --port 5000 --reload
   ```
3. Open your browser to `http://localhost:5000`, log in, navigate to **Extensions**, and activate **PocketMoney**.

---

## License
MIT License. Copyright (c) 2025-2026 Hakuna.
