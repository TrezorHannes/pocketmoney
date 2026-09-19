# Contributing to PocketMoney

Thank you for your interest in improving **PocketMoney**! We welcome bug fixes, documentation enhancements, feature proposals, and architectural improvements.

---

## 1. Development Setup

PocketMoney runs as an LNbits extension. We recommend developing locally using Python 3.12 and [`uv`](https://github.com/astral-sh/uv).

### Prerequisites
- Python 3.10 – 3.12
- `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Git

### Initializing the Environment
1. Clone LNbits and install its dependencies:
   ```bash
   git clone https://github.com/lnbits/lnbits.git
   cd lnbits
   uv sync --extra all
   ```

2. Clone PocketMoney and symlink it into LNbits extensions:
   ```bash
   git clone https://github.com/TrezorHannes/pocketmoney.git
   ln -s "$(pwd)/pocketmoney" "$(pwd)/lnbits/lnbits/extensions/pocketmoney"
   ```

3. Run the development server:
   ```bash
   LNBITS_BACKEND_WALLET_CLASS=FakeWallet FAKE_WALLET_SECRET=dev uv run --directory lnbits lnbits --port 5000
   ```

---

## 2. Code Quality & Diligence Standards

- **Zero Mocked Dataplanes**: Tests and implementations must interact with real database layers and genuine LNbits payment pipelines. Do not write dummy stubs that return `True` without executing real operations.
- **Fail-Closed Verification**: Any unverified financial state or balance check must fail closed.
- **Zero New External Dependencies**: PocketMoney strictly relies on dependencies already bundled with LNbits core (`fastapi`, `pydantic`, `httpx`, `sqlalchemy`, standard library `datetime`/`zoneinfo`).
- **Idempotent Migrations**: Database schemas must always use `CREATE TABLE IF NOT EXISTS` to support safe re-runs and upgrades.

---

## 3. Testing & Linting

Before submitting any code changes, ensure all linting and test checks pass cleanly:

```bash
# 1. Lint and format checks
uv run --directory ../lnbits ruff check .

# 2. Automated test suite (unit + e2e integration)
uv run --directory ../lnbits pytest tests/ -v
```

All 5+ unit and integration tests must pass with 100% success.

---

## 4. Privacy & Git Identity Standards

To ensure privacy and maintain a clean git history:
- Verify local git identity before committing (`git config user.name` and `git config user.email`).
- Never commit personal or corporate credentials, API tokens, or server passwords into the repository.
- Use atomic, descriptive commit messages adhering to Conventional Commits:
  - `feat: add support for Nostr notifications`
  - `fix: prevent potential race condition in plan execution`
  - `docs: update webhook integration examples`

---

## 5. Pull Request Workflow

1. Fork the repository and create a feature branch (`git checkout -b feature/my-feature`).
2. Make your changes with focused, atomic commits.
3. Run the linter and test suite.
4. Push to your fork and submit a Pull Request against `main`.
5. Ensure all GitHub Actions CI checks (`lint-and-test`) pass cleanly.
