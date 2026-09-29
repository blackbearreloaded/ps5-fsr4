# PS5 FSR4 Showcase

A native PS5 app, packaged as its own title (**PPSA99010**, *PS5 FSR4 Showcase*),
that shows what the `ps5_fsr4` SDK does on the console. It renders the demo
scene (`examples/fsr4_demo_scene.comp`), upscales it with FSR4 and presents it at
1920×1080, with a guided tour, live timings and comparison views:

- **Every quality mode:** Native AA (1×), Quality (1.5×), Balanced (1.7×),
  Performance (2×) and Ultra Performance (3×), which runs AMD's dedicated
  Ultra Performance model. The mode can change from one frame to the next.
- **Three output sizes:** 1920×1080, 2560×1440 and 3840×2160. Larger outputs
  are box-filtered to the 1080p display, so the whole frame is supersampled,
  and the lens shows the output's own pixels. Changing the output recreates
  the FSR4 context, which takes about 85 ms.
- **Comparisons:** FSR4 alone, split against a bilinear upscale of the same
  jittered frame or against a native render at output size without
  anti-aliasing, or either reference alone.
- **Lens:** a magnifier (2× to 8×). In a split view it becomes a pair of lenses
  that show the same pixels from both sources side by side.
- **Dynamic resolution:** a context created with
  `PS5FSR4_FLAG_DYNAMIC_RESOLUTION`, whose render size sweeps between the
  mode's size and half of it every four seconds while history is kept.
- **RCAS sharpening** with adjustable strength.
- **HUD:** mode, render and output sizes, FSR4 and scene times and the frame
  rate. The CPU draws it and uploads only what changed.

## Guided tour

The app starts with a tour of ten chapters, about 110 seconds in all, and
repeats it. Any button or stick takes over. The tour resumes after 45 seconds
without input, or when OPTIONS is pressed.

| # | Chapter | Configuration |
| --- | --- | --- |
| 1 | FSR 4 on PlayStation 5 | 1280×720 → 1920×1080 (Quality) |
| 2 | FSR 4 against bilinear | Quality, split against bilinear, lens pair |
| 3 | Every quality mode | Quality, Balanced, Performance and Ultra Performance in turn, split against bilinear |
| 4 | Ultra Performance | 640×360 → 1920×1080, split against bilinear, lens pair |
| 5 | Dynamic resolution | render size sweeping between 1280×720 and 640×360 |
| 6 | RCAS sharpening | strength sweeping from 0 to 1, lens |
| 7 | 1440p output | 1280×720 → 2560×1440 (Performance), lens on 1440p pixels |
| 8 | 4K output | 1920×1080 → 3840×2160 (Performance), split against bilinear, lens pair on 4K pixels |
| 9 | 4K from 720p | 1280×720 → 3840×2160 (Ultra Performance), split against bilinear, lens pair |
| 10 | FSR 4 against native rendering | 960×540 → 1920×1080 against native 1080p without anti-aliasing, lens pair |

## Controls

| Input | Action |
| --- | --- |
| Left stick | Orbit the camera |
| L2 / R2 | Camera distance |
| Right stick | Move the lens, or the split when the lens is off |
| Cross | Next view: FSR 4, against bilinear, against native, bilinear, native |
| L1 / R1 | Quality mode (at 4K, Native AA is skipped: it would render at 4K) |
| Circle | Next output size: 1080p, 1440p, 4K |
| Square | Lens on or off; D-pad left and right change its zoom |
| Triangle | Sharpening on or off; D-pad up and down change its strength |
| L3 | Dynamic resolution on or off |
| R3 | Pause the scene |
| OPTIONS | Guided tour on or off |
| Touch pad | Hide or show the HUD |

## Performance on PS5

Measured by the `--selftest` build in the FSR4 view (times from submission to
fence, averaged per setting):

| Output | Mode | Render size | FSR4 (ms) | Scene (ms) | Frame rate |
| --- | --- | --- | ---: | ---: | ---: |
| 1920×1080 | Native AA | 1920×1080 | 2.75 | 4.6 | 60 |
| 1920×1080 | Quality | 1280×720 | 2.66 | 2.4 | 60 |
| 1920×1080 | Balanced | 1129×635 | 2.64 | 2.1 | 60 |
| 1920×1080 | Performance | 960×540 | 2.63 | 1.7 | 60 |
| 1920×1080 | Ultra Performance | 640×360 | 2.66 | 1.0 | 60 |
| 2560×1440 | Native AA | 2560×1440 | 4.66 | 7.4 | 60 |
| 2560×1440 | Quality | 1706×960 | 4.40 | 3.9 | 60 |
| 2560×1440 | Balanced | 1505×847 | 4.37 | 3.1 | 60 |
| 2560×1440 | Performance | 1280×720 | 4.35 | 2.4 | 60 |
| 2560×1440 | Ultra Performance | 853×480 | 4.36 | 1.5 | 60 |
| 3840×2160 | Quality | 2560×1440 | 10.40 | 7.4 | 30 |
| 3840×2160 | Balanced | 2258×1270 | 9.56 | 6.0 | 30 |
| 3840×2160 | Performance | 1920×1080 | 9.65 | 4.1 | 48–50 |
| 3840×2160 | Ultra Performance | 1280×720 | 9.78 | 2.5 | 60 |

FSR4's cost follows the output size: the network runs at output resolution, so
the quality mode mostly changes what the scene costs. Composing the display
frame adds 1.6 to 1.9 ms. The driver runs a frame's submissions and the flip
one after another, so a frame longer than 16.7 ms waits for the next vblank. At
4K, only Ultra Performance leaves room for this ray-marched scene at 60 fps.
The native comparison views render the scene a second time at output size,
which costs 3.8 ms at 1080p and about 16 ms at 4K.

## Build and install

Stage the SDK first (`make sdk`, see [BUILDING.md](../../BUILDING.md)), then:

```sh
make showcase
```

This builds `build/fsr4-showcase/PPSA99010` with the SDK's warmed pipeline
cache. Upload the `PPSA99010` folder to `/data/homebrew/` and launch it from the
home screen once it is registered. The app runs until it is closed. It writes
`fsr4-showcase-log.txt` to `/data/fsr4-results` (or to its own `results`
folder) with `FSR4_SHOWCASE_*` lines: setup, chapters and averaged timings
every 120 frames.

`SHOWCASE_ARGS` passes options to `tools/build_fsr4_showcase.py`:

- `--release VERSION` also writes `ps5-fsr4-showcase-VERSION-PPSA99010.zip`
  and its SHA-256. The app embeds `libps5_fsr4.a` and its pipeline cache, so
  AMD's terms apply to the zip as to any build: see
  [LICENSING.md](../../LICENSING.md#amd-fsr4-material) before distributing it.
- `--screenshots` saves a 1920×1080 BGRA frame of each chapter during the first
  tour (`fsr4-showcase-NN.bgra`, next to the log).
- `--selftest` replaces the controller with a scripted walk: every view at
  every quality mode of each output, then each toggle. It logs one
  `FSR4_SHOWCASE_STEP` line with averaged timings per setting and ends with
  `FSR4_SHOWCASE_SELFTEST_DONE`.

## Launch assets

The package currently uses the native app template's generic icon, backgrounds
and music. To replace them, add any of these files to `sce_sys/` in this
directory. The builder takes each file it finds there and the template's for
the rest:

| File | Format |
| --- | --- |
| `icon0.png` | 512×512 PNG |
| `pic0.dds`, `pic1.dds` | 3840×2160 BC7 DDS (DX10 header) |
| `snd0.at9` | ATRAC9 |

The HUD text uses DejaVu Sans, rasterized at build time from the system font
(`fonts-dejavu-core`) under the Bitstream Vera license.
