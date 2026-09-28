# Building FSR4 for PS5

## Requirements

- Linux (WSL works) with Python 3.11+, CMake, Ninja, a C compiler and
  `spirv-link` from SPIRV-Tools (the pass tables link the generated postpass
  head into the converted postpass).
- The payload SDK and native app template the driver's SDK builder uses: set
  `PS5_PAYLOAD_SDK` and `PS5_NATIVE_APP_TEMPLATE` as described in
  [the driver's build notes](external/ps5-vulkan/BUILDING.md).
- The pinned dxil-spirv checkout (`DXIL_SPIRV_DIR`, default
  `../references/dxil-spirv`) and the pinned DXC under
  `build/reference-runtime/dxc/`.
- Local FSR4 reference exports under `build/reference-runtime/`, created as
  described in [the reference procedure](docs/FSR4_REFERENCE_RUNTIME.md). They
  are never committed.

## Build

```sh
make driver   # pinned driver dependencies, PSBC (PS5 and host), driver SDK with the FSR4 profile
make sdk      # converter, pass tables, then dist-sdk: the driver SDK plus libps5_fsr4.a
make demo     # PPSA88900 interactive demo against dist-sdk
make check    # host tests
```

`make` fetches the driver when `external/ps5-vulkan` is missing: the pinned
submodule in a git checkout, otherwise a clone of `DRIVER_URL` at that
revision (or its default branch when the revision is unknown).

`make driver-sdk` stages the driver SDK with the profile FSR4 needs:
`PS5VK_SHADER_INT8_DIAGNOSTIC`, `PS5VK_SHADER_INT16_DIAGNOSTIC`,
`PS5VK_SUBGROUP_ALL_DIAGNOSTIC` and `PS5VK_EXTENDED_COMPUTE_DIAGNOSTIC`. The FSR4
tools copy it into `dist-sdk` whenever it changes. The driver's own `make check`
restages its SDK without this profile, so `make sdk` restages it first; after
running the driver tests, run `make driver-sdk` before building FSR4 applications. To build against a driver
checkout elsewhere, set `PS5VK_ROOT` for the tools and `DRIVER` for make.

A pipeline cache is only valid for the `libps5vk.a` build that saved it; see
[the SDK guide](docs/FSR4_SDK.md).

The pass tables come from `tools/build_fsr4_runtime.py`. Its `--int8-kernels`
option takes the dispatch indices whose converted shaders are replaced by
packed-i16 kernels that `tools/fsr4_int8_kernels.py` generates from the local
model. The default, 3, 5, 9, 11, 21, 23 and 25 (network passes 1, 2, 4, 5 and
10-12), is the set the PS5 runs faster unrolled; 13, 15, 17 and 19 (passes 6-9)
and 26 (the postpass head, run in place of its border clear) are also
available. `--int8-loops` takes the dispatch indices generated in loop form by
`tools/fsr4_int8_loops.py`, whose loop bodies fit the instruction cache and
stream their weight pairs from a table after the model; its default is 13, 15,
17, 19 and 23. All of them produce byte-identical output and bake the model's
weights, so the generated sources stay in the build tree like the converted
shaders; empty values keep every converted shader. The postpass's own learned
head is replaced by the generated INT8 head as well: it is compiled as a
function and linked into the converted postpass (`--no-int8-postpass-head`
keeps the FP32 head). `--wave64` lists the dispatches compiled as wave64
(default 13-23, the odd ones: network passes 6-11); the tables header carries
the set to the runtime.

## Native FSR4 applications

These PPSA88900 applications use the prepared native template and payload SDK
and write their own result files:

- the runtime test (`tools/build_fsr4_runtime_test.py`) behind the
  [acceptance](VALIDATION.md#fsr4-acceptance);
- the interactive demo (`tools/build_fsr4_demo.py`), built from the staged
  [FSR4 SDK](docs/FSR4_SDK.md);
- the headless benchmark (`tools/build_fsr4_bench.py`), which times 720p to
  1080p upscaling from submission to fence and profiles each pass, without a
  display or a capture;
- the replay and precision witnesses below.

They need the driver SDK staged with the FSR4 profile (`make driver-sdk`).

The frame builder requires the original and corrected local reference exports
documented in [the reference procedure](docs/FSR4_REFERENCE_RUNTIME.md). The
precision witnesses require the pinned DXC and converter but no capture:

```sh
python3 tools/build_fsr4_precision_probe.py --rounding rtz --out build/fsr4-precision-rtz-app
python3 tools/build_fsr4_precision_probe.py --rounding rte --out build/fsr4-texture-rte-app
python3 tools/build_fsr4_frame.py --dispatch 29 --use-staged-sdk --out build/fsr4-image29-app
python3 tools/build_fsr4_frame.py --frames 4 --use-staged-sdk --out build/fsr4-temporal-app
python3 -m unittest tests.test_fsr4_precision_corpus tests.test_fsr4_frame_continuity tests.test_fsr4_image_compare
python3 tests/test_fsr4_converter_fma.py
```

The RTZ test checks scalar conversion; the RTE test checks typed texture loads,
including subnormals and ties. Constructing an app does not validate its
output; keep results local. Use only the console explicitly authorized in your
local configuration.
