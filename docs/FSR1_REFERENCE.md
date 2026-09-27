# PS5 FSR1 research reference

Reviewed [sainsaji/ps5-upscalar-research](https://github.com/sainsaji/ps5-upscalar-research)
at commit [0321573fc9f88c681be30ecfe6892e2344cec86e](https://github.com/sainsaji/ps5-upscalar-research/tree/0321573fc9f88c681be30ecfe6892e2344cec86e).
Its EVO Player integration runs FSR1 EASU and RCAS and small Anime4K networks
as offline-compiled **fragment** passes through native sceAgc. Its captures
and submit-to-fence timing are from a **PS5 Pro on firmware 12.70**, not a base
PS5 or this Vulkan driver. The published measurements do not predict FSR4
performance.

| Finding | Use in this project |
| --- | --- |
| [Shader toolchain](https://github.com/sainsaji/ps5-upscalar-research/blob/0321573fc9f88c681be30ecfe6892e2344cec86e/docs/04-shader-toolchain.md) derives user-SGPR slots and registers from PAL metadata | Continue checking our PSBC metadata and Vulkan descriptor ABI. A successful compiler exit is not enough. |
| [First hardware failure](https://github.com/sainsaji/ps5-upscalar-research/blob/0321573fc9f88c681be30ecfe6892e2344cec86e/docs/06-lessons-from-hardware.md) was sampling a tiled color target through a linear texture descriptor | The OpenGL/Vulkan bridge must carry exact tiling, pitch, alignment and format. Compare the target register and sampled descriptor; test asymmetric pixels across tile boundaries. The current Vulkan sampled-image path refuses general color-attachment sampling, so the FSR bridge needs a separately qualified route. |
| [Dependent passes](https://github.com/sainsaji/ps5-upscalar-research/blob/0321573fc9f88c681be30ecfe6892e2344cec86e/docs/02-pipeline.md) needed a color-block flush and texture-cache invalidation before reading a previously written scratch surface | Check transfer→compute, compute→compute and compute→sample hazards on real PS5 hardware, especially when an intermediate is reused. Translate this requirement through Vulkan barriers and the native encoder; do not copy the AGC bit pattern blindly. |
| [Capture workflow](https://github.com/sainsaji/ps5-upscalar-research/blob/0321573fc9f88c681be30ecfe6892e2344cec86e/docs/07-reproducing.md) held the same video frame, forced redraw of both scanout buffers, logged the mode actually used, then captured losslessly | Give our demo deterministic scene times, mode telemetry and matched captures. FSR4 additionally needs the same preceding frame sequence and an explicit history reset before each comparison. A frozen single frame alone cannot test a temporal model. |
| [Timing](https://github.com/sainsaji/ps5-upscalar-research/blob/0321573fc9f88c681be30ecfe6892e2344cec86e/docs/01-results.md#gpu-cost) used submit-to-fence retirement; sub-millisecond timings were limited by polling grain | Label any similar measure whole-frame/queue-retirement time, include transfer and presentation paths where applicable, and keep it separate from per-pass GPU timing. |

FSR1's [EASU/RCAS generator and CPU reference](https://github.com/sainsaji/ps5-upscalar-research/tree/0321573fc9f88c681be30ecfe6892e2344cec86e/examples)
may serve as an **optional visual baseline** once the demo can render and capture
reliably. FSR1 is spatial and has no FSR4 model weights, temporal history or
provider dispatch graph. Its AGC fragment pipelines cannot be linked into the
Vulkan compute SDK as FSR4 kernels. We have not copied its code or captures.

The reference's example/tool code is GPL-3.0-or-later; its FSR1 shader math
also carries AMD's MIT notice. Any future adaptation must preserve the
applicable notices and pin the exact source revision. Its Python math self-test
was not run during this audit because WSL lacks NumPy.
