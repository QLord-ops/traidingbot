# Claude Code instructions

Read `HANDOFF_RU.md` and `README_RU.md` before changing code.

Work as a senior Python quant/backend engineer and security-conscious trading systems developer.

Rules:

- Start by auditing the repository. Do not assume the existing implementation is correct.
- Preserve live trading lockout. Work only with public data, dry-run, and Binance Futures Testnet until explicit later approval.
- Never commit `.env`, API keys, secrets, database files, logs, or generated backtest results.
- Do not claim profitability. Validate every strategy result after realistic fees and execution assumptions.
- Avoid look-ahead bias, data leakage, survivorship bias, and parameter overfitting.
- Prefer small, reviewable commits.
- Add tests for every material risk or execution change.
- Keep the UI in Russian unless code-level labels require English.
- Before using Binance endpoints, verify current official Binance USDⓈ-M Futures API documentation.
- Do not implement any regional restriction bypass.

First deliverable:

1. Inspect and run the current project and tests.
2. Produce a concise audit with critical/high/medium issues.
3. Fix blockers needed for a safe, reproducible backtest and web app.
4. Add Docker Compose and one-command local startup.
5. Commit and push to `https://github.com/QLord-ops/traidingbot.git`.
6. Then begin the Testnet MVP roadmap in `HANDOFF_RU.md`.
