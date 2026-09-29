"""The runtime reproduces the captured reference constants and dispatch sizes exactly."""
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fsr4_paths import VULKAN_HEADERS  # noqa: E402
from fsr4_int8_kernels import LAYOUTS  # noqa: E402
EXPORTS = ROOT / "build/reference-runtime"
MANIFEST = ROOT / "build/fsr4-runtime/manifest.json"
VULKAN = VULKAN_HEADERS
WORDS = {"SPD": 7, "MLSR": 26, "TENSOR": 68, "RCAS": 8}
RULES = ("SPD", "PREPASS", "POSTPASS", "NETWORK", "PADDING", "RCAS", "NONE")
SHARPNESS = {"rcas": 0.4}


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
                                             ctypes.c_int, f] + [ctypes.POINTER(u)] * 4
        cls.lib.fsr4_test_groups.argtypes = [u] * 10 + [ctypes.POINTER(u), ctypes.POINTER(u)]
        cls.lib.fsr4_test_mode.argtypes, cls.lib.fsr4_test_mode.restype = [u, u], u
        cls.lib.fsr4_test_band.argtypes = [u, u]

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
        # 28 dispatches per frame, 29 when the frame ends with RCAS sharpening.
        self.per_frame = 29 if len(dispatches) > 28 and "rcas" in dispatches[28]["entryPoint"] else 28
        output = textures[dispatches[27]["uav"][2]["descriptor"]["resource"]]
        color = textures[dispatches[0]["srv"][0]["descriptor"]["resource"]]
        return path, graph, dispatches, (output["width"], output["height"]), (color["width"], color["height"])

    def check_export(self, name, scenario):
        path, _, dispatches, (ow, oh), render = self.load(name)
        previous, per = 0.0, self.per_frame
        sharpness = SHARPNESS.get(scenario)
        for frame in range(len(dispatches) // per):
            (jx, jy), (rw, rh), reset = probe_parameters(scenario, frame, render)
            blocks = {k: (ctypes.c_uint32 * 68)() for k in WORDS}
            rc = self.lib.fsr4_test_encode(render[0], render[1], rw, rh, ow, oh, jx, jy, rw, rh, 1.0,
                                           int(reset), 0.0 if reset else previous,
                                           sharpness is not None, sharpness or 0.0,
                                           blocks["SPD"], blocks["MLSR"], blocks["TENSOR"], blocks["RCAS"])
            self.assertEqual(rc, 0)
            previous = 1.0
            for index in range(per):
                d = dispatches[frame * per + index]
                if not d["cbv"]:
                    continue
                kind = ("SPD" if index == 0 else "MLSR" if index in (1, 27) else
                        "RCAS" if index == 28 else "TENSOR")
                data = (path / (d["cbv"][0]["at_event"]["sha256"] + ".bin")).read_bytes()
                expected = list(memoryview(data[:4 * WORDS[kind]]).cast("I"))
                actual = list(blocks[kind])[:WORDS[kind]]
                self.assertEqual(actual, expected, f"{name} frame {frame} pass {index} {kind}")

    def check_geometry(self, name, scenario):
        if not MANIFEST.is_file():
            self.skipTest("generated runtime tables unavailable")
        manifest = json.loads(MANIFEST.read_text())
        _, graph, dispatches, (ow, oh), render = self.load(name)
        # The family FidelityFX ran: the output's resolution band, Ultra Performance from a 2.99 ratio.
        band = int(ow > 1920 or oh > 1080)
        model = "ultra-performance" if self.lib.fsr4_test_mode(ow, render[0]) == 5 else "standard"
        family = next((f for f in manifest["families"] if f["family"] == f"{model}-band{band}"), None)
        if family is None:
            self.skipTest(f"{model}-band{band} is not in the generated runtime tables")
        rules = [p["groups"] for p in family["passes"]]
        # A generated postpass head runs per H pixel in place of the captured border clear, and
        # banked generated kernels add workgroup layers.
        replaced = ({26} & set(manifest.get("int8_kernels", []))) | {int(k) for k in manifest.get("int8_banks", {})}
        names = {r["resourceId"]: r["name"] for r in graph["resources"]}
        sizes = {names[t["resourceId"]]: (t["width"], t["height"]) for t in graph["textures"]}
        buffers = {names[b["resourceId"]]: b.get("length", b.get("byteSize")) for b in graph["buffers"]}
        self.assertEqual(buffers["FSR4UPSCALER_ScratchBuffer"], LAYOUTS[band]["scratch"])
        per = self.per_frame
        for frame in range(len(dispatches) // per):
            _, (rw, rh), _ = probe_parameters(scenario, frame, render)
            for index, (rule, tensor, limit_w, limit_h) in enumerate(rules[:per]):
                if index in replaced or rule == "NONE":  # not dispatched at all
                    continue
                groups, luma = (ctypes.c_uint32 * 3)(), (ctypes.c_uint32 * 2)()
                self.assertEqual(self.lib.fsr4_test_groups(render[0], render[1], ow, oh, rw, rh,
                                                           RULES.index(rule), tensor, limit_w, limit_h,
                                                           groups, luma), 0)
                self.assertEqual(list(groups), dispatches[frame * per + index]["dispatchDimension"],
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

    def test_sharpening(self):
        self.check_export("rcas", "rcas")
        self.check_geometry("rcas", "rcas")

    def test_ultra_performance(self):
        self.check_export("up", "static")
        self.check_geometry("up", "static")

    def test_ultra_performance_motion(self):
        self.check_export("up-motion", "motion")

    def test_odd_output(self):
        self.check_export("odd", "static")
        self.check_geometry("odd", "static")

    def test_2k_output(self):
        self.check_export("k2", "static")
        self.check_geometry("k2", "static")

    def test_2k_motion(self):
        self.check_export("k2-motion", "motion")

    def test_2k_quality(self):
        self.check_export("k2-quality", "static")
        self.check_geometry("k2-quality", "static")

    def test_4k_output(self):
        self.check_export("k4", "static")
        self.check_geometry("k4", "static")

    def test_4k_motion(self):
        self.check_export("k4-motion", "motion")

    def test_4k_ultra_performance(self):
        self.check_export("k4-up", "static")
        self.check_geometry("k4-up", "static")

    def test_modes(self):
        # FidelityFX's per-frame mode of the output-to-render width ratio; 5 is Ultra Performance.
        for render, mode in ((1920, 0), (1281, 0), (1280, 1), (1137, 1), (1136, 2), (965, 2), (964, 3),
                             (643, 3), (642, 5), (640, 5)):
            self.assertEqual(self.lib.fsr4_test_mode(1920, render), mode, render)

    def test_bands(self):
        # Band 0 shaders cover outputs up to 1920x1080, band 1 up to 3840x2160.
        for size, band in (((1920, 1080), 0), ((1921, 1080), 1), ((1920, 1081), 1), ((2560, 1440), 1),
                           ((3840, 2160), 1), ((3841, 2160), -1), ((3840, 2161), -1)):
            self.assertEqual(self.lib.fsr4_test_band(*size), band, size)


if __name__ == "__main__":
    unittest.main()
