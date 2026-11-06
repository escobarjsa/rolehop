# Daily workflow

## At a glance

| Situation | Command |
|---|---|
| Start the day | `awslogin` |
| See every profile | `rolehop list` |
| See session status per organization | `rolehop status` |
| Use a profile | `AWS_PROFILE=acme-prod-admin aws s3 ls` |
| Log in to one organization | `awslogin acme` |
| Renew now, even with a live session | `awslogin --force` |
| Before leaving an agent working | `rolehop run --login --max 6h` |

## Morning login

```bash
awslogin
```

`awslogin` checks each organization and runs `aws sso login --sso-session <org>` **only where needed**. Organizations that already have a session are skipped:

```console
$ awslogin
✓ acme: ya tiene sesión (~6h 40m restantes). Usa --force para renovarla.
→ personal: aws sso login --sso-session personal --use-device-code
✓ personal: sesión iniciada · cuenta 333333333333
```

!!! note "CLI language"

    `rolehop` messages are currently in Spanish. Commands, flags, file formats and exit codes are stable and documented in English here.

One login per organization unlocks **all** of its accounts and roles. Inside the portal session, the AWS CLI and SDKs renew each role's credentials whenever they expire — an Administrator role every hour, a ReadOnly role every eight — and nobody notices.

## Checking the session

```bash
rolehop status          # human-readable
rolehop status --json   # for scripts and agents
```

`status` probes each organization with a cheap `sts get-caller-identity` call and shows the **estimated** time left in the portal session.

!!! warning "The remaining time is an estimate"

    AWS does not expose when a portal session ends. `rolehop` estimates it from the time of your last `awslogin` and `portal_session_hours` in `orgs.toml`.

## When the session ends

You will see errors like `Token has expired and refresh failed` or `The SSO session associated with this profile has expired`. Run:

```bash
awslogin <org>
```

and continue. If an AI agent hits the error, its hook marks the organization as blocked and the agent asks you to log in; the next `awslogin` logs in straight away without probing.

## Approving from your phone

`awslogin` uses the **device code** flow by default (ideal for WSL, SSH and remote terminals). The verification link works in any browser, so you can approve a login from your phone when you are away from your desk.
