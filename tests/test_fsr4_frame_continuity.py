import copy
import subprocess
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from build_fsr4_frame import validate_resource_continuity


class CaptureContinuity(unittest.TestCase):
    def test_unrecorded_mutations_and_aliases_are_refused(self):
        def binding(resource, before, after):
            return dict(descriptor=dict(resource=resource), before_event=before, at_event=after)
        frames = [
            dict(srv=[binding("input", "initial", "initial")],
                 uav=[binding("history", "zero", "frame1")]),
            dict(srv=[binding("history", "frame1", "frame1")],
                 uav=[binding("output", "zero", "frame2")])]
        validate_resource_continuity(frames)
        changed = copy.deepcopy(frames)
        changed[1]["srv"][0]["at_event"] = "unrecorded clear"
        with self.assertRaisesRegex(ValueError, "changed between"):
            validate_resource_continuity(changed)
        changed = copy.deepcopy(frames)
        changed[1]["uav"] = [binding("history", "unrecorded clear", "frame2")]
        changed[1]["srv"] = []
        with self.assertRaisesRegex(ValueError, "changed between"):
            validate_resource_continuity(changed)
        changed[1]["srv"] = [binding("history", "frame1", "frame2")]
        with self.assertRaisesRegex(ValueError, "aliases"):
            validate_resource_continuity(changed)

    def test_frame_selection_rejected_before_loading_native_inputs(self):
        script = Path(__file__).resolve().parents[1] / "tools/build_fsr4_frame.py"
        for args, message in [
            (["--start-frame", "3", "--frames", "2"], "Selected frames exceed"),
            (["--start-frame", "1", "--dispatch", "29"], "cannot be combined")]:
            result = subprocess.run([sys.executable, str(script), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
