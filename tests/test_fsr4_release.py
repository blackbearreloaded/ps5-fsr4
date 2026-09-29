import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import fetch_build_inputs as inputs  # noqa: E402
import package_fsr4_release as release  # noqa: E402

HEX64 = re.compile(r"[0-9a-f]{64}")


class BuildInputs(unittest.TestCase):
    def test_pins(self):
        pins = json.loads(inputs.PINS.read_text())
        for name in ("app_template", "dxil_spirv"):
            self.assertRegex(pins[name]["commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(pins[name]["repository"].startswith("https://github.com/"))
        for name in ("dxc", "bc250_dll", "pipeline_cache"):
            self.assertTrue(pins[name]["url"].startswith("https://github.com/"), name)
            self.assertRegex(pins[name]["sha256"], HEX64)
        cache = pins["pipeline_cache"]
        self.assertRegex(cache["table_sha256"], HEX64)
        self.assertIn(cache["sha256"][:12], cache["url"])
        self.assertTrue(cache["url"].endswith(".bin") and "/releases/download/build-inputs/" in cache["url"])
        # The converter's pin and the DLL's digest have one source each.
        self.assertIn(pins["dxil_spirv"]["commit"], (ROOT / "Makefile").read_text())
        self.assertEqual(pins["bc250_dll"]["members"][0], "amd_fidelityfx_upscaler_dx12.dll")

    def test_cache_identity_matches_the_driver(self):
        # The cache a PS5 saved (vendor 0x1002, device 0) for the pinned driver. When the
        # driver's cache identity changes, the pinned cache must be made again.
        pins = json.loads(inputs.PINS.read_text())
        self.assertEqual(inputs.cache_uuid(0x1002, 0, inputs.driver_constants()),
                         pins["pipeline_cache"]["cache_uuid"])

    def test_cache_uuid_follows_every_input(self):
        constants = inputs.driver_constants()
        base = inputs.cache_uuid(0x1002, 0, constants)
        self.assertNotEqual(inputs.cache_uuid(0x1002, 1, constants), base)
        for name in inputs.UUID_INPUTS:
            changed = dict(constants, **{name: constants[name] + 1})
            self.assertNotEqual(inputs.cache_uuid(0x1002, 0, changed), base, name)

    def test_driver_constants_parse(self):
        with tempfile.TemporaryDirectory() as d:
            header = Path(d) / "profile.h"
            header.write_text("\n".join(f"#define {n} {v}u /* x */" for n, v in
                                        zip(inputs.UUID_INPUTS, (1013, 1, 0x50534243, 3, 1, 1))) + "\n")
            values = inputs.driver_constants(header)
        self.assertEqual(values["PS5VK_COMPILER_IDENTITY"], 0x50534243)
        self.assertEqual(values["PS5VK_GFX_TARGET"], 1013)


class Packaging(unittest.TestCase):
    def test_zip_is_reproducible_and_lists_its_files(self):
        entries = {"b/x.bin": b"\x00\x01", "a.txt": b"text"}
        with tempfile.TemporaryDirectory() as d:
            first = release.write_zip(Path(d) / "one.zip", entries).read_bytes()
            second = release.write_zip(Path(d) / "two.zip", dict(reversed(entries.items()))).read_bytes()
            with zipfile.ZipFile(Path(d) / "one.zip") as z:
                sums = z.read("SHA256SUMS").decode()
                names = z.namelist()
        self.assertEqual(first, second)
        self.assertEqual(names, ["SHA256SUMS", "a.txt", "b/x.bin"])
        self.assertIn(f"{release.sha256(b'text')}  a.txt\n", sums)

    def test_listed_takes_tracked_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "kept.c").write_text("int x;\n")
            (repo / "skip").mkdir()
            (repo / "skip/big.png").write_bytes(b"png")
            (repo / "untracked.o").write_bytes(b"o")
            subprocess.run(["git", "add", "kept.c", "skip/big.png"], cwd=repo, check=True)
            names = [name for name, _ in release.listed(repo, "top", exclude=("skip/",))]
        self.assertEqual(names, ["top/kept.c"])

    def test_release_notes_name_every_asset(self):
        version = "1.2.3"
        assets = [f"ps5-fsr4-showcase-{version}-PPSA99010.zip", f"ps5-fsr4-sdk-{version}.zip",
                  f"ps5-fsr4-{version}-source.tar.gz"]
        notes = release.release_notes(version, assets)
        for name in [*assets, "SHA256SUMS"]:
            self.assertIn(f"`{name}`", notes)
        self.assertIn("not affiliated", " ".join(notes.split()))

    def test_provenance_names_the_pinned_inputs(self):
        text = release.provenance()
        pins = json.loads(inputs.PINS.read_text())
        self.assertIn(pins["bc250_dll"]["sha256"], text)
        self.assertIn(json.loads((ROOT / "tools/fsr4_dll_map.json").read_text())["dll_sha256"], text)
        self.assertIn("MIT", text)


if __name__ == "__main__":
    unittest.main()
