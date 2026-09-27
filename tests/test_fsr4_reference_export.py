# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
