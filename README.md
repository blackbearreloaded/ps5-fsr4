# PS5 FSR4

> [!WARNING]
> **Experimental.** FSR4 runs natively on the PS5 and passes its acceptance
> against the WARP reference in every captured scenario, but this is not a
> release. The SDK is built locally from reference exports of the FSR4
> provider, and it needs a diagnostic profile of the Vulkan driver.

> [!IMPORTANT]
> **Primary FSR4 porting credit: the BC250 FSR4 project.**
> Our FSR4 effort builds on the pioneering work of **[dmoraza (dmorazasanchez)](https://github.com/dmorazasanchez/bc250-fsr4)**
> and **[daniel-h-0 and contributors](https://github.com/daniel-h-0/bc250-fsr4-fork)**,
> whose BC250 fork is our primary technical reference for FSR4 INT8 shader
> optimizations, provider tooling and reference workloads.
> **AMD / GPUOpen created FSR4 itself.** BlackBearReloaded's contribution here is
> the native PS5 adaptation, integration and validation.
> See [full credits and source provenance](LICENSING.md#fsr4-and-bc250-credits).

A working copy of [Manuel Pereira's ps5-vulkan](https://github.com/mpereiraesaa/ps5-vulkan),
being developed toward a reusable **FSR 4.1.1 INT8 SDK for native PS5 homebrew**.
The imported Vulkan code is copyright its original authors. This project is
maintained by BlackBearReloaded and is independent of the upstream project.

**Status:** Experimental.

- The `ps5_fsr4` runtime upscales 1280×720 to 1920×1080 on the PS5 in about
  3.75 ms per frame, on par with the same shaders on a BC250 (3.93 ms).
- Every captured scenario is accepted: static, motion, camera cut, SDR,
  dynamic resolution, sharpening, several output sizes and the 1080p target
  ([validation](VALIDATION.md#fsr4-acceptance)).
- An interactive demo renders a scene, upscales it and presents it at 60 fps.
- `tools/build_fsr4_sdk.py` stages the SDK: `libps5_fsr4.a`, its header,
  notices and a warmed pipeline cache ([FSR4 SDK](docs/FSR4_SDK.md)).

## Development

- [Build instructions](BUILDING.md)
- [FSR4 SDK](docs/FSR4_SDK.md)
- [Validation](VALIDATION.md)
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
- [Reference tools](docs/FSR4_REFERENCE_RUNTIME.md)
- [Vulkan API](API.md)

Keep test captures, logs and detailed results local. Commit code changes with
brief descriptions. This repository remains public.

## License

Project code is GPL-3.0-or-later. See [LICENSE](LICENSE) and
[LICENSING.md](LICENSING.md) for upstream credits and dependency terms.
