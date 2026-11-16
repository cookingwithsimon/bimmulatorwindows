#!/usr/bin/env python3
"""Remote control for the emulated head unit: iDrive controller buttons and screenshots.

Presses go through the same native controller path as the keyboard in the QEMU
window (IDRIVE_HMI_CONTROLLER=1): one key code per datagram to the ANGLE
server's controller socket, which queues an authentic BMW controller event in
HmiMain3D. Screenshots come from QEMU's display over QMP.

  serve   page with buttons at http://127.0.0.1:8772/ plus an HTTP API:
            GET  /api/v1/buttons         button names
            POST /api/v1/press           {"keys": ["menu", "next", "push"], "delay_ms": 400}
            GET  /api/v1/screen.png      current head-unit screen
  press   press buttons from the command line:   hmi-remote.py press menu next push
  screen  save a screenshot:                     hmi-remote.py screen shot.png

Buttons: menu, back, options, media, comm, nav, map, push, up, down, left,
right (controller tilts), next and prev (turn the controller clockwise and
anticlockwise). Run-folder sockets are found automatically when --run is given.
"""

import argparse
import json
import socket
import struct
import threading
import time
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Key codes understood by the ANGLE server's controller ring; the same codes
# tools/idrive-cid-input.py sends for keys pressed in the QEMU window.
BUTTONS = {
    "menu": 1, "back": 2, "options": 3, "media": 4, "comm": 6, "nav": 7,
    "push": 8, "up": 9, "down": 10, "left": 11, "right": 12,
    "next": 13, "prev": 14, "map": 17,
}
ALIASES = {"select": "push", "enter": "push", "ok": "push", "cw": "next", "ccw": "prev",
           "radio": "media", "phone": "comm", "communication": "comm", "navigation": "nav",
           "option": "options", "escape": "back", "home": "menu"}
MAX_KEYS = 64
MAX_BODY = 8192


def button_code(name):
    key = str(name).strip().lower()
    key = ALIASES.get(key, key)
    if key not in BUTTONS:
        raise ValueError(f"unknown button {name!r}; use one of {', '.join(sorted(BUTTONS))}")
    return BUTTONS[key]


class Controller:
    def __init__(self, socket_path):
        self.path = str(socket_path)
        self.lock = threading.Lock()

    def press(self, keys, delay_ms=400):
        codes = [button_code(key) for key in keys]
        if not 1 <= len(codes) <= MAX_KEYS:
            raise ValueError(f"send 1 to {MAX_KEYS} buttons")
        if not 0 <= delay_ms <= 10000:
            raise ValueError("delay_ms must be 0 to 10000")
        with self.lock, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            for index, code in enumerate(codes):
                if index:
                    time.sleep(delay_ms / 1000)
                try:
                    sock.sendto(bytes((code,)), self.path)
                except OSError as error:
                    raise RuntimeError(f"controller socket {self.path}: {error}") from error
        return len(codes)


def qmp_screendump(socket_path, target, device=None, timeout=10):
    """Ask QEMU for a PPM screenshot of its display (the head unit's DSS scanout)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        stream = sock.makefile("rwb")

        def command(name, arguments=None):
            request = {"execute": name}
            if arguments:
                request["arguments"] = arguments
            stream.write((json.dumps(request) + "\r\n").encode())
            stream.flush()
            while True:
                line = stream.readline()
                if not line:
                    raise RuntimeError(f"QMP closed during {name}")
                reply = json.loads(line)
                if "error" in reply:
                    raise RuntimeError(f"QMP {name}: {reply['error'].get('desc', reply['error'])}")
                if "return" in reply:
                    return reply["return"]

        greeting = json.loads(stream.readline() or b"{}")
        if "QMP" not in greeting:
            raise RuntimeError("not a QMP socket")
        command("qmp_capabilities")
        arguments = {"filename": str(target)}
        if device:
            arguments["device"] = device
        command("screendump", arguments)


def ppm_to_png(data):
    """Binary PPM (P6, maxval 255) as QEMU writes it, to PNG."""
    fields, position = [], 0
    while len(fields) < 4:
        while data[position:position + 1].isspace():
            position += 1
        if data[position:position + 1] == b"#":
            position = data.index(b"\n", position) + 1
            continue
        end = position
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(data[position:end])
        position = end
    if fields[0] != b"P6" or fields[3] != b"255":
        raise RuntimeError("unsupported screenshot format")
    width, height = int(fields[1]), int(fields[2])
    pixels = data[position + 1:position + 1 + width * height * 3]
    if len(pixels) != width * height * 3:
        raise RuntimeError("short screenshot")
    row = width * 3
    raw = b"".join(b"\0" + pixels[y * row:(y + 1) * row] for y in range(height))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class Screen:
    def __init__(self, qmp_socket, device=None, scratch=None):
        self.qmp_socket = qmp_socket
        self.device = device
        self.scratch = Path(scratch or "/tmp")
        self.lock = threading.Lock()

    def png(self):
        if not self.qmp_socket:
            raise RuntimeError("no QMP socket configured (--qmp-socket or --run)")
        with self.lock:
            target = self.scratch / f"hmi-remote-{threading.get_ident()}.ppm"
            try:
                qmp_screendump(self.qmp_socket, target.resolve(), self.device)
                return ppm_to_png(target.read_bytes())
            finally:
                target.unlink(missing_ok=True)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>iDrive remote</title>
<style>
:root{--bg:#121417;--panel:#1d2025;--text:#e3e7ec;--muted:#8c949e;--key:#2a2f37;--edge:#3b424c;--accent:#4aa8ff}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,sans-serif;padding:16px}
h1{font-size:16px;margin:0 0 12px}
.wrap{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
#screen{max-width:100%;width:960px;background:#000;border:1px solid var(--edge);border-radius:6px;display:block}
.pad{background:var(--panel);border-radius:10px;padding:12px;display:grid;gap:8px;grid-template-columns:repeat(3,72px)}
button{background:var(--key);color:var(--text);border:1px solid var(--edge);border-radius:8px;height:44px;font:inherit;cursor:pointer}
button:hover{border-color:var(--accent)} button:active{background:#343b45}
.wide{grid-column:span 3}.push{border-radius:50%;width:56px;height:56px;justify-self:center;background:#333a44}
.muted{color:var(--muted);font-size:12px} #status{min-height:1.4em}
</style></head><body>
<h1>iDrive remote</h1>
<div class="wrap">
 <div><img id="screen" alt="Head-unit screen"><p class="muted" id="status"></p></div>
 <div class="pad">
  <button data-k="menu">Menu</button><button data-k="back">Back</button><button data-k="options">Option</button>
  <button data-k="media">Media</button><button data-k="comm">Comm</button><button data-k="nav">Nav</button>
  <span></span><button data-k="up" title="Tilt up">&#9650;</button><span></span>
  <button data-k="left" title="Tilt left">&#9664;</button><button class="push" data-k="push" title="Push">OK</button><button data-k="right" title="Tilt right">&#9654;</button>
  <span></span><button data-k="down" title="Tilt down">&#9660;</button><span></span>
  <button data-k="prev" title="Turn anticlockwise">&#8634; Turn</button><button data-k="map">Map</button><button data-k="next" title="Turn clockwise">Turn &#8635;</button>
  <p class="muted wide">Keys: Page Up/Down turn, arrows tilt, Enter push, Esc back, M menu, O option, R media, T comm, N nav, V map.</p>
 </div>
</div>
<script>
const img=document.getElementById("screen"),status=document.getElementById("status");
let busy=false;
function refresh(){ if(busy) return; busy=true; const next=new Image();
  next.onload=()=>{img.src=next.src;busy=false;status.textContent="Updated "+new Date().toLocaleTimeString();};
  next.onerror=()=>{busy=false;status.textContent="No screenshot yet (is the emulator running?)";};
  next.src="api/v1/screen.png?t="+Date.now(); }
async function press(k){ const r=await fetch("api/v1/press",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({keys:[k]})});
  if(!r.ok) status.textContent=(await r.json()).error; setTimeout(refresh,500); }
document.querySelectorAll("button[data-k]").forEach(b=>b.onclick=()=>press(b.dataset.k));
const keys={PageDown:"next",PageUp:"prev",ArrowUp:"up",ArrowDown:"down",ArrowLeft:"left",ArrowRight:"right",Enter:"push",Escape:"back",Backspace:"back",m:"menu",o:"options",r:"media",c:"media",t:"comm",n:"nav",v:"map"};
addEventListener("keydown",e=>{const k=keys[e.key]||keys[e.key.toLowerCase()]; if(k){e.preventDefault();press(k);}});
refresh(); setInterval(refresh,1500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def send(self, status, kind, data):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def json(self, status, value):
        self.send(status, "application/json", json.dumps(value).encode())

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send(HTTPStatus.OK, "text/html; charset=utf-8", PAGE.encode())
        elif path == "/api/v1/buttons":
            self.json(HTTPStatus.OK, {"buttons": sorted(BUTTONS), "aliases": ALIASES})
        elif path == "/api/v1/screen.png":
            try:
                self.send(HTTPStatus.OK, "image/png", self.server.screen.png())
            except (OSError, RuntimeError, ValueError) as error:
                self.json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        else:
            self.json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/v1/press":
            self.json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError(f"body must be 1 to {MAX_BODY} bytes")
            body = json.loads(self.rfile.read(length))
            keys = body.get("keys") if isinstance(body, dict) else None
            if isinstance(keys, str):
                keys = keys.split()
            if not isinstance(keys, list):
                raise ValueError('send {"keys": ["menu", "next", "push"]}')
            delay = body.get("delay_ms", 400)
            if not isinstance(delay, int) or isinstance(delay, bool):
                raise ValueError("delay_ms must be a whole number")
            sent = self.server.controller.press(keys, delay)
            self.json(HTTPStatus.OK, {"pressed": sent})
        except ValueError as error:
            self.json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            self.json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def log_message(self, *_args):
        pass


def resolve(args):
    controller = args.controller_socket
    qmp = args.qmp_socket
    if args.run:
        qmp = qmp or args.run / "qmp.sock"
    if controller is None:
        raise SystemExit("--controller-socket is required (the launcher passes it)")
    return controller, qmp


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--controller-socket", type=Path, help="ANGLE server controller socket")
    parser.add_argument("--qmp-socket", type=Path, help="head-unit QEMU QMP socket")
    parser.add_argument("--run", type=Path, help="run folder holding qmp.sock")
    parser.add_argument("--device", help="QEMU display device id for screenshots (default: first display)")
    modes = parser.add_subparsers(dest="mode", required=True)
    serve = modes.add_parser("serve", help="serve the button page and HTTP API")
    serve.add_argument("--port", type=int, default=8772, help="HTTP port on 127.0.0.1 (0 picks one)")
    press = modes.add_parser("press", help="press buttons")
    press.add_argument("keys", nargs="+")
    press.add_argument("--delay-ms", type=int, default=400)
    shot = modes.add_parser("screen", help="save a PNG screenshot")
    shot.add_argument("output", type=Path)
    args = parser.parse_args()

    if args.mode == "screen":
        qmp = args.qmp_socket or (args.run / "qmp.sock" if args.run else None)
        args.output.write_bytes(Screen(qmp, args.device, args.output.resolve().parent).png())
        return 0
    controller, qmp = resolve(args)
    if args.mode == "press":
        Controller(controller).press(args.keys, args.delay_ms)
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.controller = Controller(controller)
    server.screen = Screen(qmp, args.device, controller.parent)
    print(f"iDrive remote: http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
