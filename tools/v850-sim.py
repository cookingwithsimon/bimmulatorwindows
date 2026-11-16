#!/usr/bin/env python3
"""Emulated V850 for the head unit: feeds car data to the HMI over MOST.

Connects to the QEMU UART socket that the guest's io-ipc V850 transport uses
(tools/qnx-v850-transport.c), answers the HMI's MOST subscriptions and serves
a small HTTP API:

  GET  /api/v1/vehicle   current vehicle state
  POST /api/v1/vehicle   partial update, same fields as tools/kombi-sim.py
  GET  /api/v1/v850      link, watchdog and MOST bridge state

With --follow URL the vehicle state is read from a running kombi-sim instead,
so one set of controls drives the 6WB cluster, the HUD and the head unit.
"""

import argparse
import importlib.util
import json
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v850_link import V850  # noqa: E402

_spec = importlib.util.spec_from_file_location("kombi_sim", Path(__file__).with_name("kombi-sim.py"))
kombi_sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kombi_sim)

MAX_BODY = 8192


class FollowedVehicle:
    """Polls a kombi-sim /api/v1/vehicle endpoint; keeps the last good state."""

    def __init__(self, url):
        self.url = url.rstrip("/") + "/api/v1/vehicle"
        self._state = dict(kombi_sim.INITIAL)
        self._lock = threading.Lock()
        threading.Thread(target=self._poll, daemon=True, name="v850-follow").start()

    def _poll(self):
        while True:
            try:
                with urlopen(self.url, timeout=2) as response:
                    state = json.load(response)
                with self._lock:
                    self._state = state
            except (OSError, ValueError):
                pass
            time.sleep(0.25)

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def update(self, _patch):
        raise ValueError("vehicle state follows kombi-sim; send updates there")


class Handler(BaseHTTPRequestHandler):
    def json(self, status, value):
        data = json.dumps(value, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/v1/vehicle":
            self.json(HTTPStatus.OK, self.server.vehicle.snapshot())
        elif self.path == "/api/v1/v850":
            self.json(HTTPStatus.OK, self.server.v850.snapshot())
        else:
            self.json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/v1/vehicle":
            self.json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("body must be 1 to 8192 bytes")
            self.json(HTTPStatus.OK, self.server.vehicle.update(json.loads(self.rfile.read(length))))
        except ValueError as error:
            self.json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("uart_socket", type=Path, help="QEMU unix socket of the V850 UART")
    parser.add_argument("--port", type=int, default=8770, help="HTTP port on 127.0.0.1 (0 picks one)")
    parser.add_argument("--follow", metavar="URL", help="read the vehicle state from this kombi-sim")
    parser.add_argument("--log", type=Path, help="write the V850 log here instead of stdout")
    parser.add_argument("--demo", action="store_true", help="generate a looping drive (ignored with --follow)")
    args = parser.parse_args()

    vehicle = FollowedVehicle(args.follow) if args.follow else kombi_sim.Vehicle()
    v850 = V850(args.uart_socket, vehicle.snapshot, log_path=args.log)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.vehicle, server.v850 = vehicle, v850
    threading.Thread(target=server.serve_forever, daemon=True, name="v850-http").start()
    stopped = threading.Event()
    if args.demo and not args.follow:
        threading.Thread(target=kombi_sim.demo, args=(vehicle, stopped), daemon=True, name="v850-demo").start()
    print(f"V850 simulator: http://127.0.0.1:{server.server_address[1]}/api/v1/v850", flush=True)
    try:
        v850.run()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        v850.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
