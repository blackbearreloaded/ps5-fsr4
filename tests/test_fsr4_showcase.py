import inspect
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_fsr4_showcase as showcase  # noqa: E402
from build_fsr4_clear import build_native_app  # noqa: E402


def font_available():
    try:
        import PIL  # noqa: F401
        showcase.font_path(showcase.FACES[0][1])
        return True
    except (ImportError, SystemExit):
        return False


class Showcase(unittest.TestCase):
    def test_package_identity(self):
        self.assertEqual(showcase.TITLE_ID, "PPSA99010")
        self.assertRegex(showcase.CONTENT_ID, r"^UP9000-PPSA99010_00-[A-Z0-9]{16}$")
        # The other native apps keep their title unless they ask for another.
        defaults = inspect.signature(build_native_app).parameters
        self.assertEqual(defaults["title_id"].default, "PPSA88900")
        self.assertIsNone(defaults["sce_sys"].default)

    def test_tour_fits_the_quality_rules(self):
        source = (showcase.SOURCE / "main.c").read_text(encoding="utf-8")
        tour = source[source.index("} TOUR[] = {"):source.index("enum { CHAPTERS")]
        chapters = re.findall(r"\n     (OUT_\w+), (Q_\w+), (V_\w+), ([01]), ([01]), ([01]), (\d+)\}", tour)
        self.assertEqual(len(chapters), 10)
        for output, quality, *_ in chapters:
            self.assertFalse(output == "OUT_4K" and quality == "Q_NATIVE", "4K renders at most 2560x1440")
        self.assertEqual(sum(int(c[-1]) for c in chapters), 110)

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


if __name__ == "__main__":
    unittest.main()
