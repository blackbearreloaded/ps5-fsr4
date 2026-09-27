import math
from pathlib import Path
import struct
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from compare_fsr4_image import compare


class ImageComparison(unittest.TestCase):
    def test_metrics_and_invalid_inputs(self):
        reference = struct.pack("<4f", 0, 0, 0, 1)
        actual = struct.pack("<4f", .25, 0, 0, .5)
        result = compare(actual, reference, 1, 1)
        self.assertFalse(result["byte_exact"])
        self.assertFalse(result["acceptance_defined"])
        self.assertEqual(result["rgb_max_absolute_error"], .25)
        self.assertAlmostEqual(result["rgb_rmse"], .25 / math.sqrt(3))
        self.assertEqual(result["alpha_max_absolute_error"], .5)
        exact = compare(reference, reference, 1, 1)
        self.assertTrue(exact["byte_exact"])
        self.assertEqual(exact["rgb_rmse"], 0)
        for bad in (b"", reference[:-1], struct.pack("<4f", math.nan, 0, 0, 1),
                    struct.pack("<4f", math.inf, 0, 0, 1)):
            with self.assertRaises(ValueError):
                compare(bad, reference, 1, 1)
        with self.assertRaises(ValueError):
            compare(reference, reference, 0, 1)


if __name__ == "__main__":
    unittest.main()
