# CLI reference

```text
rolehop [-h] [--version] {setup,init,sync,login,status,list,run,agent,envrc,discover} ...
```

Every command that modifies files makes a timestamped backup (`<file>.bak-YYYYmmdd-HHMMSS`) first, and most accept `--dry-run`.

## `rolehop setup`

All-in-one: shell `PATH` and direnv hook, wizard if there is no configuration, profiles, and AI agents.

```text
rolehop setup [--dry-run] [--no-agents] [--agent AGENT]...
```

| Option | Description |
|---|---|
| `--dry-run` | Show what would change. |
| `--no-agents` | Do not install agent rules or hooks. |
| `--agent AGENT` | Only this agent. Repeatable. Default: detected agents. |

## `rolehop init`

Interactive wizard: asks for organizations, logs in and discovers accounts and roles, then writes `orgs.toml`.

```text
rolehop init
```

## `rolehop sync`

Writes the profiles into `~/.aws/config`, inside managed blocks.

```text
rolehop sync [--dry-run] [--prune] [--quiet]
```

| Option | Description |
|---|---|
| `--dry-run` | Show the diff without writing. |
| `--prune` | Remove blocks of organizations no longer in `orgs.toml`. |
| `--quiet` | Do not print the diff. |

Exit code `2` on validation errors or conflicts; nothing is written in that case.

## `rolehop login` / `awslogin`

Runs `aws sso login` only for organizations that need it.

```text
rolehop login [--device] [--force] [ORG ...]
awslogin      [--device] [--force] [ORG ...]
```

| Option | Description |
|---|---|
| `ORG` | Organizations to log in to. Default: all. |
| `--device` | Force the device code flow. |
| `--force` | Log in even if a session exists (renews the full window). |

## `rolehop status`

Session status per organization, with the estimated time left.

```text
rolehop status [--json] [ORG ...]
```

## `rolehop list`

Profile catalog: organization, account, account ID, role and profile name.

```text
rolehop list [--json] [--org ORG]
```

## `rolehop run`

Checks (and optionally renews) the session before leaving an agent working. See [Unattended work](../guide/unattended-work.md).

```text
rolehop run [--org ORG]... [--max DURATION] [--login] [--yes] [-- COMMAND ...]
```

## `rolehop agent`

Rules and hooks for AI coding agents. See [AI coding agents](../guide/ai-agents.md).

```text
rolehop agent {list,install,uninstall,rules} [NAME ...] [--all] [--dry-run] [--no-hooks] [--json]
```

Agent names: `claude`, `codex`, `kiro`, `copilot`, `gemini`, `qwen`, `opencode`, `amp`, `pi`, `devin`, `cursor`.

## `rolehop envrc`

Prints an `.envrc` for direnv. See [Per-repository defaults](../guide/per-repo-defaults.md).

```text
rolehop envrc PROFILE [--state PROFILE] [--state-var NAME]
```

## `rolehop discover`

Lists the real accounts and permission sets of an organization through the AWS access portal API. Requires an active session.

```text
rolehop discover ORG
```

## `rolehop hook`

Internal entry points used by agent hooks. You normally do not run them yourself.

```text
rolehop hook post-tool [--format {claude,codex,copilot,gemini,qwen,text}]
rolehop hook cwd
```

- `post-tool` reads the hook payload on stdin and reacts to SSO expiry errors.
- `cwd` exports the direnv environment for Claude Code's `CwdChanged` event.
