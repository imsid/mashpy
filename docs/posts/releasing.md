---
title: Releasing Mash
description: How versioning, changelogs, and PyPI releases work.
---

# Releasing Mash

Releases are automated. A maintainer's only decision is **when to merge the
release PR** — everything else (version bump, changelog, tag, PyPI publish) is
handled by [release-please](https://github.com/googleapis/release-please).

## How it works

Every PR that lands on `main` must have a
[Conventional Commit](https://www.conventionalcommits.org/) title (enforced by
CI). Those commits drive the release pipeline:

1. As `feat:`/`fix:` commits land on `main`, release-please opens (or updates)
   a **release PR** titled `chore(main): release X.Y.Z`.
2. The release PR contains the updated `pyproject.toml` version and a new
   `CHANGELOG.md` entry.
3. A maintainer reviews and merges the release PR when ready to ship.
4. On merge, release-please creates the **git tag** and **GitHub Release**.
5. The `release-please.yml` workflow then **builds and publishes to PyPI**
   automatically via trusted publishing.

Contributors never bump versions or edit `CHANGELOG.md` by hand.

## Versioning

Mash follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).

| Commit type | Version bump | Example |
|---|---|---|
| `fix:`, `perf:` | patch | `0.6.12 → 0.6.13` |
| `feat:` | minor | `0.6.12 → 0.7.0` |
| `feat!:` / `BREAKING CHANGE:` footer | minor (pre-1.0) | `0.6.12 → 0.7.0` |
| `docs:`, `refactor:`, `ci:`, `chore:`, `test:` | none | no release PR |

While Mash is pre-1.0, breaking changes bump the **minor** version rather than
the major. After 1.0.0 ships, breaking changes bump the major.

## Commit types

| Type | Use for |
|---|---|
| `feat` | A new user-facing feature |
| `fix` | A bug fix |
| `perf` | A performance improvement |
| `deps` | Dependency updates |
| `docs` | Documentation only |
| `refactor` | Code restructuring with no behavior change |
| `test` | Test additions or fixes |
| `build` | Build system or packaging changes |
| `ci` | CI workflow changes |
| `chore` | Housekeeping (release PRs use this) |

A scope is optional: `fix(cli): ...`, `feat(runtime): ...`.

## Breaking changes

Mark a breaking change with a `!` after the type:

```
feat!: rename AgentSpec.build_llm to build_provider
```

Or with a `BREAKING CHANGE:` footer in the commit body:

```
feat(runtime): restructure host composition API

BREAKING CHANGE: HostBuilder.build() now returns AgentPool directly.
```

Either form triggers a minor bump (pre-1.0) and a "Breaking Changes" section
in the changelog.

## Cutting a release (maintainer checklist)

1. Wait for the open release PR (`chore(main): release X.Y.Z`) to reflect all
   the changes you want in the release. Additional `feat:`/`fix:` PRs merged
   after it opens will be added to it automatically.
2. Review the release PR — verify the version bump and changelog entries are
   correct.
3. Merge the release PR. No other steps needed.

After the merge:
- The git tag (`vX.Y.Z`) is created by release-please.
- The GitHub Release is created with the changelog as its body.
- The `release-please.yml` `publish` job triggers and pushes to PyPI via
  trusted publishing (environment: `pypi`, workflow: `release-please.yml`).

## Local checks before opening a PR

```bash
# Run the full test suite
uv run --extra dev pytest -q tests/mash

# Verify the package builds and metadata is valid
uv build && uvx twine check dist/*
```

## PyPI trusted publishing

The release workflow uses OIDC trusted publishing — no long-lived API tokens.
The publisher is configured on PyPI as:

- **Owner:** `imsid`
- **Repository:** `mashpy`
- **Workflow:** `release-please.yml`
- **Environment:** `pypi`

If publishing fails with an auth error, verify the trusted publisher on PyPI
matches these exact values.
