# FSR4 SDK

`ps5_fsr4` runs the FSR 4.1.1 INT8 upscaler natively on the PS5 through the
ps5vk Vulkan driver. Applications record it into their own command buffers.
It is experimental, but it is validated on hardware: every captured scenario up
to 2560×1440 passes the [acceptance rule](../VALIDATION.md#fsr4-acceptance) on
the PS5. At 4K, motion and Ultra Performance pass, and the static capture comes
within 0.16 dB of the rule.
At 1280×720 → 1920×1080 FSR4 takes about 2.7 ms per frame from submission to completion with
the default generated network kernels, and 3.26 ms with AMD's converted shaders alone, against
3.93 ms of GPU time for those shaders on a BC250 at 1850 MHz.

Outputs up to 3840×2160 are supported. The headless benchmark
(`tools/build_fsr4_bench.py`) submits frames back to back, so it reads higher
than the per-frame time above:

| Render → output | Mode | Benchmark |
| --- | --- | ---: |
| 1280×720 → 1920×1080 | Quality | 2.95 ms |
| 640×360 → 1920×1080 | Ultra Performance | 2.92 ms |
| 1706×960 → 2560×1440 | Quality | 4.95 ms |
| 1280×720 → 2560×1440 | Performance | 4.92 ms |
| 1920×1080 → 3840×2160 | Performance | 10.86 ms |
| 1280×720 → 3840×2160 | Ultra Performance | 10.94 ms |

## Contents

`tools/build_fsr4_sdk.py` adds these to the staged `dist-sdk`:

| Path | Purpose |
| --- | --- |
| `include/ps5fsr4/ps5_fsr4.h` | C API, also usable from C++ |
| `lib/libps5_fsr4.a` | Native runtime with the shaders and model data built in |
| `share/ps5fsr4/pipeline-cache-ps5.bin` | Optional warmed pipeline cache for the staged `libps5vk.a` |
| `share/ps5fsr4/identity.json` | Compiler version and archive digests the cache belongs to |
| `share/ps5fsr4/passes.json` | DXIL and SPIR-V digests of every embedded pass |
| `share/ps5fsr4/NOTICE.md`, `AMD-SDK-LICENSE.md` | FSR4 provenance and the AMD terms |

## Building

The FSR4 shaders and models come from the BC250 RC11 build of AMD's
`amd_fidelityfx_upscaler_dx12.dll`, placed at `build/reference-runtime/bc250-rc11/`;
`tools/fsr4_extract_dll.py` verifies every copy against its recorded digest.
Nothing from it is committed. The acceptance tests also need local reference
exports, created as described in the [reference procedure](FSR4_REFERENCE_RUNTIME.md).
The converter target needs the pinned dxil-spirv checkout (`DXIL_SPIRV_DIR`,
default `../references/dxil-spirv`). The pass-table generator also needs the
pinned DXC under `build/reference-runtime/dxc/`.

```sh
export PS5_PAYLOAD_SDK=/absolute/path/to/ps5-payload-sdk
make driver   # driver dependencies, PSBC and the driver SDK with the FSR4 profile
make sdk      # converter, pass tables and dist-sdk
```

`make driver-sdk` sets the four `PS5VK_*` switches of the driver profile FSR4 needs:

- INT8 and INT16 shader arithmetic;
- the subgroup vote;
- sampled storage images up to 3840×2160.

`build_fsr4_sdk.py` copies the driver SDK into `dist-sdk`, adds the FSR4 runtime,
then links a C and a C++ consumer against a relocated copy of the SDK, using
only its headers and archives.

A pipeline cache is only valid for the `libps5vk.a` build that saved it. Produce
one by running the runtime test on the PS5 without a cache: it saves
`fsr4-rt-pipeline-cache.bin` to its results directory. Then pass that file to
`build_fsr4_sdk.py --pipeline-cache`.

## Using it

1. Create a Vulkan 1.3 device with `shaderInt16` and
   `VK_KHR_storage_buffer_storage_class`. If the driver offers
   `subgroupSizeControl`, enable it and pass
   `PS5FSR4_FLAG_SUBGROUP_SIZE_CONTROL`. Five passes then run as wave64,
   saving about 0.1 ms with bit-identical output.
2. Create the inputs at render resolution. All images use
   `VK_IMAGE_LAYOUT_GENERAL` and optimal tiling, and are storage images with
   `SAMPLED` added. The images the demo uses:

   | Image | Format | Written by |
   | --- | --- | --- |
   | color | `R16G16B16A16_SFLOAT` | the renderer, linear |
   | depth | `R32_SFLOAT` | the renderer, device depth |
   | motion | `R32G32B32A32_SFLOAT` | the renderer; `.rg` holds current-to-previous motion |
   | output | `R32G32B32A32_SFLOAT` | FSR4, at output resolution |
3. Load the optional pipeline cache into a `VkPipelineCache`. Then call
   `ps5fsr4_context_create` with the maximum render size, the output size and
   the flags.

   The runtime holds AMD's standard model and its Ultra Performance model for
   3× upscaling, each in two resolution bands: one for outputs up to
   1920×1080 and one up to 3840×2160. The output size picks the band. As in
   FidelityFX, each frame's output-to-render width ratio
   selects the mode: at 2.99 or more the Ultra Performance model runs, and a
   change of mode discards history. Such a context creates the pipelines of
   both models, so a change of mode never compiles during a frame. When the
   render size changes from frame to frame, pass
   `PS5FSR4_FLAG_DYNAMIC_RESOLUTION`: the standard model then runs at every
   ratio and keeps history across size changes.

   FidelityFX's quality modes and their render sizes:

   | Mode | Ratio | 1920×1080 | 2560×1440 | 3840×2160 |
   | --- | ---: | --- | --- | --- |
   | Quality | 1.5 | 1280×720 | 1706×960 | 2560×1440 |
   | Balanced | 1.7 | 1129×635 | 1505×847 | 2258×1270 |
   | Performance | 2.0 | 960×540 | 1280×720 | 1920×1080 |
   | Ultra Performance | 3.0 | 640×360 | 853×480 | 1280×720 |
4. Each frame:
   1. Render with the offset from `ps5fsr4_jitter_offset`.
   2. Add a barrier from the render writes to compute reads.
   3. Call `ps5fsr4_dispatch`.
   4. Submit, and wait before the next dispatch on that context.

   Set `reset` on the first frame and on camera cuts. `motion_vector_scale`
   converts stored motion to render pixels. Set `enable_sharpening` and a
   `sharpness` in [0, 1] to run RCAS after reconstruction.

   ps5vk writes back and invalidates the CPU caches over every mapped
   `HOST_COHERENT` allocation around each submission. Unmap staging buffers
   once their upload is done, or use non-coherent memory with
   `vkFlushMappedMemoryRanges`: 41 MB of mapped coherent memory costs about
   1.3 ms per submission.
5. Save the cache with `vkGetPipelineCacheData` if it was created empty.
   Without a cache, the first context creation compiles the passes of both
   models for about 16 s. With the shipped cache it takes under 0.1 s.
6. Link in this order:
   1. `libps5_fsr4.a`;
   2. `libps5vk.a`;
   3. `libpsbc.a`;
   4. the payload SDK's C++ runtime and libc.

   `build_native_app` in `tools/build_fsr4_clear.py` has the complete command.

`examples/fsr4_demo_main.c` is a complete interactive consumer.
It renders a scene at 720p, upscales it, and shows FSR4, bilinear or a split
view on the display, switchable with the controller. Build it from the staged
SDK with `tools/build_fsr4_demo.py`. The [showcase app](../examples/fsr4_showcase/README.md)
goes further: it recreates the context for each output size and for dynamic
resolution, changes the quality mode per frame and enables sharpening.

## Limits

- Output up to 3840×2160. The render size may change per frame, up to the
  context maximum (see `PS5FSR4_FLAG_DYNAMIC_RESOLUTION` above).
- One dispatch in flight per context.
- No OpenGL backend. FSR4 runs on Vulkan compute.

## Licensing

The runtime is GPL-3.0-or-later. The embedded FSR4 material stays under AMD's
terms and credits the BC250 FSR4 project; see `share/ps5fsr4/NOTICE.md` and
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
