import hashlib
import json
from pathlib import Path
import re
import runpy
import unittest


REPO = Path(__file__).resolve().parents[1]
MAPS = REPO / "tools" / "shaders" / "native-reconstructions"
PREPARE = runpy.run_path(str(REPO / "tools" / "prepare-hmi-native-essl-map.py"))


def pinned_fragment_map():
    script = (REPO / "run-emulator.sh").read_text()
    return MAPS / re.search(r"native-reconstructions/(hmi-fragment-map-v\d+\.json)", script).group(1)


class ShaderMapTests(unittest.TestCase):
    def resolved(self, path):
        return PREPARE["apply_map_delta"](json.loads(path.read_text()), REPO)

    def test_pinned_fragment_map_sources_match_their_hashes(self):
        mappings = self.resolved(pinned_fragment_map())["mappings"]
        self.assertTrue(mappings)
        for entry in mappings:
            source = REPO / entry["source_path"]
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), entry["source_essl_sha256"],
                             entry["asset_path"])

    def test_list_text_uses_the_adapted_fade_shader(self):
        mappings = {entry["asset_path"]: entry for entry in self.resolved(pinned_fragment_map())["mappings"]}
        fade = (REPO / mappings["common/shader/Text_fragment_fade.shd"]["source_path"]).read_text()
        self.assertIn("packHalf2x16(vec2(0.0, u_opacity))", fade)
        self.assertIn("packHalf2x16(vColor.xy)", fade)
        self.assertIn("pa[2].r", fade)
        self.assertNotIn("sa[2].y = uintBitsToFloat(0x00000000u)", fade)


if __name__ == "__main__":
    unittest.main()
