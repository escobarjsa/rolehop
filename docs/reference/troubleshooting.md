# Troubleshooting

| Symptom | Fix |
|---|---|
| `✗ <org> … sin sesión · ejecuta: awslogin <org>` | Run `awslogin <org>`. |
| `Token has expired and refresh failed` / `SSO session … expired` | The portal session ended: `awslogin <org>`. |
| `command not found: rolehop` or `awslogin` | Open a new terminal, or run `rolehop setup` again to fix `PATH`. |
| `The config profile (x) could not be found` | Check the exact name with `rolehop list`. |
| `ForbiddenException` on one profile | That role was revoked: `rolehop refresh --prune` (or `rolehop remove <profile>`). |
| A new account or role is missing | `rolehop refresh`. |
| `sync` reports sections that "chocan" (conflict) | An older profile with the same name exists outside the managed blocks. Rename or delete it, then `rolehop sync`. |
| `no es TOML válido … line N` | Syntax error in `orgs.toml` at that line. |
| `rolehop run` says «No arranco» (exit 75) | The session does not cover the job: use `--login`, a smaller `--max`, or split the work. |
| `status` shows ✓ but commands fail | Cached role credentials can outlive the portal session. The agent hook corrects the state as soon as a command fails; run `awslogin <org>`. |
| Something looks wrong in `~/.aws/config` | Restore the latest `~/.aws/config.bak-<timestamp>`. |

## Known limitations

- The remaining portal session time is an **estimate** based on your last `awslogin` and `portal_session_hours`. AWS does not expose the real value.
- `status` may report ✓ while role credentials are still cached after the portal session has ended.
- Linux, WSL and macOS only. On Windows, use WSL.
- CLI messages are currently in Spanish.

## Getting help

Open an issue on [GitHub](https://github.com/escobarjsa/rolehop/issues) with the output of:

```bash
rolehop --version
aws --version
rolehop status --json
rolehop sync --dry-run
```

!!! danger "Never share"

    Do not paste the contents of `~/.aws/sso/cache`, `~/.aws/credentials`, or any access key in an issue.
