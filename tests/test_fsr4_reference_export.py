# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


class ResourceId:
    def __str__(self):
        return "ResourceId::7"


class SwigArray:
    # RenderDoc arrays implement the sequence protocol without __iter__.
    def __len__(self):
        return 2

    def __getitem__(self, index):
        return [3, 5][index]


class SwigRoot:
    this = object()
    thisown = True
    parameters = SwigArray()
    resourceId = ResourceId()
    gpuAddress = 0x12345678


class ReferenceExport(unittest.TestCase):
    def test_preserve_swig_values_reject_opaque_objects_and_corrupt_blobs(self):
        path = Path(__file__).resolve().parents[1] / "tools/fsr4_reference_export.py"
        spec = importlib.util.spec_from_file_location("reference_export_test", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", renderdoc=types.SimpleNamespace(ResourceId=ResourceId)):
            spec.loader.exec_module(module)
        self.assertEqual(module.serialise(SwigRoot()),
                         dict(parameters=[3, 5], resourceId="ResourceId::7"))
        with self.assertRaises(TypeError):
            module.serialise(object())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            record = module.blob(out, b"known tensor")
            self.assertEqual(record["bytes"], 12)
            self.assertEqual(module.blob(out, b"known tensor"), record)
            (out / (record["sha256"] + ".bin")).write_bytes(b"damaged")
            with self.assertRaises(ValueError):
                module.blob(out, b"known tensor")


    def test_parameterized_export_requires_matching_independent_reference(self):
        path = Path(__file__).resolve().parents[1] / "tools/fsr4_reference_export.py"
        spec = importlib.util.spec_from_file_location("reference_export_test", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", renderdoc=types.SimpleNamespace(ResourceId=ResourceId)):
            spec.loader.exec_module(module)
        self.assertEqual(module.export_expectations(Path("unused"), "scalar-unpack")[0], 4)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference, captured = root / "reference", root / "captured"
            reference.mkdir()
            captured.mkdir()
            pixels = bytes(240 * 144 * 16)
            digest = hashlib.sha256(pixels).hexdigest()
            capture = captured / "probe.rdc"
            capture.write_bytes(b"capture")
            manifest = dict(
                workload=dict(render_size=[160, 96], output_size=[240, 144],
                              frames=1, scenario="static"),
                provider_sha256="provider", reference_variant="scalar-unpack",
                probe_source_sha256="source", runtime_sha256={"d3d10warp.dll": module.WARP_SHA},
                process_exit=0, readback=dict(bytes=len(pixels), sha256=digest))
            for directory in (reference, captured):
                (directory / "output.rgba32f").write_bytes(pixels)
                (directory / "run.json").write_text(json.dumps(manifest))
            manifest["capture"] = dict(file=capture.name,
                                       sha256=hashlib.sha256(capture.read_bytes()).hexdigest())
            (captured / "run.json").write_text(json.dumps(manifest))
            frames, expected, provenance = module.export_expectations(
                capture, "scalar-unpack", reference)
            self.assertEqual((frames, expected), (1, digest))
            self.assertTrue(provenance["output_matches_separate_uncaptured_run"])
            with self.assertRaisesRegex(ValueError, "separate uncaptured"):
                module.export_expectations(capture, "scalar-unpack", captured)
            manifest["workload"]["frames"] = 2
            (captured / "run.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "identity mismatch: workload"):
                module.export_expectations(capture, "scalar-unpack", reference)
            manifest["workload"]["frames"] = 1
            (captured / "run.json").write_text(json.dumps(manifest))
            (reference / "output.rgba32f").write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "readback identity"):
                module.export_expectations(capture, "scalar-unpack", reference)
            (reference / "output.rgba32f").write_bytes(pixels)
            capture.write_bytes(b"wrong capture")
            with self.assertRaisesRegex(ValueError, "Capture identity"):
                module.export_expectations(capture, "scalar-unpack", reference)


if __name__ == "__main__":
    unittest.main()
