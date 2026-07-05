# Changelog

This file is generated automatically by
[release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/) landed on `main`.
Do not edit it by hand.

Mash follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While
Mash is pre-1.0, breaking changes (`feat!:` / `fix!:` / a `BREAKING CHANGE:`
footer) bump the **minor** version; once 1.0.0 ships they bump the major.

## [0.16.0](https://github.com/imsid/mashpy/compare/mashpy-v0.15.0...mashpy-v0.16.0) (2026-07-05)


### Features

* **admin-ui:** cache breakdowns, participant/workflow log filters, workflow activity ([#129](https://github.com/imsid/mashpy/issues/129)) ([3a18d70](https://github.com/imsid/mashpy/commit/3a18d70e251bfaaf0801f1e77b935175c6eeaf3a))
* **admin-ui:** group sidebar nav into Deployment and Activity sections ([#126](https://github.com/imsid/mashpy/issues/126)) ([3760262](https://github.com/imsid/mashpy/commit/376026296391cca7d1564f522395fbd120de9edd))
* **evals:** synthetic evals — datasets, rubrics, experiments, scoring ([#124](https://github.com/imsid/mashpy/issues/124)) ([145b41f](https://github.com/imsid/mashpy/commit/145b41f9807777822efb4f2b50535f65b73e7452))
* **runtime:** attach masher workflows to built hosts when masher registers ([#128](https://github.com/imsid/mashpy/issues/128)) ([04a2671](https://github.com/imsid/mashpy/commit/04a267145e66397f49cfcdd35b950b9fb352f758))


### Documentation

* cover synthetic evals in the root README ([#131](https://github.com/imsid/mashpy/issues/131)) ([730af54](https://github.com/imsid/mashpy/commit/730af5479ce2738f84d31fb6de5482b6b0134b81))
* cover the evals subsystem in the module READMEs ([#132](https://github.com/imsid/mashpy/issues/132)) ([e6290b2](https://github.com/imsid/mashpy/commit/e6290b21187736e7f0e42d083890080a79aa1e14))
* position evals across the blog and publish the synthetic evals post ([#125](https://github.com/imsid/mashpy/issues/125)) ([fe282b9](https://github.com/imsid/mashpy/commit/fe282b998cfa49795ae66a4a691c1219068e41c1))
* **review:** add synthetic evals design doc ([#121](https://github.com/imsid/mashpy/issues/121)) ([42e096b](https://github.com/imsid/mashpy/commit/42e096bb303ac66374da61d27905e87c8671a64b))

## [0.15.0](https://github.com/imsid/mashpy/compare/mashpy-v0.14.1...mashpy-v0.15.0) (2026-07-01)


### Features

* **admin-ui:** add copy-to-clipboard to message content, raw events, and trace/session IDs ([#120](https://github.com/imsid/mashpy/issues/120)) ([95456ff](https://github.com/imsid/mashpy/commit/95456ff8d5ef4a41e6ed248ad58bcdd5858aec9e))


### Bug Fixes

* **admin-ui:** truncate overflowing tool names and clamp long descriptions in tool cards ([#118](https://github.com/imsid/mashpy/issues/118)) ([dbd0237](https://github.com/imsid/mashpy/commit/dbd023720289337e3e61ea1fa4ac194ee101d54f))
* **api:** replace bare connection with AsyncConnectionPool in PostgresAPIEventStore ([#116](https://github.com/imsid/mashpy/issues/116)) ([f9dc28f](https://github.com/imsid/mashpy/commit/f9dc28f8a5fc3d49dfbe8faa182e68ab81d3a927))
* **ci:** match mashpy-v* tag pattern in docker-pilot and release-pilot ([#109](https://github.com/imsid/mashpy/issues/109)) ([6a6a0d0](https://github.com/imsid/mashpy/commit/6a6a0d03fcf9892fe61c5e8034a9c6ff6deba811))
* **ci:** pass explicit tag_name to gh-release on workflow_dispatch ([#111](https://github.com/imsid/mashpy/issues/111)) ([7ecad1a](https://github.com/imsid/mashpy/commit/7ecad1aedb36608282f3965e00d93639ae2c1f26))


### Documentation

* **index:** add Pilot quickstart as zero-code try-it-now path ([#112](https://github.com/imsid/mashpy/issues/112)) ([4f7e821](https://github.com/imsid/mashpy/commit/4f7e8218527519f1d445d18421633b26e2fc4ef6))
* **index:** clarify Pilot as a multi-agent host runtime ([#113](https://github.com/imsid/mashpy/issues/113)) ([00c4288](https://github.com/imsid/mashpy/commit/00c4288586dc4504c4bab44dfb8531deee33679d))
* **index:** rename "Start here" to "Learn More" ([#114](https://github.com/imsid/mashpy/issues/114)) ([584da66](https://github.com/imsid/mashpy/commit/584da6698a3d19a4640afcda4a77db9c53980a81))

## [0.14.1](https://github.com/imsid/mashpy/compare/mashpy-v0.14.0...mashpy-v0.14.1) (2026-06-30)


### Bug Fixes

* **pilot:** mock AdminCopilotSpec.build_llm in integration tests ([#107](https://github.com/imsid/mashpy/issues/107)) ([cdc38a2](https://github.com/imsid/mashpy/commit/cdc38a2f7b4288591bb1c2e82220728c0c846e8e))

## [0.14.0](https://github.com/imsid/mashpy/compare/mashpy-v0.13.0...mashpy-v0.14.0) (2026-06-29)


### Features

* **pilot:** merge mash-pilot into mashpy as src/pilot ([#105](https://github.com/imsid/mashpy/issues/105)) ([c615c00](https://github.com/imsid/mashpy/commit/c615c0094c561d3809b3e4cf848caa8d099e4f1f))


### Documentation

* add Moma and Pilot case study posts ([#103](https://github.com/imsid/mashpy/issues/103)) ([fbcafb9](https://github.com/imsid/mashpy/commit/fbcafb97282bfdcb1b58ee55fa683be5f8d94944))
* add Moma case study post ([#101](https://github.com/imsid/mashpy/issues/101)) ([9ef5f72](https://github.com/imsid/mashpy/commit/9ef5f723ea494db5ffdbca925a69380d3d804aec))


### Refactors

* **pilot:** slim spec.py to catalog loop, restore admin+quiz, update docs ([#106](https://github.com/imsid/mashpy/issues/106)) ([36f4250](https://github.com/imsid/mashpy/commit/36f42503fd1c542599a2110beb61f7c552f54451))

## [0.13.0](https://github.com/imsid/mashpy/compare/mashpy-v0.12.0...mashpy-v0.13.0) (2026-06-26)


### Features

* **llm:** full reasoning model support for OpenAI provider ([#95](https://github.com/imsid/mashpy/issues/95)) ([377d089](https://github.com/imsid/mashpy/commit/377d0899505cab346ce602a46344372515093639))


### Bug Fixes

* **llm:** resolve pylint, ruff, and pyright errors in base and openai providers ([#98](https://github.com/imsid/mashpy/issues/98)) ([3f7703a](https://github.com/imsid/mashpy/commit/3f7703a363cedf0c9897c72306f254e94dea3298))


### Documentation

* add core concepts page to Start Here ([#99](https://github.com/imsid/mashpy/issues/99)) ([4e00f85](https://github.com/imsid/mashpy/commit/4e00f85198f5a95c0711f18c3b3004e2c6a5ac5b))
* **concepts:** convert concept labels to subheaders ([#100](https://github.com/imsid/mashpy/issues/100)) ([f71d1ee](https://github.com/imsid/mashpy/commit/f71d1ee4fee5d1de607ff442a3fa3d1b4c625eb8))

## [0.12.0](https://github.com/imsid/mashpy/compare/mashpy-v0.11.0...mashpy-v0.12.0) (2026-06-24)


### Features

* **cli:** unify assistant_blocks rendering across REPL and workflow paths ([#93](https://github.com/imsid/mashpy/issues/93)) ([8ebe3f1](https://github.com/imsid/mashpy/commit/8ebe3f16f0cf27efa85a065adc50e2b29138633e))


### Bug Fixes

* **runtime:** scope finalize_structured_output to the final assistant turn ([#90](https://github.com/imsid/mashpy/issues/90)) ([e177ff0](https://github.com/imsid/mashpy/commit/e177ff0a2bea49c2c4352daf1dd3bda53556f1a7))

## [0.11.0](https://github.com/imsid/mashpy/compare/mashpy-v0.10.1...mashpy-v0.11.0) (2026-06-24)


### Features

* **api:** expose assistant_blocks through the reasoning trace endpoint ([#87](https://github.com/imsid/mashpy/issues/87)) ([8318331](https://github.com/imsid/mashpy/commit/83183315c505a7953012bdd2716af52a6794b849))
* **cli:** render response from assistant_blocks rather than response_payload.text ([#84](https://github.com/imsid/mashpy/issues/84)) ([7146b63](https://github.com/imsid/mashpy/commit/7146b63140ec75ca8455c9aca61f397b6e2410e0))
* **runtime,cli:** first-class structured output for workflow tasks ([d9b550b](https://github.com/imsid/mashpy/commit/d9b550b8cbbb3215d5ee1c05ee739ee84a84f2d0))

## [0.10.1](https://github.com/imsid/mashpy/compare/mashpy-v0.10.0...mashpy-v0.10.1) (2026-06-24)


### Bug Fixes

* **gemini:** raise retryable error on empty completed interaction; add 500 to server_error patterns ([#80](https://github.com/imsid/mashpy/issues/80)) ([0a1cca0](https://github.com/imsid/mashpy/commit/0a1cca0e6dde1e1accccc10df45fa28ce87e7c1f))

## [0.10.0](https://github.com/imsid/mashpy/compare/mashpy-v0.9.1...mashpy-v0.10.0) (2026-06-23)


### Features

* **admin-ui:** add Tools and Skills tabs with cards and detail views ([#76](https://github.com/imsid/mashpy/issues/76)) ([326099d](https://github.com/imsid/mashpy/commit/326099d1c03aa9979b96ac54e54a085bd304c974))


### Documentation

* **admin-ui:** add README and AGENTS guide for the web-admin SPA ([#78](https://github.com/imsid/mashpy/issues/78)) ([ed4e8c8](https://github.com/imsid/mashpy/commit/ed4e8c857cbeb092b273f86ec45031906d8733a1))

## [0.9.1](https://github.com/imsid/mashpy/compare/mashpy-v0.9.0...mashpy-v0.9.1) (2026-06-23)


### Bug Fixes

* **gemini:** fall back to real Interaction when streaming produces no text for grounded responses ([#72](https://github.com/imsid/mashpy/issues/72)) ([c954629](https://github.com/imsid/mashpy/commit/c9546298b961eea8d5ee9557ff640763ce36f7a4))

## [0.9.0](https://github.com/imsid/mashpy/compare/mashpy-v0.8.0...mashpy-v0.9.0) (2026-06-23)


### Features

* **gemini:** add web_search flag to enable native google_search without MCP ([#65](https://github.com/imsid/mashpy/issues/65)) ([c3a0654](https://github.com/imsid/mashpy/commit/c3a06545c3738c7453ceb0cc0d6e65e413cf92fb))


### Bug Fixes

* **cli:** surface empty agent responses and MCP/tool errors in the REPL ([#70](https://github.com/imsid/mashpy/issues/70)) ([4daed1e](https://github.com/imsid/mashpy/commit/4daed1e704a02bd7cd313e920236d2df6afe0b49))
* **mcp:** _normalize_url no longer appends /mcp to paths starting with mcp ([#68](https://github.com/imsid/mashpy/issues/68)) ([3b8517e](https://github.com/imsid/mashpy/commit/3b8517e8a5ae3d78103097e7708ded79a3be28fc))

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
