# Security Policy

## Supported Versions

Only the latest release of PocketMoney is actively maintained with security updates and bug fixes:

| Version | Supported          |
| :---    | :---               |
| 0.1.x   | :white_check_mark: |
| < 0.1.0 | :x:                |

---

## Reporting a Vulnerability

We take the security of PocketMoney and funds routed over the Bitcoin Lightning Network extremely seriously. If you discover or suspect a security vulnerability, please report it responsibly.

### How to Report

1. **Email Directly**: Send an encrypted or plain report to **`hakuna@tunnelsats.com`**.
2. **GitHub Security Advisory**: Alternatively, submit a private advisory through the [GitHub Advisory Tab](https://github.com/TrezorHannes/pocketmoney/security/advisories/new).
3. **DO NOT** open a public issue or discuss the vulnerability in public channels until a fix has been coordinated and released.

### What to Include in Your Report

To help us triage and remediate the issue rapidly, please provide:
- A description of the vulnerability and its potential impact (e.g. unauthorized balance disbursement, privilege escalation, denial of service).
- Step-by-step instructions or proof-of-concept (PoC) code to reproduce the behavior.
- LNbits version, Python version, and backend funding source (e.g. LND, CLN, FakeWallet) tested.
- Any proposed remediation or patches, if available.

### Response Timelines

- **Acknowledgment**: Within 48 hours of receipt.
- **Assessment & Triage**: Within 5 business days.
- **Patch Release & Advisory**: Dependent on severity, typically within 14 days.

---

## Security Model & Invariants

PocketMoney is built with the following security invariants:

1. **Fail-Closed Execution**: If an exchange rate conversion fails, wallet balance is insufficient, or price slippage exceeds limits, PocketMoney skips the entire plan execution without partial payments or satoshi deductions.
2. **Per-Plan Concurrency Locks**: Plans cannot be executed concurrently across parallel daemon loops or overlapping manual triggers (`asyncio.Lock` per plan ID).
3. **Secret Webhook Tokens**: Webhook trigger endpoints are secured by high-entropy UUID tokens unique to each plan. No global admin keys or wallet credentials are exposed to external automation webhooks.
4. **Tenant & Wallet Scoping**: All database CRUD operations are strictly partitioned by `wallet_id` and verified against LNbits' core authentication checks (`require_admin_key`).
