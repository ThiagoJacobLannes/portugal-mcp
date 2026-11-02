# Security

## Reporting a vulnerability

Please report it privately through GitHub: **Security → Report a vulnerability** on this repository. Do not open a public issue. You should get a reply within a week.

## What this server does and does not do

- It only **reads** public open data over HTTPS. It sends no personal data anywhere; requests carry only the search terms the tools need and a `User-Agent` that names this project.
- It needs **no API keys, tokens or accounts**, and it never asks for any.
- It writes only to its own cache folder (`PORTUGAL_MCP_CACHE_DIR` or the OS cache directory).
- `open_data_preview` fetches only `https://` files hosted on `dados.gov.pt` and does not follow redirects to other hosts. It reads at most 512 KB.
- Remote XML is parsed with `defusedxml`, or with the standard pull parser where expat's entity-expansion limits apply.
- The `--http` mode binds to `127.0.0.1` by default. Expose it on a network only behind your own authentication.

## Privacy

Public-contract records name the parties. When a party is an individual person, their tax number (NIF) is hidden. Parliament data covers members of Parliament in their public role only.

## Supply chain

- Dependencies are few, bounded by major version, and audited with `pip-audit` in CI.
- GitHub Actions are pinned by commit SHA and updated through Dependabot.
- Releases are built in CI from a tag and published with PyPI trusted publishing (OIDC). No long-lived tokens exist.
