# rolehop

[![CI](https://github.com/escobarjsa/rolehop/actions/workflows/ci.yml/badge.svg)](https://github.com/escobarjsa/rolehop/actions/workflows/ci.yml)
[![Docs](https://github.com/escobarjsa/rolehop/actions/workflows/docs.yml/badge.svg)](https://escobarjsa.github.io/rolehop/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

**Hop between AWS accounts and roles with one login a day — and let your AI coding agent do the same, safely.**

`rolehop` turns a small TOML file describing your AWS IAM Identity Center organizations, accounts and roles into native, auto-refreshing `sso-session` profiles in `~/.aws/config`. It gives you a one-command login (`awslogin`) and teaches AI coding agents — Claude Code, Codex, Kiro, Copilot CLI, Gemini CLI, Qwen Code, OpenCode, Amp and more — how to pick profiles, and what to do when the SSO session expires.

📖 **Documentation: <https://escobarjsa.github.io/rolehop/>**

## Why

Copying the temporary access keys from the IAM Identity Center portal gives you **static** credentials that die with the role duration — often one hour. You and your agent lose access mid-task.

| Clock | Typical duration | Who renews it |
|---|---|---|
| Role credentials (permission set) | 1 h (Admin), 4 h, 8 h… | The AWS CLI and SDKs, automatically |
| Identity Center portal session | As configured (8 h by default) | You: `awslogin` — one browser approval |

With `rolehop`, one `awslogin` per organization unlocks every account and role in it for the whole portal session.

## Features

- **Many organizations** — work, personal, clients: one `[[org]]` block each. Profiles are named `<org>-<account>-<role>`.
- **Wizard with discovery** — `rolehop init` asks for the portal URL and discovers your accounts and permission sets.
- **AI agent integration** — rules and hooks so agents choose profiles freely, never read credentials, and stop to ask for `awslogin` on expiry.
- **Unattended runs** — `rolehop run --login --max 6h` checks the session covers the job before you walk away.
- **Safe writes** — validated input, managed blocks only, atomic writes, backups, `0600`. Never writes tokens or keys.
- **Zero dependencies** — Python standard library only.

## Install

Requires Python 3.11+ and AWS CLI v2 (2.22+). Linux, WSL or macOS.

```bash
uv tool install rolehop        # or: pipx install rolehop
rolehop setup
```

Before the PyPI release, install from GitHub:

```bash
uv tool install git+https://github.com/escobarjsa/rolehop
```

`rolehop setup` adds `~/.local/bin` to your `PATH`, runs the wizard if needed, writes your profiles and configures the AI agents it detects.

## Usage

```bash
awslogin                                   # start the day
rolehop status                             # session per organization
rolehop list                               # profile catalog
AWS_PROFILE=acme-prod-admin aws s3 ls      # use a profile
rolehop run --login --max 6h               # before leaving an agent working
```

Then tell your agent: *"use whatever AWS profiles and roles you need."*

## Configuration

`~/.config/rolehop/orgs.toml`:

```toml
[[org]]
name = "acme"
start_url = "https://acme.awsapps.com/start"
sso_region = "us-east-1"
default_role = "admin"

[org.roles]
admin = "Administrator"
ro = "ReadOnly"

[[org.accounts]]
name = "devsecops"
id = "111111111111"
roles = ["admin", "ro"]
```

See the [configuration reference](https://escobarjsa.github.io/rolehop/guide/configuration/).

## Supported agents

| Agent | Instructions | Expiry hook |
|---|---|---|
| Claude Code | `~/.claude/rules/aws.md` | ✅ + `deny` rules |
| OpenAI Codex CLI | `~/.codex/AGENTS.md` | ✅ |
| Kiro | `~/.kiro/steering/aws.md` | — |
| GitHub Copilot CLI | `~/.copilot/copilot-instructions.md` | ✅ |
| Gemini CLI | `~/.gemini/GEMINI.md` | ✅ |
| Qwen Code | `~/.qwen/QWEN.md` | ✅ |
| OpenCode · Amp · Pi · Devin Desktop | their `AGENTS.md` / `global_rules.md` | — |
| Cursor | paste `rolehop agent rules` in Settings → Rules | — |

## Migrating from awsorg

`rolehop` was named `awsorg` up to 1.2.0. Configuration, state, managed blocks and agent hooks are adopted automatically — see the [migration guide](https://escobarjsa.github.io/rolehop/reference/migrating-from-awsorg/).

> **Note:** CLI messages are currently in Spanish.

## Development

```bash
uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group docs zensical serve    # docs at http://localhost:8000
uv build
```

## License

[MIT](LICENSE)
