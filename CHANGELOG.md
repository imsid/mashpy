# Changelog

This file is generated automatically by
[release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/) landed on `main`.
Do not edit it by hand.

Mash follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While
Mash is pre-1.0, breaking changes (`feat!:` / `fix!:` / a `BREAKING CHANGE:`
footer) bump the **minor** version; once 1.0.0 ships they bump the major.

## [0.8.0](https://github.com/imsid/mashpy/compare/mashpy-v0.7.2...mashpy-v0.8.0) (2026-06-22)


### Features

* **gemini:** migrate GeminiProvider to the Interactions API ([#63](https://github.com/imsid/mashpy/issues/63)) ([4bf2512](https://github.com/imsid/mashpy/commit/4bf251224c85dd4ebfbb3782122db1c035817371))


### Documentation

* revise internals series posts and replace two-stores with persistence-store ([#60](https://github.com/imsid/mashpy/issues/60)) ([22fc70f](https://github.com/imsid/mashpy/commit/22fc70f9a1f7057a3dddf57edfdfb09d520f7a04))

## [0.7.2](https://github.com/imsid/mashpy/compare/mashpy-v0.7.1...mashpy-v0.7.2) (2026-06-22)


### Bug Fixes

* **mcp:** stop phantom session creation from MCP startup events ([#58](https://github.com/imsid/mashpy/issues/58)) ([1e42175](https://github.com/imsid/mashpy/commit/1e42175fc212549f1dc7227601b1abcd4dc19961))
* **telemetry:** eliminate token double-counting in session and trace aggregations ([#56](https://github.com/imsid/mashpy/issues/56)) ([5cdfe33](https://github.com/imsid/mashpy/commit/5cdfe33876fef4f0feaf6660414b598c1fd4d1d0))


### Documentation

* add /triage skill, NOTICE file, and dev flow note ([#59](https://github.com/imsid/mashpy/issues/59)) ([5a84c76](https://github.com/imsid/mashpy/commit/5a84c767165fcbc61eea0674148ad41808aa5c97))

## [0.7.1](https://github.com/imsid/mashpy/compare/mashpy-v0.7.0...mashpy-v0.7.1) (2026-06-21)


### Bug Fixes

* **packaging:** include SQL migration files in wheel ([fd75e51](https://github.com/imsid/mashpy/commit/fd75e51a90130a942240a0a504b186d2bcab3c7a))

## [0.7.0](https://github.com/imsid/mashpy/compare/mashpy-v0.6.12...mashpy-v0.7.0) (2026-06-21)


### Features

* **runtime:** expose cached token counts at trace and session level ([#48](https://github.com/imsid/mashpy/issues/48)) ([5108e71](https://github.com/imsid/mashpy/commit/5108e71c6ed64cb98c0b2119995ef1f677b929eb))


### Documentation

* add Code of Conduct and release process guide ([#44](https://github.com/imsid/mashpy/issues/44)) ([5c2057f](https://github.com/imsid/mashpy/commit/5c2057f017ce9c60cfccf3a0f59fde3674b6fa94))
* correct posts for the one-session model ([9e310e7](https://github.com/imsid/mashpy/commit/9e310e719a9d6bc948f87a81f4c1e7fd69c80ce6))
* correct posts for the one-session model ([e4a0010](https://github.com/imsid/mashpy/commit/e4a00108b8770979e4cfc140c80eb7a3046a78cb))
* fix prose and nav issues in index and product brief ([#49](https://github.com/imsid/mashpy/issues/49)) ([2d1e949](https://github.com/imsid/mashpy/commit/2d1e9494c62f324a2406966adc6e09838f432c8e))
* update blog posts from telemetry UI to the admin dashboard ([7054f8f](https://github.com/imsid/mashpy/commit/7054f8f258639f6c044a6abf3177df5e4dd43c37))


### Refactors

* **runtime:** split RuntimeStore and remove SQLite memory backend ([#52](https://github.com/imsid/mashpy/issues/52)) ([8bea95e](https://github.com/imsid/mashpy/commit/8bea95e0f7cbf859748c59c15ecb78f887678eb7))

## 0.6.12

Baseline release. Automated changelog entries begin with the next release;
earlier history lives in the git log and release tags.
