# Contributing

Thanks for helping. Issues and pull requests are welcome, in English or Portuguese.

## Layout

- `portugal_mcp/app.py` holds the single `FastMCP` instance. Every feature registers its tools on it with `@mcp.tool()` and adds its usage rules with `note(feature, text)`.
- `portugal_mcp/server.py` maps feature names to modules (`FEATURES`). It loads the enabled ones and runs the server.
- `portugal_mcp/common.py` holds the shared HTTP client (with retries and rate-limit handling), the on-disk `FileCache`, and the helpers for text normalisation and Portuguese numbers.
- Simple features are a single module (`clima.py`, `ine.py`, …). Larger ones are a package with a `tools.py` (`saude/`, `energia/`, …).

## Rules for a new source or tool

1. **Official or clearly public open data only.** Respect `robots.txt` and the publisher's terms. Do not use keys embedded in other people's apps, do not scrape behind logins or bot walls, and do not hammer an API: cache, and fetch only what a question needs.
2. **Read-only.** No tool may submit forms, create accounts or change anything anywhere.
3. **No personal data beyond what the publisher already shows for a public role**, and mask identifiers of private individuals (see `contratos._party`).
4. **Every answer says its source and its date or period.**
5. **Errors come back as `{"error": "..."}`** with a hint on what to try, not as a traceback. Use `common.fail`.
6. **Tool names are unique and descriptive.** Prefix them when a short name would be ambiguous (`sns_`, `bus_`).
7. **Tests:** add offline unit tests with small fixtures for the parsing, and one shape check in `tests/test_live.py`.
8. **New dependencies** need a good reason, a major-version bound, and a clean `pip-audit`.

## Checks before a pull request

```bash
ruff check .
pytest
pytest -m live -k <your feature>
```

Pull requests are the only way into `main`; CI must pass before a merge.

## Releases (maintainer)

Bump `version` in `pyproject.toml`, `portugal_mcp/__init__.py` and `server.json` (in both places), update `CHANGELOG.md`, then push a tag `vX.Y.Z`. The publish workflow runs the tests, then publishes to PyPI and the MCP Registry.
