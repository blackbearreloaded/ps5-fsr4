# FSR4 SDK

`ps5_fsr4` runs the FSR 4.1.1 INT8 upscaler natively on the PS5 through the
ps5vk Vulkan driver. Applications record it into their own command buffers.
It is experimental, but it is validated on hardware: every captured scenario
passes the [acceptance rule](../VALIDATION.md#fsr4-acceptance) on the PS5.
At 1280×720 → 1920×1080 the 28 passes take about 3.75 ms per frame from submission to completion,
against 3.93 ms of GPU time for the same shaders on a BC250 at 1850 MHz.

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

The FSR4 shaders and model data come from local reference exports of the
BC250 FSR4 provider. Create them as described in the
[reference procedure](FSR4_REFERENCE_RUNTIME.md). They are never committed.
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
- sampled storage images up to 1920×1080.

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
4. Each frame:
   1. Render with the offset from `ps5fsr4_jitter_offset`.
   2. Add a barrier from the render writes to compute reads.
   3. Call `ps5fsr4_dispatch`.
   4. Submit, and wait before the next dispatch on that context.

   Set `reset` on the first frame and on camera cuts. `motion_vector_scale`
   converts stored motion to render pixels. Set `enable_sharpening` and a
   `sharpness` in [0, 1] to run RCAS after reconstruction.
5. Save the cache with `vkGetPipelineCacheData` if it was created empty.
   Without a cache, the first context creation compiles the passes for about
   77 s. With the shipped cache it takes about 0.1 s.
6. Link in this order:
   1. `libps5_fsr4.a`;
   2. `libps5vk.a`;
   3. `libpsbc.a`;
   4. the payload SDK's C++ runtime and libc.

   `build_native_app` in `tools/build_fsr4_clear.py` has the complete command.

`examples/fsr4_demo_main.c` is a complete interactive consumer.
It renders a scene at 720p, upscales it, and shows FSR4, bilinear or a split
view on the display, switchable with the controller. Build it from the staged
SDK with `tools/build_fsr4_demo.py`.

## Limits

- Output up to 1920×1080. The render size may change per frame, up to the
  context maximum.
- One dispatch in flight per context.
- No OpenGL backend. FSR4 runs on Vulkan compute.

## Licensing

The runtime is GPL-3.0-or-later. The embedded FSR4 material stays under AMD's
terms and credits the BC250 FSR4 project; see `share/ps5fsr4/NOTICE.md` and
[LICENSING.md](../LICENSING.md).
