# Configuration (`orgs.toml`)

`rolehop` is configured by a single declarative file:

```
~/.config/rolehop/orgs.toml
```

Override the location with the `ROLEHOP_CONFIG` environment variable. The file contains **no secrets**: names, URLs, regions, account IDs and permission set names only.

## Full example

```toml
[[org]]
name = "acme"                                  # lowercase, digits, - or _
start_url = "https://acme.awsapps.com/start"   # AWS access portal URL
sso_region = "us-east-1"                       # region of IAM Identity Center
region = "us-east-1"                           # default region for the profiles
login_flow = "device"                          # "device" (recommended on WSL/SSH) or "pkce"
portal_session_hours = 8                       # portal session length, only for estimates
default_role = "ro"                            # role used by `rolehop status` to probe
# probe_profile = "acme-devsecops-ro"          # optional fixed probe profile

# Short aliases → exact permission set names in IAM Identity Center
[org.roles]
ro = "ReadOnly"
power = "PowerUser"
admin = "Administrator"

[[org.accounts]]
name = "devsecops"          # becomes part of the profile name: acme-devsecops-admin
id = "111111111111"
roles = ["ro", "power", "admin"]

[[org.accounts]]
name = "prod"
id = "222222222222"
roles = ["ro", "admin"]
region = "us-east-2"        # optional per-account region

# Optional: IAM roles assumed from an SSO profile (role chaining)
[[org.assume_roles]]
name = "deploy-prod"                                  # → profile acme-deploy-prod
role_arn = "arn:aws:iam::222222222222:role/DeployRole"
source = "devsecops-admin"                            # <account>-<role> of this org
# external_id = "optional"
# duration_seconds = 3600

# A second organization (another Identity Center portal)
[[org]]
name = "personal"
start_url = "https://personal.awsapps.com/start"
sso_region = "us-east-1"
default_role = "admin"

[org.roles]
admin = "AdministratorAccess"

[[org.accounts]]
name = "main"
id = "333333333333"
roles = ["admin"]
```

## `[[org]]`

| Key | Required | Default | Description |
|---|---|---|---|
| `name` | yes | — | Short organization name. Prefix of every profile and name of the `sso-session`. |
| `start_url` | yes | — | AWS access portal URL, `https://<something>.awsapps.com/start`. |
| `sso_region` | yes | — | Region where IAM Identity Center is enabled. |
| `region` | no | `sso_region` | Default region written to each profile. |
| `login_flow` | no | `device` | `device` (device code, works anywhere) or `pkce` (browser on the same machine). |
| `portal_session_hours` | no | `8` | Your portal session length. Used **only** to estimate remaining time. |
| `default_role` | no | — | Role alias used to choose the probe profile for `status`. |
| `probe_profile` | no | — | Exact profile used to probe the session. Must exist in this org. |

## `[org.roles]`

A table of `alias = "PermissionSetName"`. Aliases keep profile names short (`acme-prod-admin` instead of `acme-prod-administratoraccess`).

In `roles = [...]` of an account you can use an alias **or** the exact permission set name. A name without an alias is slugified for the profile (`"Billing Admin"` → `billing-admin`).

## `[[org.accounts]]`

| Key | Required | Description |
|---|---|---|
| `name` | yes | Short account name, part of the profile name. |
| `id` | yes | 12-digit AWS account ID, as a string. |
| `roles` | yes | List of role aliases or permission set names you have in that account. |
| `region` | no | Region for this account's profiles. |

## `[[org.assume_roles]]`

Generates a profile with `role_arn` and `source_profile`, so the SDK chains it on top of an SSO profile and renews it automatically. Each link in the chain lasts at most one hour, but is refreshed transparently.

| Key | Required | Description |
|---|---|---|
| `name` | yes | Profile suffix: the profile is `<org>-<name>`. |
| `role_arn` | yes | ARN of the IAM role to assume. |
| `source` | yes | `<account>-<role>` of an SSO profile in the same org. |
| `external_id` | no | External ID, if the trust policy requires it. |
| `duration_seconds` | no | Session duration for the assumed role. |
| `region` | no | Region for this profile. |

## What gets written to `~/.aws/config`

For each organization, `rolehop sync` writes one managed block:

```ini
# >>> rolehop:acme (gestionado por rolehop, no editar) >>>
[sso-session acme]
sso_start_url = https://acme.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile acme-devsecops-admin]
sso_session = acme
sso_account_id = 111111111111
sso_role_name = Administrator
region = us-east-1
# <<< rolehop:acme <<<
```

`sso_registration_scopes = sso:account:access` is what gives the AWS CLI a **refresh token**, which is why credentials keep renewing for the whole portal session.

## Validation

Before anything is written, every value is validated: names, URLs, regions, 12-digit IDs, ARNs, duplicates, `source` references and `probe_profile`. The result is then parsed as INI. If anything fails, nothing is written and you get an error with the exact problem.

## Environment variables

| Variable | Purpose |
|---|---|
| `ROLEHOP_CONFIG` | Path to `orgs.toml`. |
| `AWS_CONFIG_FILE` | Path to the AWS config file (default `~/.aws/config`). |
| `XDG_STATE_HOME` | Base directory for state (default `~/.local/state`, state lives in `rolehop/`). |
| `ROLEHOP_SSO_CACHE` | AWS CLI SSO cache directory (default `~/.aws/sso/cache`). |

The previous `AWSORG_*` names are still honoured. See [Migrating from awsorg](../reference/migrating-from-awsorg.md).
