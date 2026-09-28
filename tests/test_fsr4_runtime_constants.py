"""The runtime reproduces the captured reference constants and dispatch sizes exactly."""
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "build/reference-runtime"
MANIFEST = ROOT / "build/fsr4-runtime/manifest.json"
VULKAN = ROOT / "third_party/vulkan-headers/include"
WORDS = {"SPD": 7, "MLSR": 26, "TENSOR": 68}
RULES = ("SPD", "PREPASS", "POSTPASS", "NETWORK", "PADDING")


def probe_parameters(scenario, frame, render):
    """Dispatch parameters of the pinned reference probe
    (references/bc250-fsr4-fork/dll/probe/provider_probe.c)."""
    jitter = (0.0, 0.0)
    if scenario == "motion":
        jitter = ((frame % 4) * .25 - .375, (frame % 3) * .3333333 - .3333333)
    width, height = render
    if scenario == "resize" and frame % 4 >= 2:
        width, height = width * 3 // 4, height * 3 // 4
    reset = frame == 0 or (scenario == "reset" and frame % 4 == 0)
    return jitter, (width, height), reset


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
        cls.lib.fsr4_test_encode.argtypes = [u, u, u, u, u, u, f, f, f, f, f, ctypes.c_int, f,
                                             ctypes.POINTER(u), ctypes.POINTER(u), ctypes.POINTER(u)]
        cls.lib.fsr4_test_groups.argtypes = [u] * 10 + [ctypes.POINTER(u), ctypes.POINTER(u)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def load(self, name):
        path = EXPORTS / f"capture-export-{name}"
        if not (path / "complete.json").is_file():
            self.skipTest(f"local reference export {name} unavailable")
        graph = json.loads((path / "graph.json").read_text())
        textures = {t["resourceId"]: t for t in graph["textures"]}
        dispatches = graph["dispatches"]
        output = textures[dispatches[27]["uav"][2]["descriptor"]["resource"]]
        color = textures[dispatches[0]["srv"][0]["descriptor"]["resource"]]
        return path, graph, dispatches, (output["width"], output["height"]), (color["width"], color["height"])

    def check_export(self, name, scenario):
        path, _, dispatches, (ow, oh), render = self.load(name)
        previous = 0.0
        for frame in range(len(dispatches) // 28):
            (jx, jy), (rw, rh), reset = probe_parameters(scenario, frame, render)
            blocks = {k: (ctypes.c_uint32 * 68)() for k in WORDS}
            rc = self.lib.fsr4_test_encode(render[0], render[1], rw, rh, ow, oh, jx, jy, rw, rh, 1.0,
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

    def check_geometry(self, name, scenario):
        if not MANIFEST.is_file():
            self.skipTest("generated runtime tables unavailable")
        rules = [p["groups"] for p in json.loads(MANIFEST.read_text())["passes"]]
        _, graph, dispatches, (ow, oh), render = self.load(name)
        names = {r["resourceId"]: r["name"] for r in graph["resources"]}
        sizes = {names[t["resourceId"]]: (t["width"], t["height"]) for t in graph["textures"]}
        buffers = {names[b["resourceId"]]: b.get("length", b.get("byteSize")) for b in graph["buffers"]}
        self.assertEqual(buffers["FSR4UPSCALER_ScratchBuffer"], 20880256)
        for frame in range(len(dispatches) // 28):
            _, (rw, rh), _ = probe_parameters(scenario, frame, render)
            for index, (rule, tensor, limit_w, limit_h) in enumerate(rules):
                groups, luma = (ctypes.c_uint32 * 3)(), (ctypes.c_uint32 * 2)()
                self.assertEqual(self.lib.fsr4_test_groups(render[0], render[1], ow, oh, rw, rh,
                                                           RULES.index(rule), tensor, limit_w, limit_h,
                                                           groups, luma), 0)
                self.assertEqual(list(groups), dispatches[frame * 28 + index]["dispatchDimension"],
                                 f"{name} frame {frame} pass {index} {rule}")
            self.assertEqual(tuple(luma), sizes["FSR4UPSCALER_Luma_Mip_5"])

    def test_static_reference(self):
        self.check_export("scalar-unpack", "static")
        self.check_geometry("scalar-unpack", "static")

    def test_motion_jitter(self):
        self.check_export("motion", "motion")

    def test_periodic_reset(self):
        self.check_export("reset", "reset")

    def test_standard_dynamic_range(self):
        self.check_export("sdr", "sdr")

    def test_dynamic_resolution(self):
        self.check_export("resize", "resize")
        self.check_geometry("resize", "resize")

    def test_wider_output(self):
        self.check_export("size240", "static")
        self.check_geometry("size240", "static")

    def test_unaligned_output(self):
        self.check_export("size320", "static")
        self.check_geometry("size320", "static")


if __name__ == "__main__":
    unittest.main()
