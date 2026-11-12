# Unattended work

When you leave an agent working for hours, the AWS portal session is the only thing that can stop it. `rolehop run` makes sure the session covers the job **before** you leave.

## Before you leave

```bash
rolehop run --login --max 6h
```

1. `--login` renews the session now (`awslogin --force`), so the full portal window starts from this moment.
2. `rolehop` checks that every organization has at least `--max` plus a 15-minute margin left.
3. If the window is too short, it tells you the maximum possible and suggests splitting the work.

```console
$ rolehop run --login --max 6h
→ Renewing the session so the full window starts now…
✓ acme: ~8h00m left (you need 6h15m).
✓ Ready: the session will last ~6h. You can leave your agent working.
```

Agents that have the `rolehop` rules will **suggest this command themselves** when you say *"I'm leaving"* or *"keep working"*, propose a duration based on the pending work, and reorder the plan so AWS tasks run first.

## Headless runs

`rolehop run` can also wrap the agent command itself:

```bash
rolehop run --login --max 6h -- claude -p "Upgrade the EKS add-ons in dev"
```

It checks the session, runs the command and, at the end, reports any organization whose session expired during the run, with the exact commands to recover:

```text
⚠ The AWS session ended during the work in: acme
  1) awslogin acme
  2) Resume your agent's conversation (in Claude Code: claude --continue).
```

## Options

| Option | Default | Description |
|---|---|---|
| `--max DURATION` | `4h` | Expected job length: `90m`, `3h`, `1h30m`. |
| `--login` | off | Renew the session first, for a full window from now. |
| `--org ORG` | all | Organization to check. Repeatable. |
| `--yes` | off | Start even when the remaining time cannot be estimated. |
| `-- COMMAND` | - | Command to run after the checks. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Session is long enough (or the wrapped command succeeded). |
| `1` | Login failed. |
| `2` | Invalid `--max`. |
| `75` | The session does not cover the requested duration. Nothing was started. |
| `127` | The wrapped command was not found. |
| other | Exit code of the wrapped command. |

## Best practices

- **AWS first.** Do the work that needs AWS early in the window; leave code, docs and local tests for the end.
- **Infrastructure through pipelines.** Changes that must be applied while you are away belong in a PR applied by CI/CD with its own role (OIDC, CodePipeline) - never a local `apply` with human credentials.
- **Do not raise session durations to make this work.** Splitting the job is safer than longer-lived sessions.
