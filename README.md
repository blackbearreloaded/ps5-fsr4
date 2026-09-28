# PS5 FSR4

> [!WARNING]
> **Experimental — work in progress. This project is not fully working yet.**
> Native PS5 tests can execute the FSR4 graph, but image correctness and
> temporal stability remain unresolved. The reusable SDK, graphical demo and
> target-resolution performance are not yet qualified.
> Current code are research progress, not a ready-to-use release.

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

**Status:** Experimental. Captured FSR4 graphs execute natively, but image
correctness and temporal stability remain unresolved. The reusable FSR SDK
and target-resolution performance are still under development.

## Development

- [Build instructions](BUILDING.md)
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
- [Reference tools](docs/FSR4_REFERENCE_RUNTIME.md)
- [Vulkan API](API.md)

Keep test captures, logs and detailed results local. Commit code changes with
brief descriptions. This repository remains public.

## License

Project code is GPL-3.0-or-later. See [LICENSE](LICENSE) and
[LICENSING.md](LICENSING.md) for upstream credits and dependency terms.
