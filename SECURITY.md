# Security Policy

## Reporting a vulnerability

Please use **GitHub → Security → Report a vulnerability** on this repository to report privately. Do not open a public issue for security problems.

## Security model

- `rolehop` never stores or writes tokens or keys. Login and credential refresh are handled by the AWS CLI.
- `init` and `discover` read the SSO token from the AWS CLI cache in memory only, to query the AWS access portal (`portal.sso.<region>.amazonaws.com`). It is never printed or logged.
- `orgs.toml` contains no secrets. Every value is still validated before `~/.aws/config` is written.
- The rules installed into AI agents forbid reading `~/.aws` and exporting credentials. Treat them as guidance, not a barrier: protect your machine with each agent's own sandbox and permission features.

See the [security documentation](https://escobarjsa.github.io/rolehop/reference/security/) for details.
