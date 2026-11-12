# Installation

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11 or newer | Uses `tomllib` from the standard library |
| AWS CLI | v2, 2.22 or newer | Handles login and credential refresh |
| OS | Linux, WSL, macOS | Windows is supported through WSL |
| direnv | optional | Only for [per-repository defaults](../guide/per-repo-defaults.md) |

## Install the package

=== "uv (recommended)"

    ```bash
    uv tool install rolehop
    ```

=== "pipx"

    ```bash
    pipx install rolehop
    ```

=== "From GitHub"

    ```bash
    uv tool install git+https://github.com/escobarjsa/rolehop
    # or pin a release
    uv tool install git+https://github.com/escobarjsa/rolehop@v1.5.0
    ```

=== "From a GitHub Release"

    Every release attaches a ready-to-install wheel. No git needed:

    ```bash
    uv tool install https://github.com/escobarjsa/rolehop/releases/download/v1.5.0/rolehop-1.5.0-py3-none-any.whl
    ```

    See all versions on the [Releases page](https://github.com/escobarjsa/rolehop/releases).

This installs two commands:

`rolehop`
:   The main CLI: setup, profiles, status, agents and hooks.

`awslogin`
:   A shortcut for `rolehop login`: the one command you run every morning.

## Run the setup

```bash
rolehop setup
```

`setup` is an all-in-one, idempotent command. Each step can be re-run safely:

1. **Shell** — adds `~/.local/bin` to your `PATH` (zsh, bash or fish) and the direnv hook if direnv is installed, inside a marked block with a backup of your rc file.
2. **Organizations** — if `~/.config/rolehop/orgs.toml` does not exist, launches the interactive [wizard](quickstart.md).
3. **Profiles** — writes your profiles into `~/.aws/config`, only inside managed blocks.
4. **AI agents** — installs rules and hooks into the agents it detects.

!!! note "Open a new terminal"

    If `setup` tells you to open a new terminal, do it (or run `exec $SHELL`) so the updated `PATH` takes effect.

Useful flags:

```bash
rolehop setup --dry-run          # show what would change
rolehop setup --no-agents        # skip AI agent integration
rolehop setup --agent claude     # only this agent (repeatable)
```

## Upgrade

```bash
rolehop upgrade --check     # is there a newer release?
rolehop upgrade             # install it, with the same installer you used
```

`rolehop upgrade` reads the latest [GitHub Release](https://github.com/escobarjsa/rolehop/releases) and reinstalls from its wheel. Your configuration and profiles are untouched. Versions older than 1.4.0 do not have this command; upgrade them once by hand:

```bash
uv tool install --force https://github.com/escobarjsa/rolehop/releases/download/v1.5.0/rolehop-1.5.0-py3-none-any.whl
```

## Uninstall

```bash
rolehop uninstall           # profiles, agent rules and hooks, shell block, state and cached SSO tokens
uv tool uninstall rolehop   # then remove the command
```

`orgs.toml` is kept so you can reinstall later; add `--purge` to delete it as well.
