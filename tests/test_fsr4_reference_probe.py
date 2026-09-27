# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
import struct
import subprocess
import sys
from pathlib import Path
import unittest
from tools.fsr4_reference_probe import check_output


class ReferenceProbe(unittest.TestCase):
    def test_reject_fallback_missing_completion_and_invalid_readback(self):
        lines = ["provider: status=0 id=17700776140664213505 name=4.1.1r11",
                 "readback: 192x144 RGBA32F, 4 completed frames", "destroy: status=0"]
        lines += [f"completed: frame={i} fence={i+1}" for i in range(4)]
        lines += [f"dispatch: frame={i} status=0" for i in range(4)]
        log = "\n".join(lines)
        pixels = struct.pack("<f", 0.25) * (192 * 144 * 4)
        self.assertEqual(check_output(log, pixels)["finite_values"], 110592)
        for bad in (log.replace("4.1.1r11", "3.1.5"),
                    log.replace("completed: frame=3 fence=4", "")):
            with self.assertRaises(ValueError):
                check_output(bad, pixels)
        for bad in (pixels[:-4], struct.pack("<f", float("nan")) + pixels[4:]):
            with self.assertRaises(ValueError):
                check_output(log, bad)


    def test_requested_size_and_frame_count_are_enforced(self):
        lines = ["provider: status=0 id=17700776140664213505 name=4.1.1r11",
                 "readback: 240x144 RGBA32F, 5 completed frames", "destroy: status=0"]
        lines += [f"completed: frame={i} fence={i+1}" for i in range(5)]
        lines += [f"dispatch: frame={i} status=0" for i in range(5)]
        pixels = struct.pack("<f", 0.5) * (240 * 144 * 4)
        log = "\n".join(lines)
        self.assertEqual(check_output(log, pixels, 240, 144, 5)["bytes"], len(pixels))
        for width, height, frames in [(192, 144, 5), (240, 144, 4), (0, 144, 5),
                                      (3841, 144, 5), (240, 144, 601)]:
            with self.assertRaises(ValueError):
                check_output(log, pixels, width, height, frames)


    def test_cli_rejects_invalid_workloads_before_accessing_dependencies(self):
        runner = Path(__file__).resolve().parents[1] / "tools/fsr4_reference_probe.py"
        for args in [("--render-size", "0", "96"), ("--frames", "601"),
                     ("--output-size", "64", "64")]:
            result = subprocess.run([sys.executable, str(runner), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
