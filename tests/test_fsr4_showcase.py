import inspect
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_fsr4_showcase as showcase  # noqa: E402
from build_fsr4_clear import build_native_app  # noqa: E402

OUTPUTS = {"OUT_1080P": (1920, 1080), "OUT_1440P": (2560, 1440), "OUT_4K": (3840, 2160)}
QUALITIES = {"Q_NATIVE": ("Native AA", 1.0), "Q_QUALITY": ("Quality", 1.5), "Q_BALANCED": ("Balanced", 1.7),
             "Q_PERFORMANCE": ("Performance", 2.0), "Q_ULTRA": ("Ultra Performance", 3.0)}


def font_available():
    try:
        import PIL  # noqa: F401
        showcase.font_path(showcase.FACES[0][1])
        return True
    except (ImportError, SystemExit):
        return False


def numpy_available():
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def source():
    return (showcase.SOURCE / "main.c").read_text(encoding="utf-8")


class Showcase(unittest.TestCase):
    def test_package_identity(self):
        self.assertEqual(showcase.TITLE_ID, "PPSA99011")
        self.assertRegex(showcase.CONTENT_ID, r"^UP9000-PPSA99011_00-[A-Z0-9]{16}$")
        # The other native apps keep their title unless they ask for another.
        defaults = inspect.signature(build_native_app).parameters
        self.assertEqual(defaults["title_id"].default, "PPSA88900")
        self.assertIsNone(defaults["sce_sys"].default)
        self.assertIsNone(defaults["param_overrides"].default)

    def test_content_version_follows_the_release(self):
        self.assertEqual(showcase.content_version("01.000.000"), "01.000.000")
        self.assertEqual(showcase.content_version("01.002.010"), "01.002.010")
        for development in (None, "0.0.0-dev", "1.2.3", "v01.000.000", "1.000.000"):
            self.assertEqual(showcase.content_version(development), "01.000.000")

    def test_build_label_is_checked_and_written_beside_the_eboot(self):
        self.assertEqual(showcase.build_label({}), "")
        self.assertEqual(showcase.build_label({"BUILD_LABEL": ""}), "")
        for good in ("PR 12, 1ae2fd0", "pacing test 2", "a", "x" * 40, "v1.0_rc-2 #3"):
            self.assertEqual(showcase.build_label({"BUILD_LABEL": good}), good)
        for bad in ("x" * 41, "PR 12\n", "a/b", "$(id)", "caf\u00e9", "tab\there", "\"q\""):
            with self.assertRaises(SystemExit):
                showcase.build_label({"BUILD_LABEL": bad})
        with tempfile.TemporaryDirectory() as out:
            package = Path(out)
            showcase.write_build_label(package, "")
            self.assertEqual(list(package.iterdir()), [])  # a release has no such file
            showcase.write_build_label(package, "PR 12, 1ae2fd0")
            self.assertEqual((package / "build-label.txt").read_bytes(), b"PR 12, 1ae2fd0\n")
            showcase.write_build_label(package, "")  # nor a later build in the same folder
            self.assertEqual(list(package.iterdir()), [])

    def test_pull_request_builds_are_named_by_number_and_commit(self):
        workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertIn('echo "artifact=${GITHUB_REPOSITORY##*/}-PR$PR_NUMBER-$short" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn('echo "BUILD_LABEL=PR $PR_NUMBER, $short" >> "$GITHUB_ENV"', workflow)
        self.assertIn('echo "artifact=ps5-fsr4-showcase-$GITHUB_SHA" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn("name: ${{ steps.label.outputs.artifact }}", workflow)
        self.assertIn("PR_HEAD: ${{ github.event.pull_request.head.sha }}", workflow)
        # A contributor's code is never built with write access or secrets.
        self.assertNotIn("pull_request_target", workflow)

    def test_scenarios_are_the_readme_performance_table(self):
        text = source()
        table = text[text.index("} SCENARIOS[] = {"):text.index("enum { SCENARIO_COUNT")]
        scenarios = []
        for output, quality, ms in re.findall(r"\{(OUT_\w+), (Q_\w+), ([\d.]+)\}", table):
            w, h = OUTPUTS[output]
            name, scale = QUALITIES[quality]
            scenarios.append((f"{int(w / scale)}×{int(h / scale)} → {w}×{h}", name, ms))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme[readme.index("## Performance"):]
        rows = re.findall(r"\| (\d+×\d+ → \d+×\d+) \| ([\w ]+?) \| ([\d.]+) ms \|", section)
        self.assertEqual(len(scenarios), 6)
        self.assertEqual(scenarios, [tuple(row) for row in rows])

    def test_tour_fits_the_quality_rules(self):
        text = source()
        tour = text[text.index("} TOUR[] = {"):text.index("enum { CHAPTERS")]
        chapters = re.findall(r"\n     (OUT_\w+), (Q_\w+), (V_\w+), ([01]), ([01]), ([01]), (\d+), (\d+)\}", tour)
        shots = text[text.index("} SHOTS[] = {"):text.index("enum { SHOT_COUNT")].count("\n    {{")
        self.assertEqual(len(chapters), 12)
        self.assertEqual(shots, 8)
        for output, quality, *_, shot, _ in chapters:
            self.assertFalse(output == "OUT_4K" and quality == "Q_NATIVE", "4K renders at most 2560x1440")
            self.assertLess(int(shot), shots)
        self.assertEqual(sum(int(c[-1]) for c in chapters), 152)

    def test_menu_names_every_row(self):
        text = source()
        rows = re.search(r"enum \{ (ROW_SCENARIO.*?), ROW_COUNT \};", text, re.S).group(1)
        names = re.search(r"ROW_NAMES\[ROW_COUNT\] = \{(.*?)\};", text, re.S).group(1)
        self.assertEqual(len(re.findall(r"ROW_\w+", rows)), len(re.findall(r'"[^"]+"', names)))

    @unittest.skipUnless(font_available(), "Pillow and DejaVu Sans are needed")
    def test_font_covers_every_character_the_app_draws(self):
        with tempfile.TemporaryDirectory() as out:
            summary = showcase.build_font(Path(out))
            header = (Path(out) / "fsr4_showcase_font.h").read_text()
        text = "".join(p.read_text(encoding="utf-8") for p in showcase.SOURCE.glob("*.c"))
        wanted = {ord(c) for c in text if ord(c) > 127} | set(range(32, 127))
        self.assertEqual(summary["glyphs"], len(wanted))
        for face, _, _ in showcase.FACES:
            glyphs = re.search(r"hud_glyphs_%s\[\] = \{(.*?)\};" % face, header).group(1)
            codepoints = {int(g) for g in re.findall(r"\{(\d+),", glyphs)}
            self.assertEqual(codepoints, wanted)
            self.assertIn(f"hud_font_{face} = ", header)

    @unittest.skipUnless(font_available(), "Pillow and DejaVu Sans are needed")
    def test_signs_match_the_scene_shader(self):
        atlas = showcase.build_signs()
        shader = (showcase.SOURCE / "city.comp").read_text(encoding="utf-8")
        w, h = map(int, re.search(r"SIGN_W = (\d+), SIGN_H = (\d+)", shader).groups())
        self.assertEqual(atlas.size, (w, h))
        self.assertEqual(atlas.mode, "L")
        for tile in range(4):  # every sign has lettering
            box = ((tile & 1) * w // 2, (tile >> 1) * h // 2, (tile & 1) * w // 2 + w // 2, (tile >> 1) * h // 2 + h // 2)
            self.assertGreater(atlas.crop(box).getextrema()[1], 200)

    @unittest.skipUnless(shutil.which(showcase.glslang()) or Path(showcase.glslang()).exists(),
                         "glslangValidator is needed")
    def test_shaders_compile(self):
        with tempfile.TemporaryDirectory() as out:
            showcase.compile_shaders(Path(out))
            header = (Path(out) / "fsr4_showcase_shaders.h").read_text()
            native = (Path(out) / "fsr4_showcase_native.spv").read_bytes()
            scene = (Path(out) / "fsr4_showcase_scene.spv").read_bytes()
        for name, _, _ in showcase.SHADERS:
            self.assertIn(f"fsr4_showcase_{name}_spv[]", header)
        self.assertLess(len(native), len(scene))  # the native render writes color only

    def test_launch_assets_have_the_console_formats(self):
        folder = showcase.SOURCE / "sce_sys"
        self.assertEqual({p.name for p in folder.iterdir()}, {"icon0.png", "pic0.dds", "pic1.dds", "snd0.at9"})
        png = (folder / "icon0.png").read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (512, 512))
        for name in ("pic0.dds", "pic1.dds"):
            dds = (folder / name).read_bytes()
            self.assertEqual(dds[:4], b"DDS ")
            self.assertEqual(struct.unpack("<II", dds[12:20]), (2160, 3840))
            self.assertEqual(dds[84:88], b"DX10")
            self.assertEqual(struct.unpack("<I", dds[128:132])[0], 98)  # DXGI_FORMAT_BC7_UNORM
            self.assertEqual(len(dds), 148 + 3840 * 2160)
        self.assertLess((folder / "snd0.at9").stat().st_size, 2 << 20)  # the console's limit

    @unittest.skipUnless(numpy_available(), "numpy and Pillow are needed")
    def test_bc7_blocks_decode_to_the_picture(self):
        import numpy as np
        import build_fsr4_showcase_assets as assets
        y, x = np.mgrid[0:32, 0:64].astype(np.float32)
        picture = np.stack([x * 4, y * 8, 255 - x * 2 - y * 2], 2).clip(0, 255).astype(np.uint8)
        data = assets.bc7_blocks(picture)
        self.assertEqual(len(data), 16 * 8 * 16)
        weights = [0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64]
        decoded = np.zeros_like(picture)
        for block in range(8 * 16):
            low, high = struct.unpack_from("<QQ", data, block * 16)
            self.assertEqual(low & 0x7F, 0x40)  # mode 6
            ends = [(low >> (7 + 7 * n)) & 0x7F for n in range(8)]
            p0, p1 = low >> 63, high & 1
            e0 = [ends[c * 2] << 1 | p0 for c in range(3)]
            e1 = [ends[c * 2 + 1] << 1 | p1 for c in range(3)]
            self.assertEqual((ends[6] << 1 | p0, ends[7] << 1 | p1), (255, 255))  # opaque
            for k in range(16):
                index = (high >> 1) & 7 if k == 0 else (high >> (4 * k)) & 15
                w = weights[index]
                by, bx = divmod(block, 16)
                decoded[by * 4 + k // 4, bx * 4 + k % 4] = [((64 - w) * a + w * b + 32) >> 6 for a, b in zip(e0, e1)]
        error = ((decoded.astype(np.float32) - picture) ** 2).mean()
        self.assertGreater(10 * np.log10(255 ** 2 / error), 35)  # two gradients cross in every block


if __name__ == "__main__":
    unittest.main()
