# AWS with rolehop

The user works with multiple AWS IAM Identity Center organizations managed by **rolehop**. Follow these rules in every project. Reply to the user in their own language.

## Credentials and profiles

- **Always use profiles**: prefix every command (`aws`, `terraform`, `tofu`, `terragrunt`, boto3 scripts) with `AWS_PROFILE=<profile>`. If a project uses a different variable for the remote-state profile (for example `TF_STATE_PROFILE`), set it the same way.
- **Catalog:** `rolehop list` (or `rolehop list --json`) shows every profile as `<org>-<account>-<role>`, for example `acme-devsecops-admin`. Check it to pick one.
- **Choose freely** whichever profile and role the task needs, Admin included, without asking for permission. Prefer a read-only one for queries when available, and state in one line which profile you used.
- When switching account or role, confirm with `AWS_PROFILE=<profile> aws sts get-caller-identity`.
- When starting AWS work, run `rolehop status` to see which organizations have an active session.
- If the repo has an `.envrc`, its `AWS_PROFILE` is only the default; you may use a different one per command.
- If the user asks to add an organization, point them to `rolehop init` (interactive wizard).
- If an account or role is missing, or a profile fails with `ForbiddenException` (role revoked), you may run `rolehop refresh <org>`: it reloads accounts and roles from the portal and only adds. Remove profiles (`rolehop refresh --prune`, `rolehop remove`) or uninstall (`rolehop uninstall`) only when the user asks.

## Forbidden

- Reading `~/.aws`, pasting or exporting keys (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`), or asking the user for the portal's "access keys".
- Using `aws configure export-credentials`, `aws configure get`, `aws sso get-role-credentials`, `assume` or `granted`.
- Running `direnv allow` (the user approves `.envrc` files).
- Running `awslogin`, `rolehop login`, `aws sso login` or `rolehop run --login` yourself: they require the user in the browser.

## When the session expires

If a command returns `ExpiredToken`, `Token has expired`, `SSO session … expired`, `InvalidGrant`, `cannot be refreshed` or similar:
1. **Stop** all AWS work. Do not retry, and do not switch to another profile or organization to work around it.
2. Ask the user to run in their terminal: `awslogin <org>` (the org is the profile prefix).
3. Once they confirm, resume where you left off. Meanwhile, continue with anything that does not use AWS.

Each role's credentials renew automatically during the portal session. `awslogin` is needed only when that session ends.

## When the user leaves you working

If the user says something like "I'm heading out", "keep working" or "take it from here" and the task uses AWS:
1. Before they leave, ask them to run: `rolehop run --login --max <estimated duration>` (renews the session and checks it lasts long enough). Suggest a duration based on the remaining work, for example `--max 6h`.
2. Once they confirm, run `rolehop status`. If the session won't last long enough, tell them the maximum available and how to split the work.
3. Reorder the plan: **everything that needs AWS first**, then the rest.
4. If the session expires while the user is away, stop the AWS work, continue with the rest, and finish with a summary of what is left plus the command `awslogin <org>`. Remind them that the device code can be approved from their phone.
5. Infrastructure changes that must be applied without the user go through a PR and pipeline, never with the user's credentials.
