"""The runtime constant encoders reproduce the captured reference constants exactly."""
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "build/reference-runtime"
VULKAN = ROOT / "third_party/vulkan-headers/include"
# Dispatch parameters of the pinned reference probe (references/bc250-fsr4-fork/dll/probe/provider_probe.c).
SCENARIOS = {"scalar-unpack": "static"}
WORDS = {"SPD": 7, "MLSR": 26, "TENSOR": 68}


def probe_parameters(scenario, frame, render):
    jitter = (0.0, 0.0)
    if scenario == "motion":
        jitter = ((frame % 4) * .25 - .375, (frame % 3) * .3333333 - .3333333)
    reset = frame == 0 or (scenario == "reset" and frame % 4 == 0)
    return jitter, render, reset


class RuntimeConstants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (VULKAN / "vulkan/vulkan_core.h").is_file():
            raise unittest.SkipTest("pinned Vulkan headers not prepared")
        cls.tmp = tempfile.TemporaryDirectory()
        lib = Path(cls.tmp.name) / "shim.so"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        "-I" + str(ROOT / "include"), "-I" + str(VULKAN),
                        str(ROOT / "tests/fsr4_constants_shim.c"), "-o", str(lib)], check=True)
        cls.lib = ctypes.CDLL(str(lib))
        f, u = ctypes.c_float, ctypes.c_uint32
        cls.lib.fsr4_test_encode.argtypes = [u, u, u, u, f, f, f, f, f, ctypes.c_int, f,
                                             ctypes.POINTER(u), ctypes.POINTER(u), ctypes.POINTER(u)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check_export(self, name, scenario):
        path = EXPORTS / f"capture-export-{name}"
        if not (path / "complete.json").is_file():
            self.skipTest(f"local reference export {name} unavailable")
        graph = json.loads((path / "graph.json").read_text())
        textures = {t["resourceId"]: t for t in graph["textures"]}
        dispatches = graph["dispatches"]
        output = textures[dispatches[27]["uav"][2]["descriptor"]["resource"]]
        color = textures[dispatches[0]["srv"][0]["descriptor"]["resource"]]
        previous = 0.0
        for frame in range(len(dispatches) // 28):
            (jx, jy), (rw, rh), reset = probe_parameters(scenario, frame, (color["width"], color["height"]))
            blocks = {k: (ctypes.c_uint32 * 68)() for k in WORDS}
            rc = self.lib.fsr4_test_encode(rw, rh, output["width"], output["height"], jx, jy, rw, rh, 1.0,
                                           int(reset), 0.0 if reset else previous,
                                           blocks["SPD"], blocks["MLSR"], blocks["TENSOR"])
            self.assertEqual(rc, 0)
            previous = 1.0
            for index in range(28):
                d = dispatches[frame * 28 + index]
                if not d["cbv"]:
                    continue
                kind = ("SPD" if index == 0 else "MLSR" if index in (1, 27) else "TENSOR")
                data = (path / (d["cbv"][0]["at_event"]["sha256"] + ".bin")).read_bytes()
                expected = list(memoryview(data[:4 * WORDS[kind]]).cast("I"))
                actual = list(blocks[kind])[:WORDS[kind]]
                self.assertEqual(actual, expected, f"{name} frame {frame} pass {index} {kind}")

    def test_static_reference(self):
        self.check_export("scalar-unpack", "static")


if __name__ == "__main__":
    unittest.main()
