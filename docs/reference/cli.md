# CLI reference

```text
rolehop [-h] [--version] {setup,init,sync,login,status,list,refresh,remove,run,agent,envrc,discover,upgrade,uninstall} ...
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

## `rolehop refresh`

Re-reads your accounts and permission sets from the AWS access portal, using the session you already have, and updates `orgs.toml` and `~/.aws/config`. Use it when your permissions change.

```text
rolehop refresh [ORG ...] [--prune] [--dry-run] [--login] [--yes]
```

| Option | Description |
|---|---|
| `ORG` | Organizations to refresh. Default: all. |
| `--prune` | Also remove accounts and roles that were revoked. Without it they are kept and reported. |
| `--dry-run` | Show the changes without writing anything. |
| `--login` | Log in first if an organization has no session. |
| `--yes` | Do not ask for confirmation when profiles would be removed. |

Safety rules: roles used as `source` of an `assume_roles` entry are never pruned; `--prune` is refused if the portal returns no accounts or if the organization would end up empty; profiles written with an exact permission set name are never renamed. Nothing is written if `sync` would then fail. Exit code `1` if some organization could not be read (no session) - the others are still applied.

## `rolehop remove`

Removes an organization, an account or a single profile from this machine.

```text
rolehop remove TARGET ... [--dry-run] [--yes]
```

| Target | Example | What is removed |
|---|---|---|
| Organization | `acme` | The org, its `sso-session`, all its profiles, its local state and its cached SSO token |
| Account | `acme-prod` | The account and all its profiles |
| Profile | `acme-prod-admin` | That role only (the account goes too if it has no roles left) |
| Chained role | `acme-deploy-prod` | That `assume_roles` profile |

It asks for confirmation (use `--yes` in scripts). It refuses to remove a profile that is the `source` of a chained role, or to leave an organization empty - remove the whole organization instead, or use `rolehop uninstall` to remove everything.

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

## `rolehop upgrade`

Updates rolehop to the latest [GitHub Release](https://github.com/escobarjsa/rolehop/releases), using the same installer you used (`uv tool` or `pipx`).

```text
rolehop upgrade [--check] [--force]
```

| Option | Description |
|---|---|
| `--check` | Only report whether a newer version exists. |
| `--force` | Reinstall even if you already have the latest version. |

## `rolehop uninstall`

Removes everything rolehop added to this machine: the managed blocks in `~/.aws/config`, the rules and hooks of every AI agent, the shell block, the local state and the cached SSO tokens of your organizations.

```text
rolehop uninstall [--purge] [--dry-run] [--yes]
```

`orgs.toml` is kept so you can reinstall later; `--purge` also deletes it, its backups and any leftover `awsorg` configuration. Backups of `~/.aws/config` (`config.bak-*`) are listed but never deleted, because they contain your own profiles too. Finally, remove the command itself with `uv tool uninstall rolehop` (or `pipx uninstall rolehop`).

## `rolehop hook`

Internal entry points used by agent hooks. You normally do not run them yourself.

```text
rolehop hook post-tool [--format {claude,codex,copilot,gemini,qwen,text}]
rolehop hook cwd
```

- `post-tool` reads the hook payload on stdin and reacts to SSO expiry errors.
- `cwd` exports the direnv environment for Claude Code's `CwdChanged` event.
