# Contributing

Keep contributions focused and preserve the recorder subprocess contract.

1. Install the locked development environment with
   `uv sync --frozen --group dev`.
2. Add a regression test for behavioral changes.
3. Run focused tests, then `./scripts/check.sh`.
4. Update user-facing documentation when behavior or configuration changes.

Follow the surrounding Python, JavaScript, and template style. Avoid unrelated
formatting or architectural changes.

Automated tests must be deterministic and offline. Use temporary directories,
temporary SQLite databases, mocks, fake process objects, and fake HTTP
responses. Do not start Selenium, a real recorder, the supervisor, or a web
server during routine tests. Do not use production databases, recordings,
cookies, browser profiles, PIDs, ports, or `/tmp/tiktok_live_*` artifacts.

Never commit credentials or runtime data. Security-sensitive changes should
include a focused regression test and should not log secrets, cookies,
authorization headers, proxy credentials, or Telegram bot tokens.
