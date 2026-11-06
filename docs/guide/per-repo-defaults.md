# Per-repository defaults

With [direnv](https://direnv.net/), each repository can declare its default profile. Humans and agents can still override it per command.

## Generate an `.envrc`

```bash
rolehop envrc acme-dev-admin > .envrc
direnv allow
```

The generated file exports `AWS_PROFILE`, and unsets any static `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` that could shadow the profile.

## Remote state in another account

Terraform and Terragrunt setups often keep state in a central account. Add a second profile for it:

```bash
rolehop envrc acme-dev-admin --state acme-devsecops-admin > .envrc
```

This also exports `AWS_STATE_PROFILE=acme-devsecops-admin`. Use `--state-var` to choose another variable name that your Terragrunt configuration reads:

```bash
rolehop envrc acme-dev-admin --state acme-devsecops-admin --state-var TG_STATE_PROFILE
```

## Agents and direnv

- In **Claude Code**, the `CwdChanged` hook (`rolehop hook cwd`) loads the repository's `.envrc` when the agent changes directory.
- Agents are told **never** to run `direnv allow`: approving an `.envrc` is always your decision. A modified `.envrc` stays blocked until you approve it.
- The `.envrc` value is just the default. Agents may still prefix commands with another `AWS_PROFILE=…`.
