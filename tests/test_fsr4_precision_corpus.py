import struct
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from build_fsr4_precision_probe import corpus


class PrecisionCorpus(unittest.TestCase):
    def test_boundary_oracle(self):
        inputs, expected = corpus()
        self.assertEqual(len(inputs), len(expected))
        self.assertGreater(len(inputs) // 4, 190000)
        pairs = dict(zip(struct.unpack("<%dI" % (len(inputs)//4), inputs),
                         struct.unpack("<%dI" % (len(expected)//4), expected)))
        for bits, half in {0:0, 0x80000000:0x8000, 1:0, 0x337fffff:0,
                           0x33800000:1, 0x38800000:0x400,
                           0x477fe000:0x7bff, 0x47800000:0x7bff,
                           0x7f7fffff:0x7bff, 0x7f800000:0x7c00,
                           0xff800000:0xfc00, 0x7fc00000:0x7e00}.items():
            self.assertEqual(pairs[bits], half, hex(bits))

    def test_rte_ties_subnormals_and_overflow(self):
        inputs, expected = corpus("rte")
        pairs = dict(zip(struct.unpack("<%dI" % (len(inputs)//4), inputs),
                         struct.unpack("<%dI" % (len(expected)//4), expected)))
        for bits, half in {0x33000000:0, 0x33000001:1, 0x33c00000:2,
                           0x3f801000:0x3c00, 0x3f801001:0x3c01,
                           0x3f803000:0x3c02, 0x477fefff:0x7bff,
                           0x477ff000:0x7c00, 0xb3000000:0x8000,
                           0xb3000001:0x8001}.items():
            self.assertEqual(pairs[bits], half, hex(bits))
        with self.assertRaises(ValueError):
            corpus("unknown")


if __name__ == "__main__":
    unittest.main()
