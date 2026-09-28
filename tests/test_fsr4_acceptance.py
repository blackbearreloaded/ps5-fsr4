from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from check_fsr4_acceptance import evaluate, parse

BEGIN = "FSR4_RT_BEGIN fixture=ab scenario=motion frames=2 render=128x96 output=192x144 heap=0 output=/x\n"


def run(psnrs, nonfinite=0, end="COMPLETE", begin=BEGIN):
    frames = "".join(f"FSR4_RT_FRAME frame={i} reset={int(not i)} gpu_ms=1 max=0.1 rmse=0.01 psnr={p} "
                     f"nonfinite={nonfinite} differing=1\n" for i, p in enumerate(psnrs))
    return parse(begin + frames + f"FSR4_RT_END result={end}\n")


class Acceptance(unittest.TestCase):
    def test_device_at_least_as_close_as_control(self):
        accepted, lines = evaluate(run([60.0, 40.1]), run([55.0, 40.5]))
        self.assertTrue(accepted, lines)

    def test_frame_worse_than_margin_rejects(self):
        self.assertFalse(evaluate(run([60.0, 38.9]), run([55.0, 40.0]))[0])

    def test_nonfinite_or_incomplete_rejects(self):
        self.assertFalse(evaluate(run([60.0, 45.0], nonfinite=1), run([55.0, 40.0]))[0])
        self.assertFalse(evaluate(run([60.0, 45.0], end="FAIL"), run([55.0, 40.0]))[0])
        self.assertFalse(evaluate(run([60.0, 45.0]), run([55.0, 40.0], end="FAIL"))[0])

    def test_different_fixture_or_frames_rejects(self):
        other = BEGIN.replace("fixture=ab", "fixture=cd")
        self.assertFalse(evaluate(run([60.0, 45.0], begin=other), run([55.0, 40.0]))[0])
        self.assertFalse(evaluate(run([60.0]), run([55.0, 40.0]))[0])


if __name__ == "__main__":
    unittest.main()
