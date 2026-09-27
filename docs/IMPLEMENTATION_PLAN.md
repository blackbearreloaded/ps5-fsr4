# PS5 FSR4 implementation plan

Build a native FSR 4.1.1 INT8 model-2 SDK using the imported ps5-vulkan driver.
Use ps5-opengl for the graphical example and the native app boilerplate for
title PPSA88900. This remains experimental and incomplete.

## Initial scope

- Base PS5; fixed 1280x720 input and 1920x1080 output; SDR first.
- One GL context, Vulkan device/queue, view, history context and frame in flight.
- Linear scene color before UI/display encoding, depth, camera/object motion,
  jitter, exposure, frame time and explicit history reset.
- Real FSR4 preparation, inference and reconstruction; no silent FSR2/3 fallback.
- Frame generation, HDR, dynamic resolution and engine plugins are outside the initial scope.

## SDK contract

Use a C ABI with versioned plain structures and explicit error codes.
Provide context creation/destruction, dispatch recording, jitter generation
and a memory-requirements query. Expose Vulkan handles directly.

The application owns device, queue, command buffer, inputs, output and presentation.
The SDK owns pipelines, model data, constants, intermediates and history.
Dispatch records commands; the application submits and waits. Context reuse
requires completion of its previous dispatch.

Specify color formats/encoding, depth range and direction, camera parameters,
motion units/direction, jitter convention, positive exposure, masks/defaults,
frame timing/reset and incoming/outgoing resource layouts. Reject unsupported
models, dimensions, formats and resource combinations.

## Implementation order

1. Maintain a reproducible public WSL build and pinned dependencies with notices.
2. Recover the complete graph, shader permutations, model data, constants,
   resource layouts and initialization/reset behavior from the reference.
3. Correct compiler arithmetic, image access, subgroups, scratch/spills,
   allocation and multi-pass barriers using focused regression checks.
4. Implement persistent FSR contexts and pipelines, complete pass scheduling,
   resource lifetime, history ping-pong, reset and safe destruction.
5. Validate first-frame, steady history, motion, disocclusion and camera cuts
   against a repeatable reference. Establish numerical criteria independently
   of failures; do not relax thresholds simply to pass.
6. Build the native OpenGL example with offscreen color/depth/motion, previous
   transforms, jitter, explicit GL/Vulkan resource handoff and one presenter.
   Check allocation ownership, format/pitch/tiling, bounds, completion and state leakage.
   A staging bridge may aid debugging but does not qualify real-time sharing.
7. Qualify the full temporal workload at the target resolution. Measure warmed
   GPU timings, memory, synchronization and transfer costs before optimization.
8. Package libps5_fsr4.a, public headers, model/shader assets, build instructions,
   licenses and native examples. Verify a fresh build, relocated installation
   and independent C/C++ consumers.

Completion requires correct temporal FSR4, an interactive OpenGL native example,
clean teardown, target-resolution validation and a usable SDK package.
A captured replay, shader compilation or screenshot alone is insufficient.

## Workflow

Use WSL for all shell, Git and build operations. Deploy the complete native
folder via FTP port 2121 to /data/homebrew/PPSA88900/ using the authorized
console configured locally. Never select another console automatically.

Keep the repository public. Push meaningful code changes, not test results or
research diaries. Keep diagnostics and detailed evidence in ignored local
directories. Use brief progress updates. Never commit personal names, machine
paths or connection addresses.

## Credits

The primary FSR4 porting reference is the BC250 work by dmoraza and daniel-h-0.
AMD/GPUOpen created FSR4. Preserve all original licenses and credits; see
[LICENSING.md](../LICENSING.md).
