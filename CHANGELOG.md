# Changelog

## [0.3.4](https://github.com/gbasin/pandora/compare/v0.3.3...v0.3.4) (2026-09-26)


### Features

* {argN} templating in declared output paths ([#185](https://github.com/gbasin/pandora/issues/185)) ([007899b](https://github.com/gbasin/pandora/commit/007899ba70f04802671a274d25d92da9bc480723))

## [0.3.3](https://github.com/gbasin/pandora/compare/v0.3.2...v0.3.3) (2026-09-26)


### Bug Fixes

* source-manifest integrity, hint-rule rollup, per-run Perfetto trace ([#183](https://github.com/gbasin/pandora/issues/183)) ([d706c95](https://github.com/gbasin/pandora/commit/d706c95a828fd163abea5418b625e6a50df53393))


### Documentation

* install lands a release, and the fallback table names engine-version ([#171](https://github.com/gbasin/pandora/issues/171)) ([69927a1](https://github.com/gbasin/pandora/commit/69927a19a8cb4f14c2eb1044b2874d6498ac4a46))
* scrub consumer names and host details from the public tree ([#174](https://github.com/gbasin/pandora/issues/174)) ([3cf0747](https://github.com/gbasin/pandora/commit/3cf074794c6d6f3f1d461d9c5007f19d4102d743))
* the gateway e2e installs the block directly, not via provision ([#169](https://github.com/gbasin/pandora/issues/169)) ([0c69eae](https://github.com/gbasin/pandora/commit/0c69eae94badd87d49c550681d7a240f4ed6bbe7))

## [0.3.2](https://github.com/gbasin/pandora/compare/v0.3.1...v0.3.2) (2026-09-26)


### Bug Fixes

* the release upload runs where gh is not installed ([#166](https://github.com/gbasin/pandora/issues/166)) ([20d2dbd](https://github.com/gbasin/pandora/commit/20d2dbdbb4627e0fd75672f49e8ae91403d2647a))

## [0.3.1](https://github.com/gbasin/pandora/compare/v0.3.0...v0.3.1) (2026-09-26)


### Features

* add Mac evaluation resource sampler ([#50](https://github.com/gbasin/pandora/issues/50)) ([a217cc5](https://github.com/gbasin/pandora/commit/a217cc5b3a0d90f806dd0b8726e78cedf1120067))
* add surface parent dispatch adapter ([4cb10ed](https://github.com/gbasin/pandora/commit/4cb10ed30e7127939bc143a25f7b531b01e06999))
* admit remote validation in durable FIFO order ([44a6f4f](https://github.com/gbasin/pandora/commit/44a6f4f32e8ed31ce772961c7648b13aee5b4f0b))
* bound artifact delivery with same-attempt recovery ([#47](https://github.com/gbasin/pandora/issues/47)) ([b7791cc](https://github.com/gbasin/pandora/commit/b7791cc7eac28f4e6c568eb14ab3b33eca0bfa50))
* define surface shard plans ([a2367c8](https://github.com/gbasin/pandora/commit/a2367c850925670c59aac588214311d0e6a10e99))
* fetch and install a published release; a bare `pandora upgrade` means the latest ([#162](https://github.com/gbasin/pandora/issues/162)) ([412da0c](https://github.com/gbasin/pandora/commit/412da0c219ec9a84421ea38e59676c207f51bf80))
* integrate focused journey expectation return and retry ([3e930da](https://github.com/gbasin/pandora/commit/3e930da79a6d00ca441db39e1e15cc0413a94984))
* integrate surface shards with worker receipts and output return ([6f81e21](https://github.com/gbasin/pandora/commit/6f81e21931d885b228a9a5ff53fc36617c951032))
* reserve queued images and collect acknowledged unused build tags ([20fa022](https://github.com/gbasin/pandora/commit/20fa022fdd9381eddfecdd1d03ad09d980f8734c))
* resolve expectation conflicts with durable local acceptance ([#46](https://github.com/gbasin/pandora/issues/46)) ([c50e15d](https://github.com/gbasin/pandora/commit/c50e15d0cb7bc3479340af432d645bddd6b618e1))
* return focused journey update proposals ([e275330](https://github.com/gbasin/pandora/commit/e2753306bbdd79b93d46bc5f1369b0616c578441))
* route an isolated service-backed journey through SSH ([5b82b45](https://github.com/gbasin/pandora/commit/5b82b4547c1c1b18c635257e69cf778955bd6cdc))
* route scoped Docker build and run workflows over SSH ([4b01422](https://github.com/gbasin/pandora/commit/4b0142253a1be1989b5daca71a638393d2ba491b))
* route surface validation through planner ([3fa7753](https://github.com/gbasin/pandora/commit/3fa7753dac36619b825c91bf4f777f91c1ce193b))
* teammate keys on a shared worker get a gateway, not a shell ([#161](https://github.com/gbasin/pandora/issues/161)) ([5c8a10a](https://github.com/gbasin/pandora/commit/5c8a10a9d5c61a109aa38252c510f7905a192dc9))


### Bug Fixes

* abandon never registered worker request ([6381a3a](https://github.com/gbasin/pandora/commit/6381a3a18ec446fc130bc494acfc2fac05ed8866))
* abandon never registered worker request ([#60](https://github.com/gbasin/pandora/issues/60)) ([5268be5](https://github.com/gbasin/pandora/commit/5268be5dbd7444d064d7adc818008ac82644a944))
* assemble generated surface outputs for delivery ([fda1b08](https://github.com/gbasin/pandora/commit/fda1b089540e7d47f3b47c5c4cf902eb38cd0039))
* authenticate surface aggregates and preserve stopped admission ([d8f4ce2](https://github.com/gbasin/pandora/commit/d8f4ce21fc11afa18c92d7ae22023c084145fbf7))
* bind surface parent invocation evidence ([9b28c9a](https://github.com/gbasin/pandora/commit/9b28c9abb866d0d122d74bdb2fe9dbe2600649bd))
* clarify Docker image inputs and automatic outputs ([70e9964](https://github.com/gbasin/pandora/commit/70e99640a9b8a0f25964db3ceb33a4b96b628ab9))
* clean surface containers after worker death ([#52](https://github.com/gbasin/pandora/issues/52)) ([72826e0](https://github.com/gbasin/pandora/commit/72826e0636eacfd8324d5a1a4e2333719e263eea))
* configure pandora queue deadlines ([c331123](https://github.com/gbasin/pandora/commit/c331123d9642e7fc66e84b7f8cec07627be915aa))
* distinguish admitted resource owners from abandoned work ([#43](https://github.com/gbasin/pandora/issues/43)) ([c7bd5bd](https://github.com/gbasin/pandora/commit/c7bd5bdadf55e8b4add0da14dda025a447eb47aa))
* fail closed on malformed admission receipts ([e779a84](https://github.com/gbasin/pandora/commit/e779a84760d4a28470be90094f1d947180f114fa))
* harden surface shard evidence ([e38dc32](https://github.com/gbasin/pandora/commit/e38dc3232800181c0e10791dae34428841a43ff6))
* isolate source reuse by repository and protect transfer seeds ([c7f8e4b](https://github.com/gbasin/pandora/commit/c7f8e4ba5a973e3e935f2830274f2fb80587cb6d))
* make surface evidence canonical across runtimes ([9392ddf](https://github.com/gbasin/pandora/commit/9392ddf29adc1586ed8ff5f16819f4251846efad))
* own dependency builders through recoverable cleanup ([#39](https://github.com/gbasin/pandora/issues/39)) ([4ea643b](https://github.com/gbasin/pandora/commit/4ea643b3e62d6a68bf1ab57350727857dddcb98b))
* own Docker builders through verified cleanup ([#40](https://github.com/gbasin/pandora/issues/40)) ([bcf766c](https://github.com/gbasin/pandora/commit/bcf766ca038a44c5b5c4a0070972eed783aad5f9))
* pin dependency images through verified worker cleanup ([#38](https://github.com/gbasin/pandora/issues/38)) ([0dc754b](https://github.com/gbasin/pandora/commit/0dc754bb18ce8611f4323af89b0d79192328a360))
* preserve keep-going grep patterns ([24031db](https://github.com/gbasin/pandora/commit/24031dbc63690cedb0f546233778326bcb3b1f2a))
* preserve routing limits in Codex shells ([#53](https://github.com/gbasin/pandora/issues/53)) ([66b5c09](https://github.com/gbasin/pandora/commit/66b5c090fc64b1b2347ceb26841e90e7b35a0401))
* remove only inspected attempt-owned Docker resources ([#48](https://github.com/gbasin/pandora/issues/48)) ([903d2d3](https://github.com/gbasin/pandora/commit/903d2d3767d3a0b497ac17545491473c67a5548a))
* retain surface screenshots without changing compiled input ([0eaa524](https://github.com/gbasin/pandora/commit/0eaa52462b0a12a3d70b4a23c1d58ec3d7127896))
* verify stopped container ownership before retention ([#45](https://github.com/gbasin/pandora/issues/45)) ([1ed79b3](https://github.com/gbasin/pandora/commit/1ed79b3989911ca16da55eaa807fcc1256ade37e))


### Performance Improvements

* cache verified worker helpers and reuse SSH connections ([c5382ee](https://github.com/gbasin/pandora/commit/c5382eed758054808cd465495b87da92b1621dbc))


### Documentation

* --help states the installed-but-silent daemon rule ([#107](https://github.com/gbasin/pandora/issues/107)) ([50fc57d](https://github.com/gbasin/pandora/commit/50fc57d7d7967da9d831043508f5d4f2742d8da2))
* clarify image reservation timing ([1d4a30f](https://github.com/gbasin/pandora/commit/1d4a30f93da4be87c5924e08b593b42f5ac831bc))
* compare native BuildKit and Mutagen against the remote workflow contract ([8978b38](https://github.com/gbasin/pandora/commit/8978b38ffad29c905f4eddbf9085657e52831aa5))
* normalize copied build log whitespace ([1339590](https://github.com/gbasin/pandora/commit/13395903e5a9ab226dca207c25e0885928522854))
* normalize whitespace in recorded build logs ([49c4c4d](https://github.com/gbasin/pandora/commit/49c4c4d16358a80928077794974d07dcdd1dc416))
* reconcile v0.1 command contract ([68921bd](https://github.com/gbasin/pandora/commit/68921bd96f39b90f1ce05a1717d299d2c3dfc9c4))
* reconcile v0.1 command contract ([8191777](https://github.com/gbasin/pandora/commit/8191777f1ff10946783f97dba6c89d3272df35ee))
* record background-work ideas for agent software factories ([#44](https://github.com/gbasin/pandora/issues/44)) ([370b7d8](https://github.com/gbasin/pandora/commit/370b7d8f628797a862ecdd26490a4a46f1eba35b))
* record integrated journey update behavior and VM evidence ([3e9e736](https://github.com/gbasin/pandora/commit/3e9e736a9e620a533226ab920fd70c79d7ed3f62))
* record journey update and sharding stress test ([a5f0a8e](https://github.com/gbasin/pandora/commit/a5f0a8e0f8ba1688c03563a0ea0797ade7f3f182))
* record manifest transfer latency and admission race ([b9e18e4](https://github.com/gbasin/pandora/commit/b9e18e4b251c0387beadbb100e77283a131a5734))
* record multi-tenant direction and utilization reasoning ([#41](https://github.com/gbasin/pandora/issues/41)) ([60261ec](https://github.com/gbasin/pandora/commit/60261ec3e56b38b1205d1cb896842ccbec8ef018))
* require twelve-session readiness evidence ([30258b9](https://github.com/gbasin/pandora/commit/30258b98fc82dfc45c61815a5073325f87ea6b6c))
* restore v0.1 Docker scope details ([215d283](https://github.com/gbasin/pandora/commit/215d28317f7e49a0812b8a45833171c765470fa5))
* scope heavy tests, tracked updates and shard execution ([924da37](https://github.com/gbasin/pandora/commit/924da37be7266474196ec6e5c19db2a8fdaf0863))
* specify v0.1 remote workflow contract and evaluation ([a301baa](https://github.com/gbasin/pandora/commit/a301baa8105890487f0d9bb3f9aa1f1593e99321))

## v0.2 (2026-09)

The machine-wide `pnpm` shim, the per-user daemon, per-repository
`pandora.toml` and enrollment, snapshot installs under
`~/.local/share/pandora/versions` with `current` flipped after a drain,
a local lane with one memory budget, remote runs in fresh Incus instances
cloned from a golden image, sharded fan-out, and two-phase write-back for
`--update`. Exit codes 64, 70, 75, 124 and 130 are the caller contract.
The v0.1.1 Docker profile is not carried over.

## v0.1.1 (2026-09)

Per-session launcher routing (`experiments/routing/launch.py`) with a
Docker execution profile. Twelve-agent baseline in
`notes/v0.1.1-validation-2026-09-21.md`.

## v0.1 (2026-09)

First contract: a scheduler that claims a repository's heavy commands and
runs them on a shared worker. `notes/v0.1-contract.md`.
