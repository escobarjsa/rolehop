---
title: AWS SSO profiles for humans and AI agents
description: Auto-refreshing AWS IAM Identity Center profiles for many organizations, ready for humans and AI coding agents.
---

# rolehop

**Hop between AWS accounts and roles with one login a day — and let your AI coding agent do the same, safely.**

`rolehop` turns a small TOML file that describes your AWS IAM Identity Center organizations, accounts and roles into native, auto-refreshing `sso-session` profiles. It gives you a single command to log in (`awslogin`) and teaches AI coding agents — Claude Code, Codex, Kiro, Copilot CLI, Gemini CLI, Qwen Code and more — how to pick the right profile, and what to do when the SSO session ends.

[Get started](getting-started/installation.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/escobarjsa/rolehop){ .md-button }

---

<div class="grid cards" markdown>

-   :lucide-refresh-cw:{ .lg .middle } **No more expired keys**

    ---

    Native `sso-session` profiles with a refresh token. The AWS CLI and SDKs renew role credentials on their own for the whole portal session.

-   :lucide-building-2:{ .lg .middle } **Any number of organizations**

    ---

    Work, personal, clients — each Identity Center portal is one `[[org]]` block. Profiles are named `<org>-<account>-<role>`.

-   :lucide-bot:{ .lg .middle } **Built for AI agents**

    ---

    Installs rules (and hooks, where supported) so agents choose profiles freely, never touch credentials, and stop to ask for `awslogin` when the session ends.

-   :lucide-shield-check:{ .lg .middle } **Safe by design**

    ---

    Never writes tokens or keys. Validates every value, writes only inside marked blocks, atomically, with backups and `0600` permissions.

-   :lucide-wand-sparkles:{ .lg .middle } **Wizard with discovery**

    ---

    `rolehop init` asks for the portal URL, logs you in, and discovers your accounts and roles through the AWS access portal API.

-   :lucide-package:{ .lg .middle } **Zero dependencies**

    ---

    Pure Python standard library. Works on Linux, WSL and macOS with Python 3.11+ and AWS CLI v2.

</div>

## The problem

Copying the temporary **access keys** from the IAM Identity Center portal gives you *static* credentials that die with the role duration — often one hour for an administrator permission set. You and your AI agent lose access in the middle of the work, and nothing renews them.

## The solution

| Clock | Typical duration | Who renews it |
|---|---|---|
| Role credentials (permission set) | 1 h (Admin), 4 h, 8 h… | The AWS CLI and SDKs, automatically |
| Identity Center portal session | As configured by your admin (8 h by default) | You: `awslogin` — one browser approval |

With `rolehop`, the only thing that ever expires in practice is the portal session — and a single `awslogin` per organization unlocks **every** account and role in it.

```console
$ awslogin
→ acme: aws sso login --sso-session acme --use-device-code
✓ acme: session started · account 111111111111

$ AWS_PROFILE=acme-prod-readonly aws s3 ls
```

!!! tip "Tell your agent"

    *"Use whatever AWS profiles and roles you need."* The agent reads the catalog with `rolehop list`, picks the profile, and tells you which one it used.

## Supported agents

Claude Code · OpenAI Codex CLI · Kiro · GitHub Copilot CLI · Gemini CLI · Qwen Code · OpenCode · Amp · Pi · Devin Desktop (Windsurf) · Cursor. See [AI coding agents](guide/ai-agents.md).
