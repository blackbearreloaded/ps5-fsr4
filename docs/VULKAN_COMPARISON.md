# Vulkan foundation for PS5 FSR4

Source review: 2026-09-26. Recommendation: fork **mpereiraesaa/ps5-vulkan** as a separate driver dependency and keep **ps5-fsr4** as the reusable upscaler SDK. This is a source-based selection, conditional on the compiler and hardware gates below; neither driver has demonstrated FSR4 execution.

This review updates the initial OpenGL-first recommendation after the two Vulkan projects were supplied. OpenGL remains the requested example renderer. The Vulkan driver and OpenGL renderer do not currently have a proven shared-resource interface.

## Revisions inspected

| Project | Commit |
| --- | --- |
| ps5-fsr4 | `bd84c259ee4a4e2c3d9e5409306cfef6e7da4e75` |
| BC250 FSR4 fork, branch v4 | `528f13b17e48bfba5b153f17ec4ebdfb3afa5bcb` |
| ps5-opengl | `122aa899f9255e37d776b2a5317c86e5e9679907` |
| mihawk-99/PS5_Vulkan | `3a6f00df0c1cc35c6514290b6c4949769fce0b6a` |
| mpereiraesaa/ps5-vulkan | `10a76510a6b1e48061b5057f352cbbd6d78549fb` |
| AMD FidelityFX SDK 2.3.0 | `60f4ea81909200d8542eca14dccb2628b763a9a3` |

All five project repositories were cloned using WSL Git. Selected AMD documentation, headers and a complete, non-truncated Git tree listing were fetched through WSL. The review traced relevant compiler, resource, compute, synchronization, packaging and example paths; it did not run either Vulkan driver on a console.

## Why mpereiraesaa is the better FSR foundation

| Requirement | mihawk-99 | mpereiraesaa | Implication |
| --- | --- | --- | --- |
| Physical storage buffer addressing | Compiler entry rejects `PhysicalStorageBuffer64`; BDA is not exposed | Bounded buffer-device-address implementation, compiler opt-in and recorded native witnesses | Strongest reason to choose mpereiraesaa for the existing BC250 SPIR-V |
| 8/16-bit storage | No matching public narrow-storage feature set in the inspected device table | Public narrow storage, plus diagnostic INT8/INT16 arithmetic paths | Useful starting point, but storage support does not imply arithmetic support |
| Compute dependencies | Records compute packets and a partial flush; `CmdPipelineBarrier2` currently discards its arguments | Validates and records barriers; native path uses conservative completion/cache dependencies | Better basis for chained inference; both still need a dedicated FSR hazard test |
| Compute resources | Multi-set image/buffer path and a sampled-to-storage-image probe | Four compute sets, buffers, typed resources, separate sampler/image bindings, BDA | Both have useful infrastructure; neither supports the captured ABI in full |
| Storage images | Existing image descriptors and GPU test harness | Current published storage-image route is extremely narrow: R32_UINT with an 8x8 shape for its accepted usage | Significant work for mpereiraesaa before FSR pre/post passes |
| Shader waves and spills | Dispatch can program compiler-reported wave32 or wave64; FSR scratch handling is unproven | Dispatch requires wave32 and explicitly rejects scratch | mpereiraesaa needs wave/permutation selection and a spill feasibility gate |
| GPU timings | Native timestamp implementation and recorded clock probe | Timestamp queries/writes are refused | Reuse Mihawk's timestamp design as a separately validated reference |
| Application evidence | vkQuake, RetroArch and emulator workloads; public hardware records | Detailed compute/SDK witnesses and a bounded native DXVK workload | Mihawk currently has stronger evidence for running games; that does not establish FSR arithmetic support |
| Relation to ps5-opengl | Uses the OpenGL 0.3.0 PSBC lineage | Own public PSBC fork, currently pinned at `4fe7264a0cab9feb531a156e8a810b3e39cfe42c` | Mihawk is easier to align with the existing compiler; mpereiraesaa has useful newer compute features |
| SDK reuse | Statically linked driver plus substantial probe/game tooling | Explicit SDK staging and independent consumer | mpereiraesaa is closer to the intended deliverable |
| Public build dependencies | Public OpenGL adaptation and compiler patches | Native SDK and CI require logging files from a private lab repository | Remove this dependency before adopting the fork as a public SDK |
| OpenGL interoperability | No established external sharing contract | External memory/semaphore handle queries return no support | Neither gives us zero-copy GL/Vulkan interop automatically |

This comparison uses executable source where prose conflicts with it. For example, Mihawk's README contains both older Vulkan 1.0 statements and a newer 1.1 note; its current device macro is 1.1. mpereiraesaa reports 1.3 but leaves several promoted feature fields false. Version numbers are not the selection criterion.

Mihawk's active work targets Dolphin/game performance. That work list is context, not part of this FSR task.

## What the BC250 material actually requires

The reference has 348 LLVM/DXIL replacement sources: 180 preparation permutations, 72 inference permutations and 96 output permutations. Its RC9 capsule contains 42 original/optimized SPIR-V pairs: three variants each of a preparation pass, twelve model passes and an output pass.

The capsule and all 84 decompressed SPIR-V SHA-256 hashes were verified locally. The optimized programs declare:

| Capability | Optimized programs declaring it, out of 42 |
| --- | ---: |
| Int8 | 42 |
| RuntimeDescriptorArray | 42 |
| PhysicalStorageBufferAddresses | 42 |
| Int16 | 40 |
| GroupNonUniform / GroupNonUniformVote | 23 each |
| DotProduct / DotProductInput4x8BitPacked | 9 each |
| Float16 / StorageImageWriteWithoutFormat / DenormPreserve | 6 each |
| GroupNonUniformQuad / ComputeDerivativeGroupLinearKHR | 3 each |

All use physical-storage-buffer addressing with the GLSL450 memory model; descriptor sets span 1, 2 and 3. Capsule wave requirements are 26 wave32 entries, three wave64 entries and 13 without a fixed requirement. The largest optimized SPIR-V is 3,130,220 bytes.

These are module declarations and capsule selection requirements, not proof that every declared operation remains live after specialization. The first compiler gate must determine that. In particular, source inspection found actual signed-dot, subgroup vote and quad operations; simply deleting their capability declarations is not a valid port.

This is why BDA matters, but also why BDA alone is insufficient. The capture includes vkd3d descriptor conventions. A native Vulkan SDK still needs a correct binding map, constants, weights, resource layouts and dispatch schedule. FSR's model itself does not inherently require the whole vkd3d descriptor system.

Machine-readable results: [source audit](SOURCE_AUDIT.json).

## Work needed in the recommended fork

1. **Make the native build public and reproducible.** Accept explicit paths to the public payload SDK, native template and public graphics support. Replace the private logging client with a small public logging boundary or include it only if its source becomes publicly available under suitable terms. Native build requests must fail if they would otherwise produce only host/mock archives.
2. **Choose a bounded shader ABI.** Attempt the captured modules against the pinned compiler, then determine whether fixed descriptor arrays and direct bindings can replace translator-generated runtime arrays. Implement general descriptor indexing only if the selected shaders actually need it.
3. **Qualify the necessary arithmetic.** INT8/INT16 arithmetic, signed packed dot lowering, rounding/saturation, float16 behavior, subgroup votes/quad operations and wave requirements need explicit compiler and GPU tests. Do not promote diagnostic features just by changing reported bits.
4. **Extend image support.** Implement the exact sampled and storage formats, dimensions, usages and barriers required by the selected FSR graph. The existing 8x8 integer store witness is insufficient for floating-point full-frame output.
5. **Handle shader pressure.** Compile the largest selected inference pass early. Either retain a correct non-spilling variant or implement scratch allocation, descriptors, sizing and retirement. A generic successful compute dispatch does not exercise this problem.
6. **Verify multi-pass ordering.** Test compute-write to compute-read, transfer-clear to compute-read/write, compute-write to sampled-read, and in-place tensor hazards. Preserve padding clears and initialization.
7. **Resolve the OpenGL bridge separately.** First prove that the two static stacks can coexist, including PSBC/Mesa symbol and ABI compatibility. Then prove memory ownership, layout and synchronization. A pointer cast between GL and Vulkan resources is not an interface.

Mihawk remains valuable as a reference for native timestamps, image descriptors, GPU barriers and presentation. Port individual mechanisms with attribution and tests where they reduce work; combining both complete drivers would create a second integration project.

## Decision gates

Adopt mpereiraesaa provisionally, then require:

- A clean public-source native build and a tiny existing compute test on the target PS5.
- Successful compilation and correct GPU readback for one real preparation pass, one demanding inference pass and one output pass.
- A bounded, fully synchronized two-pass dependency test.
- An explicit decision on compiler coexistence and GL/Vulkan resources before promising an interactive OpenGL example.

If these fail because of a fundamental compiler limitation, reassess using the actual minimized failure and the same shader on Mihawk's compiler. Do not commit to either fork merely because of an advertised API version.

## Source anchors

- [BC250 shader inputs and implementation](https://github.com/daniel-h-0/bc250-fsr4-fork/blob/528f13b17e48bfba5b153f17ec4ebdfb3afa5bcb/dll/README.md), [capsule manifest](https://github.com/daniel-h-0/bc250-fsr4-fork/blob/528f13b17e48bfba5b153f17ec4ebdfb3afa5bcb/v4/experimental/rc9-port/manifest.json).
- [Mihawk capability refusal](https://github.com/mihawk-99/PS5_Vulkan/blob/3a6f00df0c1cc35c6514290b6c4949769fce0b6a/driver/ps5vk_pipeline.c#L663), [barrier implementation](https://github.com/mihawk-99/PS5_Vulkan/blob/3a6f00df0c1cc35c6514290b6c4949769fce0b6a/driver/ps5vk_draw.c#L1310), [compute implementation](https://github.com/mihawk-99/PS5_Vulkan/blob/3a6f00df0c1cc35c6514290b6c4949769fce0b6a/driver/ps5vk_compute.c), [device features](https://github.com/mihawk-99/PS5_Vulkan/blob/3a6f00df0c1cc35c6514290b6c4949769fce0b6a/driver/ps5vk_physical_device.c).
- [mpereiraesaa compute compiler adapter](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/src/ps5vk_compiler.c), [native feature gates](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/native/platform_ps5.c#L409), [wave32 requirement](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/src/dispatch_encode.c#L15).
- [mpereiraesaa storage-image bounds](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/src/graphics_formats.h#L182), [format roles](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/src/texture_format.c), [barrier recording](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/src/vk_command.c#L1728).
- [mpereiraesaa SDK dependencies](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/tools/build_sdk.py#L93), [private CI dependency](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/.github/workflows/host-contracts.yml#L29), [compiler pin](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/tools/prepare_compiler_deps.py).
- [mpereiraesaa public API boundaries](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/API.md), [hardware evidence](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/VALIDATION.md), [external handle queries](https://github.com/mpereiraesaa/ps5-vulkan/blob/10a76510a6b1e48061b5057f352cbbd6d78549fb/src/vk_device.c#L1947).
