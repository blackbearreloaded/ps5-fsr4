import copy
import hashlib
import json
import tempfile
import subprocess
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from build_fsr4_frame import validate_resource_continuity, select_dispatches, validate_shader_sequence, load_capture


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
            (["--start-frame", "1", "--dispatch", "29"], "cannot be combined")]:
            result = subprocess.run([sys.executable, str(script), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)

    def test_alternate_capture_identity_selection_and_shader_sequence(self):
        baseline = dict(dispatches=[dict(shader=dict(sha256=str(i), bytes=16))
                                    for i in range(28)] * 4)
        graph = copy.deepcopy(baseline)
        graph["dispatches"] = graph["dispatches"][:28]
        self.assertEqual(select_dispatches(graph, 0, 1), list(range(28)))
        self.assertEqual(select_dispatches(graph, 0, 1, start_pass=1), list(range(1, 28)))
        with self.assertRaisesRegex(ValueError, "Invalid start pass"):
            select_dispatches(graph, 0, 1, start_pass=28)
        with self.assertRaisesRegex(ValueError, "Selected frames exceed"):
            select_dispatches(graph, 0, 2)
        with self.assertRaisesRegex(ValueError, "not a captured image stage"):
            select_dispatches(graph, 0, 1, 28)
        validate_shader_sequence(graph, baseline)
        graph["dispatches"][5]["shader"]["sha256"] = "other"
        with self.assertRaisesRegex(ValueError, "sequence changed"):
            validate_shader_sequence(graph, baseline)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            raw = json.dumps(graph).encode()
            digest = hashlib.sha256(raw).hexdigest()
            (path / "graph.json").write_bytes(raw)
            (path / "complete.json").write_text(json.dumps(
                dict(graph_sha256=digest, dispatches=28)))
            self.assertEqual(load_capture(path, digest), graph)
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                load_capture(path, "wrong")
            (path / "complete.json").write_text(json.dumps(
                dict(graph_sha256=digest, dispatches=112)))
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                load_capture(path, digest)

    def test_diagnostic_image_extent_stays_separate_from_normal_profile(self):
        from fsr4_paths import DRIVER as root, VULKAN_HEADERS
        source = """
#include "texture_format.h"
#include <assert.h>
int main(void) {
    VkImageCreateInfo image = {
        .imageType=VK_IMAGE_TYPE_2D, .format=VK_FORMAT_R32G32B32A32_SFLOAT,
        .extent={240,144,1}, .mipLevels=1, .arrayLayers=1,
        .samples=VK_SAMPLE_COUNT_1_BIT, .tiling=VK_IMAGE_TILING_OPTIMAL,
        .usage=VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
               VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT};
#if PS5VK_EXTENDED_COMPUTE_DIAGNOSTIC
    assert(ps5vk_storage_image_info(&image));
    image.extent.width=3840; image.extent.height=2160;
    assert(ps5vk_storage_image_info(&image));
    image.extent.width=3841;
    assert(!ps5vk_storage_image_info(&image));
    image.extent.width=3840; image.extent.height=2161;
    assert(!ps5vk_storage_image_info(&image));
#else
    assert(!ps5vk_storage_image_info(&image));
#endif
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "extent.c").write_text(source)
            for enabled in (0, 1):
                subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                "-DPS5VK_EXTENDED_COMPUTE_DIAGNOSTIC=%d" % enabled,
                                "-I" + str(root / "src"),
                                "-I" + str(VULKAN_HEADERS),
                                str(root / "src/texture_format.c"), str(path / "extent.c"),
                                "-o", str(path / "extent")], check=True)
                subprocess.run([str(path / "extent")], check=True)


if __name__ == "__main__":
    unittest.main()
