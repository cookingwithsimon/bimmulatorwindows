from pathlib import Path
import runpy
import struct
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
ARCHIVER = runpy.run_path(str(TOOLS / "archive-idrive-rootfs.py"))
overlay = ARCHIVER["_hmi_coding_remote_hmi_overlay"]
OFFSET, EXPECTED, REPLACEMENT = ARCHIVER["HMI_REMOTE_HMI_PATCH"]
INDEX_ENTRY = 0xDB548 + (7 - 1) * 12


def library(index=(7, 0x96, 0xDB2C0), coded=b"\x00") -> bytes:
    data = bytearray(0xDB548 + 16 * 12)
    struct.pack_into("<III", data, INDEX_ENTRY, *index)
    data[OFFSET : OFFSET + 1] = coded
    return bytes(data)


class RemoteHmiOverlayTests(unittest.TestCase):
    def test_patch_sits_in_page_0x3000_byte_0x6b(self):
        self.assertEqual(OFFSET, 0xDB2C0 + 0x6B)
        # REMOTE_HMI is mask 0x70, value 1; A4A (mask 0x80) must stay clear.
        self.assertEqual((REPLACEMENT[0] & 0x70) >> 4, 1)
        self.assertEqual(REPLACEMENT[0] & 0x8F, 0)

    def test_sets_only_the_remote_hmi_field(self):
        source = library()
        patched = overlay(source)
        self.assertEqual(patched[OFFSET], 0x10)
        self.assertEqual(
            [index for index in range(len(source)) if source[index] != patched[index]],
            [OFFSET],
        )

    def test_rejects_another_firmware_layout(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected BMW default page index"):
            overlay(library(index=(7, 0x96, 0xDB2C4)))
        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            overlay(library(coded=b"\x80"))


if __name__ == "__main__":
    unittest.main()
