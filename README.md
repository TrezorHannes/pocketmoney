# PocketMoney 💶⚡

<p align="center">
  <img src="static/image/tile.png" width="160" alt="PocketMoney Logo" />
</p>

<p align="center">
  <strong>Automated recurring allowances, scheduled batch disbursements, and payroll on the Bitcoin Lightning Network.</strong>
</p>

<p align="center">
  <a href="https://github.com/TrezorHannes/pocketmoney/actions/workflows/ci.yml"><img src="https://github.com/TrezorHannes/pocketmoney/actions/workflows/ci.yml/badge.svg" alt="CI Status"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue" alt="Python Versions">
  <img src="https://img.shields.io/badge/LNbits-%3E%3D1.0.0-purple" alt="LNbits Compatibility">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

---

## Overview

**PocketMoney** is an open-source [LNbits](https://github.com/lnbits/lnbits) extension that automates recurring payouts in any currency (sats, EUR, USD, etc.) to internal child wallets, external Lightning Addresses, or LNURL-pay endpoints.

### Primary Use Cases
- 👨‍👩‍👧‍👦 **Weekly Family Allowance**: Give \$4, 4,000 sats, or 4€ every Friday morning at 09:00 to child wallets (e.g. Alisa, Felix, Valentina) with zero routing fees.
- 🖥️ **Recurring Server & SaaS Bills**: Pay your monthly VPS hosting bills automatically via Lightning Address on the 1st of every month.
- 🌍 **Global Contractor Payroll**: Stream monthly salaries across the globe to contractor Lightning Addresses with fail-closed slippage safety.

---

## Key Features

- **Multi-Recipient Batches**: Group multiple line items into a single recurring plan executed on a unified schedule.
- **Universal Recipient Support**:
  - **Internal LNbits Wallets**: Instant zero-fee ledger transfers without requiring recipient invoice keys.
  - **External Lightning Addresses & LNURL-pay**: Fresh BOLT11 invoices fetched and paid over the Lightning Network.
- **JIT Multi-Currency Budgeting**: Set allowances in EUR, USD, GBP, or SAT. Fiat conversions are calculated just-in-time at execution using LNbits' exchange rate providers.
- **Fail-Closed & Slippage Safeguards**: Optional maximum satoshi limits (`max_sat_limit`) per line item and total plan budget ceilings. If balance is insufficient or rates spike, the plan skips cleanly with zero partial payments.
- **Zero-Dependency Recurrence Engine**: Supports intuitive presets (`daily`, `weekly`, `monthly`) or full 5-part cron syntax with native timezone support (`zoneinfo`).
- **Interactive Quasar / Vue 3 Dashboard**:
  - Live dry-run simulator modal (estimates satoshi costs and tests recipient validity before saving).
  - One-click manual trigger ("Pay Now") for immediate execution.
  - Paginated audit log with status badges and per-item payment breakdown modal.
- **Secret Webhook Triggers**: High-entropy per-plan webhook URLs (`/api/v1/webhook/{token}`) for triggering plans from external cron jobs, systemd timers, or Home Assistant automations without exposing API keys.
- **Multi-Channel Alerts**: Built-in Telegram bot notifications on success, failure, and low-balance warnings.

---

## Architecture & Execution Lifecycle

```mermaid
graph TD
    Trigger[Trigger: Scheduler / Webhook / Pay Now] --> Concurrency[Acquire Plan Concurrency Lock]
    Concurrency --> JIT[JIT Currency Conversion via fiat_amount_as_satoshis]
    JIT --> Slippage[Check Max Sat Limits & Budget Ceiling]
    Slippage -->|Exceeded| SkipSlippage[Skip: Alert Telegram]
    Slippage -->|Within Limits| Balance[Check Funding Wallet Balance]
    Balance -->|Insufficient| SkipBalance[Skip: Alert Telegram & Record Audit Log]
    Balance -->|Sufficient| Dispatch[Universal Recipient Dispatcher]

    Dispatch -->|Internal Wallet ID| Internal[Core: create_invoice + pay_invoice<br/>0 Network Fees]
    Dispatch -->|user@domain / LNURL| External[Core: get_pr_from_lnurl + pay_invoice]

    Internal --> Audit[Record in ext_pocketmoney.executions]
    External --> Audit
    Audit --> NextRun[Advance next_run_at Timestamp]
    NextRun --> Release[Release Plan Concurrency Lock]
```

---

## Installation & Deployment

### Local Development Setup
1. Clone this repository and symlink it into your LNbits extensions folder:
   ```bash
   git clone https://github.com/TrezorHannes/pocketmoney.git
   ln -s "$(pwd)/pocketmoney" "/path/to/lnbits/lnbits/extensions/pocketmoney"
   ```

2. Start LNbits:
   ```bash
   uv run lnbits --port 5000
   ```

3. Open `http://localhost:5000`, navigate to **Extensions** in the left sidebar, locate **PocketMoney**, and click **Enable**.

### Production Deployment (e.g. Hetzner VPS)
1. Clone or sync the repository into your VPS LNbits extensions directory:
   ```bash
   cd /path/to/lnbits/lnbits/extensions
   git clone https://github.com/TrezorHannes/pocketmoney.git
   ```

2. Restart your LNbits systemd service to register the extension and execute database migrations:
   ```bash
   sudo systemctl restart lnbits
   ```

3. Enable **PocketMoney** in your LNbits admin dashboard.

---

## API Reference

PocketMoney exposes an authenticated REST API and a public webhook endpoint:

| Method | Endpoint | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/pocketmoney/api/v1/plans` | Admin Key | List all plans for the funding wallet |
| `POST` | `/pocketmoney/api/v1/plans` | Admin Key | Create a new recurring plan |
| `GET` | `/pocketmoney/api/v1/plans/{id}` | Admin Key | Fetch details and line items for a plan |
| `PUT` | `/pocketmoney/api/v1/plans/{id}` | Admin Key | Update plan settings, schedule, or items |
| `DELETE`| `/pocketmoney/api/v1/plans/{id}` | Admin Key | Delete a plan and its line items |
| `POST` | `/pocketmoney/api/v1/plans/{id}/simulate` | Admin Key | Dry-run simulation (estimates sats, checks balance) |
| `POST` | `/pocketmoney/api/v1/plans/{id}/run` | Admin Key | Trigger immediate execution ("Pay Now") |
| `GET` | `/pocketmoney/api/v1/history` | Admin Key | Fetch execution audit logs for the wallet |
| `GET` | `/pocketmoney/api/v1/settings` | Admin Key | Fetch Telegram notification settings |
| `PUT` | `/pocketmoney/api/v1/settings` | Admin Key | Update Telegram bot token & notification preferences |
| `POST` | `/pocketmoney/api/v1/webhook/{token}` | Token | Secret webhook trigger (no API key required) |

---

## External Automations & Webhooks

You can trigger a plan from external schedulers (e.g. systemd, cron, or Home Assistant) using its unique webhook token:

```bash
curl -X POST https://your-lnbits-instance.com/pocketmoney/api/v1/webhook/YOUR_SECRET_WEBHOOK_TOKEN
```

---

## Testing & Quality Assurance

PocketMoney includes a full test suite covering cron schedule calculations, Pydantic data schemas, dry-run simulations, and end-to-end ledger payments:

```bash
# Run linting
uv run --directory ../lnbits ruff check .

# Run unit and integration tests
uv run --directory ../lnbits pytest tests/ -v
```

---

## Security

Please review [SECURITY.md](SECURITY.md) for information regarding our security practices and instructions on how to report vulnerabilities.

---

## Contributing

We welcome contributions! Please review [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on code standards, local setup, and PR submissions.

---

## License

MIT License. Copyright (c) 2025–2026 Hakuna.
