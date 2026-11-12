# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-11-12

### Changed
- **Everything is now in English**: CLI messages, prompts, errors, help texts, the rules installed into AI agents, the `orgs.toml` template, code comments, docstrings and tests. The agent rules tell agents to answer in the user's own language.
- Managed markers are now written in English (`managed by rolehop, do not edit`). Markers written by earlier versions (Spanish rolehop markers and `awsorg` markers) are still recognized and are replaced on the next `sync`, `setup` or `agent install`, and removed by `uninstall`.
- Yes/no prompts show `[y/N]`/`[Y/n]` and still accept `s`/`si`/`sí`.

### Added
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), issue and pull request templates, and Dependabot updates for GitHub Actions.

## [1.4.0] - 2026-11-12

### Added
- `rolehop refresh [ORG] [--prune] [--dry-run] [--login]`: re-reads accounts and permission sets from the AWS access portal with the current session, adds what was granted and, with `--prune`, removes what was revoked (never roles used by `assume_roles`). Runs `sync` automatically.
- `rolehop remove <org|org-account|profile>`: removes an organization (including its local state and cached SSO token), an account, a profile or a chained role, with confirmation.
- `rolehop uninstall [--purge]`: removes everything rolehop added to the machine (managed blocks, agent rules and hooks, shell block, state, cached SSO tokens).
- `rolehop upgrade [--check]`: updates to the latest GitHub Release with the same installer (uv or pipx).
- GitHub Releases: every `vX.Y.Z` tag publishes a Release with the wheel and sdist attached. PyPI publishing is opt-in (`PUBLISH_TO_PYPI` repository variable).

### Changed
- Agent rules: agents may run `rolehop refresh <org>` when a profile fails with `ForbiddenException` or an account is missing; pruning, removing and uninstalling only on request.

## [1.3.1] - 2026-11-06

### Fixed
- `--dry-run` no longer copies the legacy `awsorg` configuration and state.
- `setup` only reports adding the direnv hook when it actually adds it.

## [1.3.0] - 2026-11-06

### Changed
- **Renamed from `awsorg` to `rolehop`**: package, command, Python module and repository. `awslogin` is unchanged.
- Configuration moved to `~/.config/rolehop/orgs.toml` and state to `~/.local/state/rolehop/`.
- Environment variables renamed to `ROLEHOP_CONFIG`, `ROLEHOP_SSO_CACHE` and `ROLEHOP_PORTAL_ENDPOINT`.
- README and documentation rewritten in English.

### Added
- Automatic migration from `awsorg`: configuration and state are copied on first run, and legacy managed blocks in `~/.aws/config`, shell rc files, agent rules and agent hooks are adopted and replaced without duplicates. Old `AWSORG_*` variables are still honoured.
- Documentation site built with [Zensical](https://zensical.org), published to GitHub Pages.

## [1.2.0] - 2026-11-06

First release as a Python package (as `awsorg`).

### Added
- `setup`: all-in-one (shell `PATH` and direnv hook, wizard when there is no configuration, profiles, AI agents).
- `agent list|install|uninstall|rules`: rules and hooks for Claude Code, Codex, Kiro, Copilot CLI, Gemini CLI, Qwen Code, OpenCode, Amp, Pi, Devin Desktop/Windsurf and Cursor.
- `hook post-tool` and `hook cwd`: Python hooks (no `jq`), with per-agent payload formats.
- `run`: checks and renews the session before leaving an agent working; reports blocked organizations at the end.
- `awslogin` shipped as a package entry point.
- `envrc --state-var` to choose the remote-state profile variable.

### Inherited from the script versions (1.0–1.1)
- Declarative `sso-session` profiles for any number of organizations, `init` wizard with discovery through the AWS access portal API (standard library), `discover`, `status`, `login`, full validation, atomic writes, symlink support and backups.

[1.5.0]: https://github.com/escobarjsa/rolehop/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/escobarjsa/rolehop/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/escobarjsa/rolehop/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/escobarjsa/rolehop/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/escobarjsa/rolehop/releases/tag/v1.2.0
