# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.3.0]: https://github.com/escobarjsa/rolehop/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/escobarjsa/rolehop/releases/tag/v1.2.0
