import hashlib
import json
from pathlib import Path
import re
import runpy
import unittest


REPO = Path(__file__).resolve().parents[1]
MAPS = REPO / "tools" / "shaders" / "native-reconstructions"
PREPARE = runpy.run_path(str(REPO / "tools" / "prepare-hmi-native-essl-map.py"))


def pinned_map(stage):
    script = (REPO / "run-emulator.sh").read_text()
    return MAPS / re.search(rf"native-reconstructions/(hmi-{stage}-map-v\d+\.json)", script).group(1)


class ShaderMapTests(unittest.TestCase):
    def resolved(self, path):
        data = json.loads(path.read_text())
        return PREPARE["apply_map_delta"](data, REPO) if "base_manifest_sha256" in data and "replacements" in data else data

    def test_pinned_map_sources_match_their_hashes(self):
        for stage in ("vertex", "fragment"):
            mappings = self.resolved(pinned_map(stage))["mappings"]
            self.assertTrue(mappings)
            for entry in mappings:
                source = REPO / entry["source_path"]
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), entry["source_essl_sha256"],
                                 entry["asset_path"])

    def test_profile_and_launcher_pin_the_same_maps(self):
        pinned = {pinned_map("vertex").name, pinned_map("fragment").name}
        for path in ("profiles/f32-x20511i-id6-light.json", "tools/idrive6-launch.py"):
            text = (REPO / path).read_text()
            found = set(re.findall(r"hmi-(?:vertex|fragment)-map-v\d+\.json", text))
            self.assertEqual(found, pinned, path)

    def test_fade_text_vertex_passes_the_written_uv(self):
        mappings = {entry["asset_path"]: entry for entry in self.resolved(pinned_map("vertex"))["mappings"]}
        vertex = (REPO / mappings["common/shader/Text_ColorFading_vertex_fadeVertical.shd"]["source_path"]).read_text()
        self.assertIn("vTexCoord = vec2(_output[2].w, _output[3].x);", vertex)

    def test_list_text_uses_the_adapted_fade_shader(self):
        mappings = {entry["asset_path"]: entry for entry in self.resolved(pinned_map("fragment"))["mappings"]}
        fade = (REPO / mappings["common/shader/Text_fragment_fade.shd"]["source_path"]).read_text()
        self.assertIn("packHalf2x16(vec2(0.0, u_opacity))", fade)
        self.assertIn("packHalf2x16(vColor.xy)", fade)
        self.assertIn("pa[2].r", fade)
        self.assertNotIn("sa[2].y = uintBitsToFloat(0x00000000u)", fade)


if __name__ == "__main__":
    unittest.main()
