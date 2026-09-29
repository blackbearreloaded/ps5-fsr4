# Validation

Run the focused host tests under tests/ and native diagnostics for the changed code.
Store captures, logs, hashes, timings and detailed comparisons locally under build/.
Do not publish test receipts or run diaries in this repository.

## FSR4 acceptance

**Reference.** The BC250 FSR4 provider runs on WARP. RenderDoc exports each
capture: every dispatch, its inputs and the WARP output
(see [the reference procedure](docs/FSR4_REFERENCE_RUNTIME.md)).

**Control.** The same runtime test runs on the host against lavapipe, with the
same SPIR-V and fixture (`build_fsr4_runtime_test.py --host`).

**Rule** (`tools/check_fsr4_acceptance.py`). A PS5 run is accepted when all of
these hold:

- the run completed;
- it produced no non-finite value;
- it replayed the same fixture as the control;
- on every frame, its PSNR against WARP is at least the control's PSNR minus 1 dB.

FSR4 quantizes activations to INT8, so last-bit floating-point differences
change quantized values. No conformant driver reproduces WARP bit-exactly, and
lavapipe diverges from WARP as much as the PS5 does. The rule therefore
requires the PS5 to be as close to the reference as an independent conformant
Vulkan implementation. The 1 dB margin was fixed before the scenario matrix was
run.

**Scenarios.** Captures of the provider probe (synthetic inputs; 4 frames unless noted):

| Scenario | Render → output | What varies |
| --- | --- | --- |
| static | 128×96 → 192×144 | nothing; steady history |
| motion | 128×96 → 192×144 | per-frame jitter, uniform motion vectors |
| reset8 | 128×96 → 192×144 | 8 frames, history reset at frames 0 and 4 (camera cut) |
| sdr | 128×96 → 192×144 | SDR input instead of HDR |
| resize | 128×96 → 192×144 | dynamic resolution: render size drops to 3/4 on frames 2–3 |
| rcas | 128×96 → 192×144 | RCAS sharpening at 0.4 |
| size240 | 160×96 → 240×144 | output size |
| size320 | 256×144 → 320×180 | output size |
| target | 1280×720 → 1920×1080 | static, at target resolution |
| target-motion | 1280×720 → 1920×1080 | per-frame jitter, uniform motion vectors |
| up | 640×360 → 1920×1080 | static, Ultra Performance (3×) model |
| up-motion | 640×360 → 1920×1080 | Ultra Performance, per-frame jitter, uniform motion vectors |
| k2 | 1280×720 → 2560×1440 | 2K output (the 3840×2160 shader band) |
| k2-motion | 1280×720 → 2560×1440 | 2K, per-frame jitter, uniform motion vectors |
| k2-quality | 1706×960 → 2560×1440 | 2K, Quality mode (1.5×) |
| k4-motion | 1920×1080 → 3840×2160 | 4K, per-frame jitter, uniform motion vectors |
| odd | 1278×718 → 1918×1078 | output width and height not multiples of 8 |

The probe's inputs are synthetic. Only the interactive demo exercises
disocclusion, and it has no numeric reference.

Run one scenario on each side, then check it:

    python3 tools/build_fsr4_runtime_test.py --capture build/reference-runtime/capture-export-motion \
        --scenario motion --out build/fsr4-rt-ps5-motion --pipeline-cache dist-sdk/share/ps5fsr4/pipeline-cache-ps5.bin
    python3 tools/build_fsr4_runtime_test.py --host --capture build/reference-runtime/capture-export-motion \
        --scenario motion --out build/fsr4-rt-host-motion
    python3 tools/check_fsr4_acceptance.py <ps5-results>/fsr4-rt-result.txt <host-results>/fsr4-rt-result.txt

Deploy the PS5 package to the locally configured console and collect its
`fsr4-rt-result.txt`. Run the host binary with
`VK_DRIVER_FILES=<lavapipe icd> FSR4_ASSET_DIR=assets FSR4_OUTPUT_DIR=out`.

**Status.** Every scenario above is accepted on the PS5. Rerun the matrix after
any change to the compiler, the driver or the runtime.

**Static 4K is not yet accepted.** 4K with motion passes. In the static
1920×1080 → 3840×2160 capture, frames 1–3 pass. Frame 0 misses the rule by
0.16 dB: the PS5 reaches 58.44 dB against WARP, lavapipe 59.60 dB. In 2K and
4K frames nearly all the remaining error, on the PS5 and on lavapipe alike,
lies on the one-pixel borders of the prepass's 16×16 tiles. There the prepass
reprojects history with bilinear-filtered taps, and Vulkan leaves the
filtering precision to the implementation. WARP's own reprojected history also
differs from its input mainly on those borders. Inside the tiles the PS5
matches WARP to a mean squared error below 10⁻⁸.