# Migrating from awsorg

`rolehop` was called **awsorg** up to version 1.2.0. Version 1.3.0 renamed the package, the command and the Python module. Existing installations are adopted automatically.

## Steps

```bash
uv tool uninstall awsorg          # if you installed the old package
uv tool install rolehop
rolehop setup
```

!!! note "Old script install"

    If you used the pre-package script kit, `awsorg` and `awslogin` live as plain files in `~/.local/bin`. Remove them first, or install with `uv tool install --force rolehop` so `awslogin` is replaced.

## What is migrated automatically

| Item | Before | After | How |
|---|---|---|---|
| Configuration | `~/.config/awsorg/orgs.toml` | `~/.config/rolehop/orgs.toml` | Copied on first run; the original is kept. |
| State | `~/.local/state/awsorg/` | `~/.local/state/rolehop/` | Copied on first run. |
| Blocks in `~/.aws/config` | `# >>> awsorg:<org>` | `# >>> rolehop:<org>` | Adopted and rewritten by `rolehop sync`, without duplicating profiles. |
| Shell block | `# >>> awsorg …` in `.zshrc`/`.bashrc` | `# >>> rolehop …` | Replaced by `rolehop setup`. |
| Agent rules | `<!-- >>> awsorg … -->` blocks | `<!-- >>> rolehop … -->` | Replaced by `rolehop agent install`. |
| Agent hooks | `awsorg hook …` | `rolehop hook …` | Replaced; Copilot's `awsorg.json` is removed. |
| Environment variables | `AWSORG_CONFIG`, `AWSORG_SSO_CACHE` | `ROLEHOP_*` | Old names still honoured. |

**Profile names do not change.** `acme-prod-admin` is still `acme-prod-admin`, so scripts, `.envrc` files and CI keep working.

## Cleaning up

Once you are happy with `rolehop`, you can delete `~/.config/awsorg/` and `~/.local/state/awsorg/`.
