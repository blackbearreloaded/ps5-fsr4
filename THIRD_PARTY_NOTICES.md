# Third-party notices

## FSR4 and BC250 credits

**The BC250 FSR4 project is the primary technical reference behind this PS5
FSR4 effort. Credit for that pioneering porting and optimization work belongs
to its original authors and contributors:**

- **[dmoraza / dmorazasanchez](https://github.com/dmorazasanchez/bc250-fsr4)**:
  originated the BC250 FSR4 project and its initial performance improvements,
  as acknowledged by the fork used here.
- **[daniel-h-0 and contributors](https://github.com/daniel-h-0/bc250-fsr4-fork)**:
  continued the BC250 work. This is the fork used directly as our reference
  for FSR4 INT8 shader optimizations, provider tooling and reference workloads.
  The audited revision is
  [528f13b17e48bfba5b153f17ec4ebdfb3afa5bcb](https://github.com/daniel-h-0/bc250-fsr4-fork/commit/528f13b17e48bfba5b153f17ec4ebdfb3afa5bcb);
  see the pinned dependency identities in the build tools.
- **AMD / GPUOpen**: original FSR4 technology, shaders and model assets.

BlackBearReloaded's work in this repository is the native PS5 adaptation,
integration and validation. It does not claim authorship of FSR4 or the BC250
optimizations. Upstream code, shaders, models and tools retain their respective
copyright and license notices; this repository's GPL grant does not relicense
those assets. Preserve their notices when adapting or distributing them.

## This repository

Copyright (C) 2026 BlackBearReloaded. The FSR4 runtime, tools, tests, examples
and documents in this repository are licensed under the GNU General Public
License, version 3 or (at your option) any later version (`GPL-3.0-or-later`).
The complete license text is in [`LICENSE`](LICENSE).

## The Vulkan driver

`external/ps5-vulkan` is a git submodule:
[BlackBearReloaded's fork](https://github.com/blackbearreloaded/ps5-vulkan) of
[Manuel Pereira's ps5-vulkan](https://github.com/mpereiraesaa/ps5-vulkan),
licensed `GPL-3.0-or-later`. Its own `LICENSING.md` covers its dependencies and
derived sources. Programs linked with the staged SDK (`libps5vk.a`,
`libpsbc.a`) are distributed under those terms as well.

## AMD FSR4 material

No AMD or BC250 shader, model or DLL is stored in this repository. The build
copies the shaders and models out of the BC250 RC11 build of AMD's
`amd_fidelityfx_upscaler_dx12.dll` into `build/fsr4-runtime/fsr4_passes.h`,
which `libps5_fsr4.a` embeds; the pipeline cache holds them compiled for the
PS5. AMD's notice, which the SDK stages as `share/ps5fsr4/AMD-SDK-LICENSE.md`,
lists that DLL (`Kits/FidelityFX/signedbin/amd_fidelityfx_upscaler_dx12.dll`)
under its MIT terms, so the material may be modified and redistributed with the
notice. The BC250 project publishes its modified DLL the same way.

## Release archives

The [pre-releases](https://github.com/blackbearreloaded/ps5-fsr4/releases)
combine GPL-3.0-or-later code (this repository, the Vulkan driver with
ps5-agc-gears, the native app template), the MIT FSR4 material and the MIT
shader compiler (opengnm-psbc). Every archive carries `notices/`: AMD's notice,
the provenance of the FSR4 material, BC250's provenance note and the other
licenses, listed in `THIRD-PARTY.md`. The corresponding source of the GPL code is
a separate asset of each release ([docs/RELEASING.md](docs/RELEASING.md)). Keep
the notices with any copy.

## Comparison images

The images and clips in [`comparisons/`](comparisons/README.md) are this
repository's own demo scene (`examples/fsr4_demo_scene.comp`), rendered and
upscaled on a PS5 by `examples/fsr4_compare_main.c`. They contain no AMD shader,
model or other FSR4 material and are distributed under the repository's
`GPL-3.0-or-later` terms.

## PS5 FSR1 research reference

[sainsaji/ps5-upscalar-research](https://github.com/sainsaji/ps5-upscalar-research)
at commit `0321573fc9f88c681be30ecfe6892e2344cec86e` informed the hardware
and capture checks in [docs/FSR1_REFERENCE.md](docs/FSR1_REFERENCE.md).
No code, shader or image from that repository is copied here. Its example/tool
code is GPL-3.0-or-later, and its FSR1 shader port carries AMD's MIT notice;
preserve both where a future direct adaptation applies.
