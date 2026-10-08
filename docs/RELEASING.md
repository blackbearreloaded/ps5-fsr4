# Releasing

GitHub Actions builds everything from pinned public inputs
([`.github/workflows/build.yml`](../.github/workflows/build.yml)). Every pull
request and every `v*` tag builds the driver, the FSR4 runtime, the SDK, the
demo and the showcase, checks the pipeline cache, runs the host tests and
packages a release. A `v*` tag publishes that package as a GitHub pre-release.
A push to `main` builds nothing: a build on `main` is started by hand
(Actions → Build → Run workflow, or `gh workflow run build.yml --ref main`).
Every run keeps the showcase app ZIP as a workflow artifact; a pull request's is
named by its number and commit ([pull-request builds](PULL_REQUEST_BUILDS.md)).

## What a release contains

| Asset | Contents |
| --- | --- |
| `ps5-fsr4-showcase-VERSION-PPSA99011.zip` | The [showcase app](../examples/fsr4_showcase/README.md) folder, a README, `notices/` and `SHA256SUMS` |
| `ps5-fsr4-sdk-VERSION.zip` | `dist-sdk`: `libps5_fsr4.a` and its header, the driver SDK, the pipeline cache, the [SDK guide](FSR4_SDK.md), `notices/` and `SHA256SUMS` |
| `ps5-fsr4-VERSION-source.tar.gz` | Corresponding source of the GPL code: this repository without `comparisons/`, the Vulkan driver, ps5-agc-gears and the native app template at the built commits |
| `SHA256SUMS` | SHA-256 of the three assets |

`notices/` holds AMD's notice for the FSR4 material (MIT; see
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md#amd-fsr4-material)), the provenance of that
material from AMD's SDK through the BC250 build to this one, BC250's own
provenance note, and the licenses of everything else in the archive
(`THIRD-PARTY.md`). `tools/package_fsr4_release.py` writes all of it, and
`make release VERSION=01.000.000` builds the same assets locally after `make sdk`.

The two ZIPs and the source tarball built by the workflow can be checked with
`gh attestation verify <file> -R blackbearreloaded/ps5-fsr4` (GitHub CLI); this
covers releases built by GitHub Actions from now on, not earlier ones.

## Inputs

[`tools/build_inputs.json`](../tools/build_inputs.json) pins what the build
downloads; `make inputs` (`tools/fetch_build_inputs.py`) fetches and verifies it
into `build/`:

- the native app template at its commit, from which the payload SDK v0.42, the
  generated `libc.prx` and the packaging tool come;
- dxil-spirv at the converter's commit;
- DXC from Microsoft's release (the pass tables read DXIL with `dxc -dumpbin`);
- the BC250 RC11 upscaler DLL and its notices from BC250's release;
- the pipeline cache, from this repository's
  [`build-inputs`](https://github.com/blackbearreloaded/ps5-fsr4/releases/tag/build-inputs)
  pre-release.

`eval "$(python3 tools/fetch_build_inputs.py --env)"` then points the builds at
them. The driver pins its own dependencies (`make driver-deps`). The workflow
runs in an Ubuntu 26.04 container: the cache is only valid for SPIR-V that is
byte-identical to what it was made from, which needs the same glslang and
SPIRV-Tools releases.

## The pipeline cache

Only a PS5 can make it. `python3 tools/fetch_build_inputs.py --check-cache`, a
workflow step, fails when the pinned cache no longer fits this build: the
driver's cache identity changed (a compiler change bumps
`PS5VK_COMPILER_IDENTITY_VERSION`), or the pass tables changed. Without a
fitting cache, creating an FSR4 context on the console compiles its pipelines
for more than a minute instead of about 0.1 s. To make a new one:

1. Build the runtime test without a cache (`tools/build_fsr4_runtime_test.py`),
   run it on the PS5 and fetch the `fsr4-rt-pipeline-cache.bin` it saves.
2. `python3 tools/fetch_build_inputs.py --pin-cache fsr4-rt-pipeline-cache.bin`
   checks that the driver saved it, updates `tools/build_inputs.json` and prints
   the `gh release upload build-inputs ...` command for the renamed file.
3. Upload it, then commit the new pin. Earlier caches stay on the
   `build-inputs` release for the commits that pin them.

## Cutting a release

1. Validate the commit on a PS5: build the showcase's self-test
   (`make showcase SHOWCASE_ARGS=--selftest`), run it, and check that it ends
   with `FSR4_SHOWCASE_SELFTEST_DONE`, that every `FSR4_SHOWCASE_OUTPUT` line
   shows a `setup_ms` of about 0.1 s, which means the pipeline cache is used,
   and that the frames it saved look right.
2. Tag and push. Releases are numbered like PlayStation content versions,
   which is also what the console reports for the installed showcase:
   `git tag v01.000.000 && git push origin v01.000.000`.
3. The workflow builds, tests and packages the tag, then publishes the
   pre-release with the notes `tools/package_fsr4_release.py` writes.

To try the packaging without publishing, run the workflow by hand
(Actions → Build → Run workflow) with a version: the assets are kept as a
workflow artifact for a week.
