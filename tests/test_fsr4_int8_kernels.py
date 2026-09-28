# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Packed-i16 kernel generator: overflow-free grouping and compilable kernels for every pass."""
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fsr4_int8_kernels as kernels  # noqa: E402


class Grouping(unittest.TestCase):
    def test_groups_cover_inputs_without_overflow(self):
        rng = random.Random(7)
        for low, high in ((-128, 127), (0, 127)):
            for _ in range(200):
                n = rng.randint(1, 160)
                rows = [[rng.randint(-128, 127) if rng.random() > 0.05 else 0 for _ in range(n)] for _ in range(2)]
                ranges = [(low, high)] * n
                groups = kernels.group_pair(rows[0], rows[1], ranges)
                used = sorted(i for g in groups for i in g)
                self.assertEqual(used, [i for i in range(n) if rows[0][i] or rows[1][i]])
                for g in groups:
                    for row in rows:
                        steps = [kernels.step(row[i], low, high) for i in g]
                        self.assertGreaterEqual(sum(s[0] for s in steps), kernels.I16_MIN)
                        self.assertLessEqual(sum(s[1] for s in steps), kernels.I16_MAX)

    def test_pairs_cover_channels_once(self):
        rows = [[random.Random(c).randint(-40, 40) for _ in range(12)] for c in range(10)]
        pairs = kernels.pair_channels(rows, -128, 127)
        self.assertEqual(sorted(c for p in pairs for c in p), list(range(10)))


class Generated(unittest.TestCase):
    def test_every_pass_compiles(self):
        tools = [shutil.which(name) for name in ("glslangValidator", "spirv-val")]
        if not all(tools):
            raise unittest.SkipTest("glslang or SPIRV-Tools not installed")
        rng = random.Random(3)
        model = bytes(rng.getrandbits(8) for _ in range(131072))
        bindings = [(0, 18, "STORAGE_BUFFER", "WEIGHTS"), (1, 11, "STORAGE_BUFFER", "SCRATCH"),
                    (2, 0, "UNIFORM_BUFFER", "CONSTANTS")]
        with tempfile.TemporaryDirectory() as tmp:
            for index in kernels.PASSES:
                with self.subTest(dispatch=index):
                    source, groups, pairs = kernels.generate(index, model, bindings, table=32768)
                    self.assertGreater(groups, 0)
                    self.assertTrue(pairs and "model_words[32768u]" in source)
                    glsl, spv = Path(tmp) / f"pass{index}.comp", Path(tmp) / f"pass{index}.spv"
                    glsl.write_text(source)
                    subprocess.run([tools[0], "--target-env", "vulkan1.3", "-o", str(spv), str(glsl)],
                                   check=True, stdout=subprocess.DEVNULL)
                    subprocess.run([tools[1], "--target-env", "vulkan1.3", str(spv)], check=True)


if __name__ == "__main__":
    unittest.main()
