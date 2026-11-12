# Quickstart

This walkthrough takes about five minutes. You need the **AWS access portal URL** of your organization - the page you open to see your accounts, for example `https://acme.awsapps.com/start`.

## 1. Run the wizard

```bash
rolehop init
```

The wizard asks for:

| Prompt | Example | Notes |
|---|---|---|
| Short name | `acme` | Lowercase letters, digits, `-` or `_`. It prefixes every profile. |
| Portal URL | `https://acme.awsapps.com/start` | Your AWS access portal. |
| SSO region | `us-east-1` | Region where IAM Identity Center lives. |

It then starts a device-code login, and **discovers your accounts and permission sets** through the AWS access portal API. You choose which ones to keep and give each account a short name.

!!! info "Nothing secret is stored"

    The wizard reads the SSO access token from the AWS CLI cache **in memory only** to list your accounts. `orgs.toml` contains names, account IDs and role names - no secrets.

## 2. Write the profiles

```bash
rolehop sync --dry-run   # review the diff
rolehop sync             # apply
```

## 3. Log in and check

```bash
awslogin
rolehop status
rolehop list
```

`awslogin` prints a code and a link. Open it in any browser - your laptop or your phone - confirm the code matches, and approve.

## 4. Use a profile

```bash
AWS_PROFILE=acme-devsecops-admin aws sts get-caller-identity
AWS_PROFILE=acme-prod-readonly aws s3 ls
```

Profile names follow `<org>-<account>-<role>`. Terraform, OpenTofu, Terragrunt, boto3 and any AWS SDK pick them up through `AWS_PROFILE`.

## 5. Tell your AI agent

If you ran `rolehop setup`, your agents already know. Just say:

> *Use whatever AWS profiles and roles you need.*

or name the target: *"list the buckets in prod"*. The agent picks `acme-prod-…`, says which profile it used, and asks you to run `awslogin acme` if the session ends.

## Next steps

- [Daily workflow](../guide/daily-workflow.md)
- [Add more organizations, accounts and roles](../guide/managing-organizations.md)
- [`orgs.toml` reference](../guide/configuration.md)
