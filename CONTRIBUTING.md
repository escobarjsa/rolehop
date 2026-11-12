# Contributing to rolehop

Thanks for your interest in improving rolehop! This guide explains how to set up a development environment, the standards we follow, and how to get a change merged.

## Ground rules

- Be respectful. This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Never include real credentials, account IDs, start URLs or SSO tokens** in issues, pull requests, tests or fixtures. Use the placeholder IDs already used in the tests (`111111111111`, `222222222222`, …).
- Report security problems privately — see [SECURITY.md](SECURITY.md).

## Development setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and (optionally, for manual testing) AWS CLI v2.

```bash
git clone https://github.com/escobarjsa/rolehop.git
cd rolehop
uv sync --group dev
uv run rolehop --help
```

## Checks

Run these before opening a pull request — CI runs the same checks on Linux and macOS with Python 3.11–3.13:

```bash
uv run --group dev ruff check src tests     # lint
uv run --group dev pytest                    # tests
uv build                                     # package builds
uv run --group docs zensical build --clean   # docs build (only if you changed docs/)
```

Preview the documentation locally with `uv run --group docs zensical serve`.

## Project principles

These are deliberate design choices; changes that break them are unlikely to be accepted:

1. **No runtime dependencies.** The package uses the Python standard library only.
2. **Never handle credentials.** Login and credential refresh belong to the AWS CLI and SDKs. rolehop may read the cached SSO access token in memory to call the access portal API, and must never print, log or persist it.
3. **Validate before writing.** Every write to `~/.aws/config`, `orgs.toml`, shell rc files or agent configuration is validated first, backed up, written atomically where possible, and limited to rolehop's own marked blocks.
4. **Idempotent and reversible.** Running a command twice yields the same result, and everything rolehop adds can be removed (`rolehop uninstall`).
5. **Backwards compatible on disk.** Users have files written by older versions (including the former `awsorg` name and older marker texts). New versions must keep recognizing and migrating them.
6. **Tests never touch the real home directory or the network.** Use the fixtures in `tests/conftest.py` and the local mock servers in the tests.

## Making a change

1. Open an issue first for anything beyond a small fix, so we can agree on the approach.
2. Create a branch from `main`.
3. Add or update tests for every behavior change. Bug fixes should include a test that fails without the fix.
4. Update the documentation in `docs/` and add an entry under an `Unreleased` heading in [CHANGELOG.md](CHANGELOG.md) following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
5. Use clear commit messages in the imperative mood; [Conventional Commits](https://www.conventionalcommits.org/) prefixes (`feat:`, `fix:`, `docs:`…) are appreciated.
6. Open a pull request and fill in the template.

## Coding style

- Code, comments, docstrings, messages and documentation are written in **English**.
- Follow the existing style; `ruff` enforces the lint rules (line length 130).
- Type-annotate public functions. Keep functions small and side effects explicit.
- User-facing messages are short and actionable, and tell the user the exact command to run next.

## Releasing (maintainers)

1. Update the version in `pyproject.toml` and `src/rolehop/__init__.py`, and move the `Unreleased` changelog entries under the new version.
2. Commit, then tag and push: `git tag -a vX.Y.Z -m "rolehop X.Y.Z" && git push origin main vX.Y.Z`.
3. The `Release` workflow checks that the tag matches the package version, runs the checks, builds the wheel and sdist, and publishes a GitHub Release with both attached. PyPI publishing runs only when the repository variable `PUBLISH_TO_PYPI` is `true`.
