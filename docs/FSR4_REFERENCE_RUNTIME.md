# FSR4 reference and native diagnostics

Use the BC250 reference provider and pinned inputs enforced by the tools.
Run all shell operations in WSL. Diagnostics and captures belong under
ignored build directories and must not be committed.

## Reference

Prepare the Wine development headers/import libraries, FidelityFX API headers,
Agility runtime, WARP runtime and rebuilt BC250 provider under
build/reference-runtime/. See tools/fsr4_reference_probe.py for pinned inputs.

    python3 tools/fsr4_reference_probe.py --run
    python3 -m unittest tests.test_fsr4_reference_probe

The reference runner uses Windows interop from WSL. It does not replace system DLLs.
The exporter validates capture identity, workload and independent output.
See tools/fsr4_reference_export.py and its tests.

Replay restarts from the capture's first event for every exported dispatch,
so export time grows with the square of the capture's length. For 2K and 4K
captures, set `FSR4_EXPORT_DETAIL=frame0`. Frame 0 is then exported in full;
later frames export only what the acceptance tests read: the first two
dispatches' inputs and the output. A 4-frame 2560×1440 capture then exports in
about 15 minutes.

## Native replay

Set PS5_PAYLOAD_SDK and PS5_NATIVE_APP_TEMPLATE to local dependency paths.

    make fsr4-dxil-converter
    python3 tools/build_fsr4_frame.py --help

The builder supports captured graphs, selected dispatches and explicit or
compiler-default FP32 fusion. Both precision policies remain experimental.
Deploy the complete PPSA88900 folder via FTP to the locally configured,
authorized console. Keep all captures, readbacks and logs local.

Captured replay is a diagnostic; it does not supply the reusable SDK runtime,
presentation or performance qualification.
