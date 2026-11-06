# Managing organizations

## Common tasks

| I want to… | Do this |
|---|---|
| Add another organization | `rolehop init` → option **1** (add an organization) → `rolehop sync` → `awslogin <org>` |
| Add an account or role I was just granted | `rolehop init` → option **2** (update accounts and roles from the portal) → `rolehop sync` |
| See the real accounts and roles in the portal | `rolehop discover <org>` (requires a session) |
| Edit by hand | edit `orgs.toml` → `rolehop sync --dry-run` → `rolehop sync` |
| Remove an organization | delete it from `orgs.toml` → `rolehop sync --prune` |

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

## Removing an organization

Delete its `[[org]]` table and run:

```bash
rolehop sync --prune
```

Without `--prune`, blocks for organizations that are no longer in `orgs.toml` are **kept** and reported, so nothing disappears by accident.

## Conflicts with existing profiles

If `~/.aws/config` already has a profile, outside the managed blocks, with the same name as a generated one, `sync` refuses to write and lists the conflicts. Rename or delete the old section (take a copy first) and run `sync` again.
