# Security

## Security model

- **No secrets at rest.** `rolehop` never stores or writes tokens or keys. Login and credential refresh are handled by the AWS CLI and SDKs.
- **Token in memory only.** `init` and `discover` read the cached SSO token in memory to query the AWS access portal (`portal.sso.<region>.amazonaws.com`) and never print or log it.
- **No injection.** Every value from `orgs.toml` is validated against strict patterns (names, HTTPS URLs, regions, 12-digit IDs, IAM role ARNs) before anything reaches `~/.aws/config`. Newlines, brackets and comment characters are rejected.
- **Safe writes.** Validated output is parsed as INI, written atomically with mode `0600`, with a timestamped backup, and only inside `rolehop`'s own blocks.
- **Least surprise for agents.** Agent rules forbid reading `~/.aws`, exporting keys, using `aws configure export-credentials`, or starting interactive logins. On Claude Code this is enforced with `deny` permission rules.

## What `rolehop` does not do

- It does not raise or change IAM Identity Center session durations.
- It does not limit which roles an agent may use - choose the permission sets you grant your user accordingly, and use each agent's sandbox and permission features.
- It cannot keep an agent working with AWS after the portal session ends. That is a property of SSO, by design. Use pipelines with their own roles (OIDC) for unattended infrastructure changes.

## Reporting a vulnerability

Please use **GitHub → Security → Report a vulnerability** on the [repository](https://github.com/escobarjsa/rolehop/security) to report privately. Do not open a public issue for security problems.
