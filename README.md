# PS5 FSR4

[![PS5 FSR4 Showcase: FSR 4 beside a bilinear upscale of the same frame](docs/images/ps5-fsr4-showcase.png)](examples/fsr4_showcase/README.md)

*The [showcase app](examples/fsr4_showcase/README.md) on a PS5, upscaling
640×360 to 1920×1080 at 60 fps. Both lenses magnify the same spot of a
resolution chart four times: FSR 4 on the left, a bilinear upscale of the same
frame on the right.*

> [!WARNING]
> **Experimental.** FSR4 runs natively on the PS5 and passes its acceptance
> against the WARP reference in every captured scenario up to 2560×1440.
> [Pre-releases](https://github.com/blackbearreloaded/ps5-fsr4/releases) carry
> the showcase app, the SDK and their source. The SDK is built from the BC250
> RC11 build of AMD's FSR4 provider and needs a diagnostic profile of the Vulkan
> driver.

> [!IMPORTANT]
> **Primary FSR4 porting credit: the BC250 FSR4 project.**
> Our FSR4 effort builds on the pioneering work of **[dmoraza (dmorazasanchez)](https://github.com/dmorazasanchez/bc250-fsr4)**
> and **[daniel-h-0 and contributors](https://github.com/daniel-h-0/bc250-fsr4-fork)**,
> whose BC250 fork is our primary technical reference for FSR4 INT8 shader
> optimizations, provider tooling and reference workloads.
> **AMD / GPUOpen created FSR4 itself.** BlackBearReloaded's contribution here is
> the native PS5 adaptation, integration and validation.
> See [full credits and source provenance](THIRD_PARTY_NOTICES.md#fsr4-and-bc250-credits).

A reusable **FSR 4.1.1 INT8 SDK for native PS5 homebrew**, running on Vulkan
compute. The Vulkan driver is the [`external/ps5-vulkan`](external/ps5-vulkan)
submodule: BlackBearReloaded's fork of
[Manuel Pereira's ps5-vulkan](https://github.com/mpereiraesaa/ps5-vulkan).
This project is independent of the upstream driver project.

**Status:** Experimental.

- The `ps5_fsr4` runtime upscales 1280×720 to 1920×1080 on the PS5 in about
  2.7 ms per frame, and outputs up to 3840×2160 ([performance](#performance)).
  Generated INT8 kernels run the network, including the postpass's learned
  head, faster than AMD's converted shaders and produce the same bytes.
- Every captured scenario up to 2560×1440 is accepted: static, motion, camera
  cut, SDR, dynamic resolution, sharpening, several output sizes, the 1080p
  and 1440p targets and AMD's Ultra Performance model for 3× upscaling. At
  4K, motion and Ultra Performance pass too; in the static capture one frame
  of four misses the rule by 0.16 dB
  ([validation](VALIDATION.md#fsr4-acceptance)).
- An interactive demo renders a scene, upscales it and presents it at 60 fps.
- The [showcase app](examples/fsr4_showcase/README.md) (PPSA99011) draws a city
  at dusk and upscales it at 60 fps. Its settings choose every quality mode at
  1080p, 1440p and 4K output, with split views against bilinear and native
  rendering, a paired magnifier and a benchmark of the upscaler.
- GitHub Actions builds and tests everything from pinned public inputs for every
  pull request and, on `main`, when started by hand; a `v*` tag publishes a
  [pre-release](https://github.com/blackbearreloaded/ps5-fsr4/releases) with
  the showcase app, the SDK and their source ([releasing](docs/RELEASING.md)).
- `tools/build_fsr4_sdk.py` stages the SDK: `libps5_fsr4.a`, its header,
  notices and a warmed pipeline cache ([FSR4 SDK](docs/FSR4_SDK.md)).

## Performance

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/perf/cases-dark.svg">
  <img alt="Milliseconds per frame on the PS5 for each render and output size, against the 16.7 ms of one 60 fps frame" src="docs/perf/cases-light.svg">
</picture>

| Render → output | Mode | Time per frame | Share of a 60 fps frame |
| --- | --- | ---: | ---: |
| 640×360 → 1920×1080 | Ultra Performance | 2.92 ms | 18% |
| 1280×720 → 1920×1080 | Quality | 2.95 ms | 18% |
| 1280×720 → 2560×1440 | Performance | 4.92 ms | 30% |
| 1706×960 → 2560×1440 | Quality | 4.95 ms | 30% |
| 1920×1080 → 3840×2160 | Performance | 10.86 ms | 65% |
| 1280×720 → 3840×2160 | Ultra Performance | 10.94 ms | 66% |

These times come from the headless benchmark (`tools/build_fsr4_bench.py`).
It submits 1,800 frames back to back and times each from submission to
completion on the GPU. The interactive demo paces frames at 60 fps and
measures less: 2.66 ms for 1280×720 → 1920×1080. For that case AMD's
converted shaders alone take 3.26 ms on the PS5, and 3.93 ms of GPU time on a
BC250 at 1850 MHz.

The [showcase app](examples/fsr4_showcase/README.md) runs the same six cases on
any console: *Benchmark the six scenarios* in its settings times each one and
shows it beside this table.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/perf/history-dark.svg">
  <img alt="Milliseconds per frame for 1280×720 to 1920×1080 after each optimization step, from 27 ms to 2.67 ms" src="docs/perf/history-light.svg">
</picture>

`tools/build_fsr4_perf_charts.py` draws both charts from the measurements
recorded in it.

## Before and after

![Captured on a PS5: bilinear and FSR4 upscales of 960×540 to 1920×1080, native 1080p and a 64× supersampled reference, 3× zoom](comparisons/hero.png)

*Captured on a PS5: FSR4 upscales 960×540 to 1920×1080 in about 2.7 ms. From a quarter of the
pixels it comes closer to a 64× supersampled reference (37.5 dB) than native 1080p rendering
does (31.2 dB).*

![Captured on a PS5: bilinear and FSR4 upscales of 1280×720 to 3840×2160, native 4K and a 64× supersampled reference, 3× zoom](comparisons/3840x2160/hero.png)

*At 4K: FSR4's Ultra Performance model upscales 1280×720, a ninth of the pixels, to
3840×2160 in 10.9 ms (headless benchmark). It comes closer to the reference (38.6 dB) than
native 4K rendering does (34.0 dB).*

> [!TIP]
> **Every comparison is in [`comparisons/`](comparisons/README.md)**: three shots of the demo
> scene at [1080p](comparisons/README.md) (from 1280×720 and 960×540),
> [1440p](comparisons/2560x1440/README.md) (from 1706×960 and 1280×720) and
> [4K](comparisons/3840x2160/README.md) (from 1920×1080 and 1280×720), with lossless full-size
> frames, 1:1 crops, 3× zooms, PSNR/SSIM against the supersampled reference and, at 1080p, an
> orbiting motion clip, all captured on the console.

## Layout

| Path | Contents |
| --- | --- |
| `include/ps5fsr4/` | Public C API |
| `src/` | Runtime: context, dispatch, constants and pass tables |
| `tools/` | Shader converter, reference exports, runtime/SDK/app builders, acceptance |
| `tests/` | Host tests |
| `examples/` | Runtime test, interactive demo, showcase app, comparison capture and replay witnesses |
| `comparisons/` | Before/after captures of the demo scene at 1080p, 1440p and 4K, built by `tools/build_fsr4_comparisons.py` |
| `docs/` | SDK guide, reference procedure, research notes, performance charts |
| `external/ps5-vulkan/` | The Vulkan driver (submodule) |
| `.github/workflows/` | The GitHub Actions build: tests, packages and pre-releases ([releasing](docs/RELEASING.md)) |

Clone with `git clone --recurse-submodules`, or let `make` fetch the driver, then
follow [BUILDING.md](BUILDING.md).

## Development

- [Build instructions](BUILDING.md)
- [Releasing](docs/RELEASING.md)
- [FSR4 SDK](docs/FSR4_SDK.md)
- [Validation](VALIDATION.md)
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
- [Reference tools](docs/FSR4_REFERENCE_RUNTIME.md)
- [Vulkan driver API](external/ps5-vulkan/API.md)

Keep test captures, logs and detailed results local. Commit code changes with
brief descriptions. This repository remains public.

<!-- bbr-footer:start -->
<!-- Generated by ps5-homebrew-dev-protocol/scripts/readme-footer. Edit the template there, not here. -->

## Credits

Built with the [PS5 Payload SDK](https://github.com/ps5-payload-dev/sdk) by John Törnblom (ps5-payload-dev).
Third-party components, authors and licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

Copyright © 2026 BlackBearReloaded. Licensed under GPL-3.0-or-later; see [LICENSE](LICENSE). Third-party components keep their own licenses. Binary releases are built from the tagged source in this repository.

## Disclaimer

- **No affiliation.** This is an independent homebrew project. It is not
  affiliated with, endorsed by, or sponsored by Sony Interactive Entertainment.
  "PlayStation", "PS5" and related marks are trademarks of Sony Interactive
  Entertainment Inc. AMD, FidelityFX and FSR are trademarks of Advanced Micro Devices, Inc., which does not endorse this project.
- **No proprietary material.** No Sony SDK, firmware, encryption keys or
  decrypted system modules are included.
- **No warranty.** This project is provided "as is", without warranty of any
  kind, to the extent permitted by law. See sections 15 and 16 of the GPL.
- **Use at your own risk.** Running homebrew requires a modified console, which
  may void its warranty, breach the platform's terms of service, or cause data
  loss.
- **Legal use only.** Use it only with hardware, accounts and content you own.
  This project does not support or enable piracy.

## AI assistance

This project was developed with AI assistance from OpenAI and/or Anthropic tools.
<!-- bbr-footer:end -->
