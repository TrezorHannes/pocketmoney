# PocketMoney LNbits Extension

PocketMoney automates recurring pocket money, allowances, contractor payroll, and vendor bills from your LNbits wallets.

### Key Features
- **Grouped Disbursement Plans**: Bundle multiple recipients (e.g. Alisa, Felix, Valentina) into a single recurring weekly or monthly schedule.
- **Universal Recipient Support**: Send to internal LNbits wallets (instant zero-fee ledger transfers without needing recipient invoice keys) or external Lightning Addresses (`user@domain.com`) and LNURL-pay endpoints.
- **Multi-Currency**: Denominate each allowance in satoshis or any fiat currency allowed by the server admin (EUR, USD, GBP, etc.) with just-in-time exchange rate conversion.
- **Dual-Mode Scheduling**: Friendly presets (Daily, Weekly with weekday picker, Monthly) and full Cron expressions with live Next Run previews.
- **Fail-Safe Volatility & Balance Controls**: Optional max-sat ceilings per item or plan, and all-or-nothing batch execution so partial payments are never made if the balance is insufficient.
- **Multi-Channel Alerts**: Built-in Telegram bot notifications and low-balance warnings.
- **Native Background Task**: Powered by LNbits' internal async task runner, with on-demand "Pay Now" testing and external webhook triggers.
