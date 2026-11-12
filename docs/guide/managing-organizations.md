# Managing organizations

## Common tasks

| I want to… | Do this |
|---|---|
| Add another organization | `rolehop init` → option **1** (add an organization) → `rolehop sync` → `awslogin <org>` |
| Pick up accounts or roles I was just granted | `rolehop refresh` |
| Drop accounts or roles that were revoked | `rolehop refresh --prune` |
| See the real accounts and roles in the portal | `rolehop discover <org>` (requires a session) |
| Edit by hand | edit `orgs.toml` → `rolehop sync --dry-run` → `rolehop sync` |
| Remove a profile, an account or an organization | `rolehop remove acme-prod-admin` · `rolehop remove acme-prod` · `rolehop remove acme` |
| Remove everything from this machine | `rolehop uninstall` |

## When your permissions change

If your administrator grants or revokes accounts or permission sets, refresh from the portal:

```bash
rolehop refresh --dry-run     # see what changed
rolehop refresh               # add new accounts and roles, keep revoked ones (reported with !)
rolehop refresh --prune       # also remove what was revoked (asks for confirmation)
```

`refresh` uses your current session (run `awslogin` first, or add `--login`), never asks questions, and runs `sync` for you. Revoked roles are only removed with `--prune`, and roles that are the `source` of a chained role are always kept.

```console
$ rolehop refresh --prune
acme: 9 account(s) in the portal
  + role acme-prod-ro (ReadOnly)
  + account billing (444444444444): admin
  - role acme-sandbox-power (PowerUser): revoked
```

!!! tip "Let your agent do it"

    If a profile starts failing with `ForbiddenException` or a new account is missing, your AI agent may run `rolehop refresh <org>` (it only adds). Pruning and removing are left to you.

## Removing profiles

```bash
rolehop remove acme-prod-admin        # one profile
rolehop remove acme-prod              # a whole account
rolehop remove personal               # a whole organization (also signs it out on this machine)
rolehop remove acme-prod --dry-run    # preview
```

Every change validates first and keeps timestamped backups of `orgs.toml` and `~/.aws/config`.

## Using the wizard

`rolehop init` is safe to run any time. When `orgs.toml` already exists, it offers to add an organization or to update an existing one with freshly discovered accounts and roles.

!!! warning "Comments are not preserved"

    The wizard rewrites `orgs.toml`. If you added comments by hand, they will be lost — a timestamped backup is written first.

## Discovering accounts and roles

```bash
rolehop discover acme
```

Lists every account and permission set your user can access in that portal, using the AWS access portal API (`portal.sso.<region>.amazonaws.com`) and the cached SSO token in memory. Use it to compare with `orgs.toml`.

## Adding an account by hand

Append under its `[[org]]`, before the next `[[org]]`:

```toml
[[org.accounts]]
name = "billing"            # short name, lowercase, no spaces
id = "123456789012"         # 12 digits, quoted
roles = ["admin"]           # alias from [org.roles] or exact permission set name
```

Then:

```bash
rolehop sync --dry-run
rolehop sync
```

## Conflicts with existing profiles

If `~/.aws/config` already has a profile, outside the managed blocks, with the same name as a generated one, `sync` refuses to write and lists the conflicts. Rename or delete the old section (take a copy first) and run `sync` again.
