import json
from pathlib import Path
import runpy
import socket
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zlib


TOOLS = Path(__file__).resolve().parents[1] / "tools"
REMOTE = runpy.run_path(str(TOOLS / "hmi-remote.py"))


def fake_qmp(path, ppm):
    """A one-connection QMP server whose screendump writes `ppm`."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def serve():
        connection, _ = server.accept()
        with connection, connection.makefile("rwb") as stream:
            stream.write(b'{"QMP": {"version": {}}}\r\n')
            stream.flush()
            for line in stream:
                request = json.loads(line)
                if request["execute"] == "screendump":
                    Path(request["arguments"]["filename"]).write_bytes(ppm)
                stream.write(b'{"event": "NOISE"}\r\n{"return": {}}\r\n')
                stream.flush()
        server.close()

    threading.Thread(target=serve, daemon=True).start()


class HmiRemoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.keys = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.keys.bind(str(self.root / "controller.sock"))
        self.keys.settimeout(2)

    def tearDown(self):
        self.keys.close()
        self.directory.cleanup()

    def received(self, count):
        return [self.keys.recv(4)[0] for _ in range(count)]

    def test_press_sends_one_code_per_button(self):
        controller = REMOTE["Controller"](self.root / "controller.sock")
        self.assertEqual(controller.press(["menu", "next", "OK", "back"], delay_ms=0), 4)
        self.assertEqual(self.received(4), [1, 13, 8, 2])

    def test_unknown_button_sends_nothing(self):
        controller = REMOTE["Controller"](self.root / "controller.sock")
        with self.assertRaisesRegex(ValueError, "unknown button"):
            controller.press(["menu", "launch"], delay_ms=0)
        self.keys.settimeout(0.1)
        with self.assertRaises(socket.timeout):
            self.keys.recv(4)

    def test_ppm_becomes_png(self):
        png = REMOTE["ppm_to_png"](b"P6\n# qemu\n2 1\n255\n" + bytes([255, 0, 0, 0, 0, 255]))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        idat = png.index(b"IDAT")
        length = int.from_bytes(png[idat - 4:idat], "big")
        self.assertEqual(zlib.decompress(png[idat + 4:idat + 4 + length]), b"\0" + bytes([255, 0, 0, 0, 0, 255]))

    def test_http_press_and_screen(self):
        fake_qmp(self.root / "qmp.sock", b"P6 1 1 255\n" + bytes([1, 2, 3]))
        server = REMOTE["ThreadingHTTPServer"](("127.0.0.1", 0), REMOTE["Handler"])
        server.controller = REMOTE["Controller"](self.root / "controller.sock")
        server.screen = REMOTE["Screen"](self.root / "qmp.sock", None, self.root)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = Request(base + "/api/v1/press", data=json.dumps({"keys": "media prev", "delay_ms": 0}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(json.load(response), {"pressed": 2})
            self.assertEqual(self.received(2), [4, 14])
            with urlopen(base + "/api/v1/screen.png", timeout=5) as response:
                self.assertEqual(response.headers["Content-Type"], "image/png")
                self.assertEqual(response.read()[:4], b"\x89PNG")
            self.assertEqual(list(self.root.glob("*.ppm")), [])
            bad = Request(base + "/api/v1/press", data=b'{"keys": ["boost"]}')
            with self.assertRaises(HTTPError) as caught:
                urlopen(bad, timeout=5)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
