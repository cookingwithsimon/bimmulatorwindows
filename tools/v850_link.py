"""Host side of the emulated V850: io-ipc framing, channel model, MOST bridge.

Derived from IDEmu's idemu/v850.py and idemu/most.py
(https://github.com/herrbimmer/IDEmu).
Required Notice: Copyright 2026 IDEmu contributors
Licensed under the PolyForm Noncommercial License 1.0.0
(https://polyformproject.org/licenses/noncommercial/1.0.0); see
licenses/PolyForm-Noncommercial-1.0.0.txt. Changes for bimmulator: trimmed to
the io-ipc link, the watchdog and flow-control channels and the channel-25
MOST bridge; vehicle values come from a kombi-sim style state dictionary;
no snapshot export, navigation, map or phone devices.

On the car the V850 is the head unit's power and vehicle-bus controller. It
reaches the OMAP through io-ipc channels; channel 25 is the DSI bridge of the
stock `most` daemon, and the V850 registers its MOST function blocks there.
That is how car data (speed, gear, trip computer, range) reaches the HMI.
The guest side is tools/qnx-v850-transport.c.

All MOST layouts below were read from HmiMain3D 007_051_002 by the IDEmu
authors. The emulator runs 007_030_001, so treat each one as unconfirmed
until the HMI is seen to accept it.
"""

from collections import OrderedDict, deque
import json
from pathlib import Path
import socket
import struct
import threading
import time


HELLO, TX, RX, DEBUG = 0x00, 0x01, 0x02, 0x10
MAX_FRAME = 0x1000
DEFAULTS_FILE = Path(__file__).with_name("v850-most-defaults.json")


def encode_frame(frame_type, data, checksum):
    data = bytes(data)
    if checksum:
        data += struct.pack("<H", sum(data) & 0xFFFF)
    return b"\xa5\x5a" + bytes([frame_type]) + struct.pack("<H", len(data)) + data


class FrameParser:
    """Splits the guest's UART byte stream into (type, payload) frames."""

    def __init__(self):
        self.buffer = b""
        self.skipped = 0
        self.junk = bytearray()  # the first bytes that were not frames, for the log

    def _skip(self, data):
        self.skipped += len(data)
        self.junk += data[:max(0, 2048 - len(self.junk))]

    def feed(self, data):
        self.buffer += data
        frames = []
        while True:
            start = self.buffer.find(b"\xa5\x5a")
            if start < 0:
                self._skip(self.buffer[:-1] if self.buffer.endswith(b"\xa5") else self.buffer)
                self.buffer = self.buffer[-1:] if self.buffer.endswith(b"\xa5") else b""
                return frames
            self._skip(self.buffer[:start])
            if len(self.buffer) < start + 5:
                self.buffer = self.buffer[start:]
                return frames
            length = self.buffer[start + 3] | (self.buffer[start + 4] << 8)
            if length > MAX_FRAME + 0x200:
                self._skip(self.buffer[start:start + 2])
                self.buffer = self.buffer[start + 2:]
                continue
            if len(self.buffer) < start + 5 + length:
                self.buffer = self.buffer[start:]
                return frames
            frames.append((self.buffer[start + 2], self.buffer[start + 5:start + 5 + length]))
            self.buffer = self.buffer[start + 5 + length:]


def ioipc_header(length, sequence, channel, toggle, flags=0):
    return struct.pack("<H", (length << 4) | (sequence & 15)) + bytes([channel, (toggle << 7) | flags])


def parse_ioipc(payload):
    """(channel, flags, sequence, data) of an io-ipc message from the guest."""
    if len(payload) < 4:
        raise ValueError("short io-ipc message")
    word = payload[0] | (payload[1] << 8)
    length, sequence, channel, flags = word >> 4, word & 15, payload[2], payload[3]
    header = 8 if flags & 0x40 else 4
    return channel, flags, sequence, bytes(payload[header:header + length])


class MostMessage:
    """DSYSMOSTTypes::MessageType as the channel-25 bridge serializes it
    (CHBByteStream, little-endian, unaligned):
    i32 transmission | i32 variant | u16 destination | u16 source | u8 FBlock |
    u8 instance | u16 function | u8 operation | u32 1 | u32 n | n data bytes."""

    OP_SET, OP_GET, OP_SETGET, OP_STATUS = 0, 1, 2, 0x0C
    _HEAD = struct.Struct("<iiHHBBHBII")

    def __init__(self, dst, src, fblock, inst, func, op, data=b"", transmission=2, variant=0):
        self.dst, self.src, self.fblock, self.inst = dst, src, fblock, inst
        self.func, self.op, self.data = func, op, bytes(data)
        self.transmission, self.variant = transmission, variant

    def pack(self):
        return self._HEAD.pack(self.transmission, self.variant, self.dst, self.src, self.fblock,
                               self.inst, self.func, self.op, 1, len(self.data)) + self.data

    @classmethod
    def parse(cls, payload, offset=0):
        fields = cls._HEAD.unpack_from(payload, offset)
        start = offset + cls._HEAD.size
        transmission, variant, dst, src, fblock, inst, func, op, _one, length = fields
        return cls(dst, src, fblock, inst, func, op, payload[start:start + length], transmission, variant)

    def __repr__(self):
        return (f"MOST {self.fblock:02X}.{self.inst} fkt {self.func:03x} op {self.op} "
                f"{self.src:04x}->{self.dst:04x} {self.data.hex()}")


GEARS = {"P": 0, "R": 1, "N": 2, "D": 3, "M": 3}


class Car:
    """Builds MOST Status payloads from a kombi-sim style vehicle state."""

    KOMBI = (0xE0, 0)
    VEHICLE = (0x05, 0)
    POWER = (0xD1, 2)

    def __init__(self, state, defaults=None):
        self.state = state                  # callable returning the vehicle dictionary
        self.defaults = defaults or {}
        self.avg_speed = 0.0
        self.avg_consumption = 0.0
        self.trip_start = time.time()
        self.odometer_km = 0.0
        self._last = time.monotonic()

    def value(self, name, default=0):
        value = self.state().get(name, default)
        return default if value is None else value

    def tick(self):
        now = time.monotonic()
        elapsed, self._last = now - self._last, now
        speed = float(self.value("speed_kph"))
        self.odometer_km += speed * elapsed / 3600.0
        self.avg_speed += 0.05 * (speed - self.avg_speed)
        self.avg_consumption += 0.05 * (self.consumption() - self.avg_consumption)

    def consumption(self):
        rpm = float(self.value("engine_rpm"))
        return 0.0 if not self.value("ignition", True) else 4.0 + rpm / 1000.0

    def range_km(self):
        litres = float(self.value("fuel_pct")) * 0.65   # G01 tank: 65 l
        return int(litres / max(4.0, self.avg_consumption or 7.5) * 100.0)

    def kombi(self, fkt):
        if fkt == 0x509:      # CBordcomputerAverage: average speed (0.1 km/h) and consumption (0.1 l/100 km)
            speed = max(0, min(4095, round(self.avg_speed * 10)))
            consumption = max(0, min(4095, round(self.avg_consumption * 10)))
            return struct.pack(">HHHHHBB", 0, speed, speed, consumption, consumption, 1, 1)
        if fkt == 0x504:      # CKombiExternalDisplay: range in km (0xFFD invalid)
            return struct.pack(">HHHB", 0, max(0, min(0xFA0, self.range_km())), 0, 0)
        if fkt == 0x512:      # CRangeTankStatus: state 0 and range in km
            tank = max(0, min(255, round(float(self.value("fuel_pct")) * 0.65)))
            return struct.pack(">HBBBBBH", 0, 0, tank, 0, 0, 0, max(0, min(0xFFE, self.range_km())))
        if fkt == 0x510:      # CBordcomputerTravelStart: year, day, hour, minute, month
            t = time.localtime(self.trip_start)
            return struct.pack(">HHBBBB", 0, t.tm_year, t.tm_mday, t.tm_hour, t.tm_min, t.tm_mon)
        if fkt == 0x511:      # CBordcomputerTravelData: hours, minutes, distance in km
            elapsed = max(0, int(time.time() - self.trip_start))
            distance = max(0, min(0xFFFC, int(self.odometer_km)))
            return struct.pack(">HHBHh", 0, min(2399, elapsed // 3600), elapsed // 60 % 60, distance, 0)
        return None

    def vehicle(self, fkt):
        gear = GEARS.get(str(self.value("gear", "P")), 3)
        ignition = bool(self.value("ignition", True))
        if fkt == 0x621:      # CCarSpeed2: u16 in 0.01 km/h (unconfirmed)
            return struct.pack(">H", max(0, min(0xFFFF, round(float(self.value("speed_kph")) * 100))))
        if fkt in (0x228, 0xCC2):   # CGearBox, CGearIndicator
            return bytes((gear,))
        if fkt == 0x22C:      # CReverseGear
            return bytes((1 if gear == 1 else 0,))
        if fkt == 0x21F:      # CTerminalStatus: 12 = terminal 15, 6 = off
            return bytes((12, 1, 0)) if ignition else bytes((6, 0, 0))
        if fkt == 0x224:      # CIgnitionKey: key present
            return struct.pack(">HBBBB", 1, 1, 1, 1, 1)
        if fkt == 0xC01:      # CTyresStatus: tyre pressure monitor active
            return bytes((0, 0, 0xA0, 0, 0))
        return None

    def status_for(self, fblock, inst, fkt):
        key = (fblock, inst)
        data = None
        if key == self.KOMBI:
            data = self.kombi(fkt)
        elif key == self.VEHICLE:
            data = self.vehicle(fkt)
        elif key == self.POWER:
            on = bool(self.value("ignition", True))
            if fkt == 0xC10:  # CSyncMarker: bit 4 of byte 3 = key in car
                data = bytes((0, 0, 0, 0x10))
            elif fkt == 0x430:   # power state: 3 = PM_fullon_useron
                data = bytes((3 if on else 0,))
            elif fkt == 0x434:   # CPowerStateDetailed: 0x63 = PMD_Fullon_useron
                data = bytes((0x63 if on else 0,))
        if data is None:
            neutral = self.defaults.get(f"{fblock:02x}:{inst}:{fkt:03x}")
            data = bytes.fromhex(neutral) if neutral else None
        return data


class MostBridge:
    """io-ipc channel 25: the `most` daemon's DSI bridge (CDSYSMOSTServerDSIIPCBridgeStub).

    Message = 20-byte header + payload: [0] u8 1, [2] u16 interface key 0x04f1,
    [4] major 4, [5] minor 5, [6] u16 message id, [8] u16 total length,
    [0x10] u16 client key. Ids from the V850: 2 register devices, 3 register
    device controllers, 4 process message, 5 register MOST client. Ids from
    `most`: 0x8000 client registered, 0x8001 MOST telegram."""

    KEY, VERSION = 0x04F1, (4, 5)
    DEVICE_CONTROLLER = 0x0200
    # Kombi, Vehicle, power mode and the blocks with validated neutral Status.
    DEVICES = ((0xE0, 0), (0x05, 0), (0xD1, 2), (0xD1, 4), (0xD1, 16), (0xD1, 17), (0xE5, 1),
               (0xE5, 3), (0xE5, 15), (0xC0, 3), (0x03, 0), (0x71, 0), (0x15, 2), (0x15, 3),
               (0x15, 12), (0xE1, 0))
    RATE, BURST = 30.0, 4          # io-ipc's receive queue is finite: pace host messages
    RETRY_ACK_S = 30.0

    def __init__(self, send, car, log):
        self._send_ioipc, self.car, self.log = send, car, log
        self.client, self.device_id = 2, 0x2002
        self.hello = False
        self.subscriptions = {}    # (fblock, inst) -> {fkt: listener device}
        self.sent = {}
        self.seen = {}             # what the HMI Set/Get to our blocks (cluster, HUD): (fb, inst, fkt) -> hex
        self.counts = {}
        self._acked = {}
        self._pending = OrderedDict()
        self._sequence = 0
        self._condition = threading.Condition()
        self._closed = False
        self.stats = {"rx": 0, "tx": 0}

    # ---- outgoing ---------------------------------------------------------------------------
    def message(self, message_id, payload=b""):
        header = bytearray(20)
        header[0] = 1
        struct.pack_into("<H", header, 2, self.KEY)
        header[4], header[5] = self.VERSION
        struct.pack_into("<H", header, 6, message_id)
        struct.pack_into("<H", header, 8, 20 + len(payload))
        struct.pack_into("<H", header, 0x10, 0xFFFF)
        return bytes(header) + bytes(payload)

    def send(self, message_id, payload=b"", coalesce=None):
        self.stats["tx"] += 1
        frame = self.message(message_id, payload)
        with self._condition:
            if coalesce is None:
                self._sequence += 1
                coalesce = ("seq", self._sequence)
            self._pending[coalesce] = frame   # a newer Status replaces one still queued
            self._condition.notify()

    def send_most(self, message):
        key = None
        if message.op == MostMessage.OP_STATUS:
            key = (message.fblock, message.inst, message.func, message.data if message.func == 1 else None)
        self.send(4, struct.pack("<I", self.client) + message.pack(), coalesce=key)

    def publish(self, fblock, inst, fkt, data):
        listener = self.subscriptions.get((fblock, inst), {}).get(fkt)
        if listener is None:
            return 0
        self.send_most(MostMessage(listener, self.device_id, fblock, inst, fkt, MostMessage.OP_STATUS, data))
        return 1

    def register_devices(self, devices):
        keys = [(fblock << 8) | inst for fblock, inst in devices]
        for start in range(0, len(keys), 40):
            chunk = keys[start:start + 40]
            self.send(2, struct.pack("<IHI", self.client, 0, len(chunk)) + b"".join(struct.pack("<H", k) for k in chunk))

    def pump(self):
        tokens, last = float(self.BURST), time.monotonic()
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
            now = time.monotonic()
            tokens = min(float(self.BURST), tokens + (now - last) * self.RATE)
            last = now
            if tokens < 1.0:
                time.sleep((1.0 - tokens) / self.RATE)
                continue
            tokens -= 1.0
            with self._condition:
                if not self._pending:
                    continue
                _, frame = self._pending.popitem(last=False)
            self._send_ioipc(25, frame)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    # ---- incoming ---------------------------------------------------------------------------
    def on_message(self, payload):
        self.stats["rx"] += 1
        if len(payload) < 20:
            self.log(f"bridge: short message {payload.hex()}")
            return
        message_id = struct.unpack_from("<H", payload, 6)[0]
        body = payload[20:]
        if not self.hello:
            self.hello = True
            self.log("bridge: most is up; registering the V850 as a MOST client")
            self.send(5)
        if message_id == 0x8000 and len(body) >= 6:
            self.client, self.device_id = struct.unpack_from("<IH", body)
            self.log(f"bridge: MOST client {self.client}, device {self.device_id:#06x}")
            self.send(3, struct.pack("<IHI", self.client, 0, 1) + struct.pack("<H", self.DEVICE_CONTROLLER))
            self.register_devices(self.DEVICES)
            return
        if message_id == 0x8001 and len(body) >= 29:
            try:
                self.on_most(MostMessage.parse(body, 4))
            except struct.error as error:
                self.log(f"bridge: unreadable telegram {error}")
            return
        self.log(f"bridge: id {message_id:#x} {body[:48].hex()}")

    def on_most(self, message):
        key = (message.fblock, message.inst, message.func, message.op)
        self.counts[key] = self.counts.get(key, 0) + 1
        if message.func != 1:
            self.seen[(message.fblock, message.inst, message.func)] = {"op": message.op, "data": message.data.hex()}
        if message.func == 1 and message.op == MostMessage.OP_SET and len(message.data) == 5 and message.data[0] == 1:
            # Set Notification: 01 | listener device (BE) | function (BE)
            listener, fkt = struct.unpack(">HH", message.data[1:5])
            self.subscriptions.setdefault((message.fblock, message.inst), {})[fkt] = listener
            data = self.car.status_for(message.fblock, message.inst, fkt)
            now, ack_key = time.monotonic(), (message.fblock, message.inst, fkt)
            # The HMI repeats a subscription until it gets a Status. Unknown
            # properties are only acknowledged now and then, or the retries
            # flood channel 25.
            if data is not None or now - self._acked.get(ack_key, -self.RETRY_ACK_S) >= self.RETRY_ACK_S:
                self._acked[ack_key] = now
                self.send_most(MostMessage(message.src, message.dst, message.fblock, message.inst, 1,
                                           MostMessage.OP_STATUS, message.data))
            if data is not None:
                self.publish(message.fblock, message.inst, fkt, data)
                self.sent[ack_key] = data
        elif self.counts[key] <= 2:
            self.log(f"bridge: {message!r}")

    def publish_changes(self):
        published = 0
        for (fblock, inst), functions in list(self.subscriptions.items()):
            for fkt in list(functions):
                data = self.car.status_for(fblock, inst, fkt)
                if data is not None and self.sent.get((fblock, inst, fkt)) != data:
                    self.sent[(fblock, inst, fkt)] = data
                    published += self.publish(fblock, inst, fkt, data)
        return published

    def snapshot(self):
        return {"most_up": self.hello, "client": self.client, "rx": self.stats["rx"], "tx": self.stats["tx"],
                "queued": len(self._pending),
                "subscriptions": {f"{fb:02X}.{inst}": sorted(f"{f:03x}" for f in fns)
                                  for (fb, inst), fns in sorted(self.subscriptions.items())},
                "from_hmi": {f"{fb:02X}.{inst}.{fkt:03x}": v for (fb, inst, fkt), v in sorted(self.seen.items())}}


class V850:
    """The emulated V850 behind the guest's io-ipc, over one QEMU UART socket."""

    def __init__(self, uart_socket, state, log_path=None, defaults_file=DEFAULTS_FILE):
        self.uart_socket = uart_socket
        defaults = json.loads(Path(defaults_file).read_text())["defaults"] if defaults_file else {}
        self.car = Car(state, defaults)
        self.bridge = MostBridge(self.send, self.car, self.log)
        self.parser = FrameParser()
        self.sock = None
        self.sequence = 0
        self.toggle = 0
        self.heartbeats = 0
        self.channels = {}
        self.connected = False
        self.guest_log = deque(maxlen=50)
        self._write_lock = threading.Lock()
        self._number_lock = threading.Lock()
        self._log = open(log_path, "a", buffering=1) if log_path else None
        self._t0 = time.monotonic()
        self._stop = threading.Event()

    def log(self, text):
        line = f"{time.monotonic() - self._t0:8.2f} {text}"
        if self._log:
            self._log.write(line + "\n")
        else:
            print(line, flush=True)

    def count(self, direction, channel):
        entry = self.channels.setdefault(channel, {"from_guest": 0, "to_guest": 0})
        entry[direction] += 1

    # ---- link ---------------------------------------------------------------------------
    def write_frame(self, frame_type, data):
        frame = encode_frame(frame_type, data, checksum=True)
        with self._write_lock:
            if self.sock is None:
                return
            try:
                self.sock.sendall(frame)
            except OSError as error:
                self.log(f"write failed: {error}")

    def send(self, channel, payload, flags=0):
        payload = bytes(payload)
        # Number and write under one lock, or two threads can reach the guest
        # out of sequence and io-ipc rejects the later message.
        with self._number_lock:
            header = ioipc_header(len(payload), self.sequence, channel, self.toggle, flags)
            self.sequence = (self.sequence + 1) & 15
            self.toggle ^= 1
            self.count("to_guest", channel)
            self.write_frame(RX, header + payload)

    def on_frame(self, frame_type, payload):
        if frame_type == DEBUG:
            text = payload.decode("latin-1", "replace")
            self.guest_log.append(text)
            self.log("guest: " + text)
        elif frame_type == HELLO:
            self.log(f"guest transport hello ({len(payload)}-byte tchannel)")
        elif frame_type == TX:
            channel, flags, _sequence, data = parse_ioipc(payload)
            self.on_message(channel, data)

    def on_message(self, channel, data):
        self.count("from_guest", channel)
        if channel == 0 and data[:1] == b"\x10" and len(data) >= 2:
            # io-ipc flow control: [0x10][n]{channel, credits, flag, 0}*n. Grant every channel.
            entries = [data[2 + 4 * i:6 + 4 * i] for i in range(data[1])]
            self.send(0, bytes([0x10, data[1]]) + b"".join(bytes([e[0], 0, 1, 0]) for e in entries if len(e) == 4))
        elif channel == 25:
            self.bridge.on_message(data)
        elif channel == 26 and data[:1] == b"\x01":
            self.heartbeats += 1             # watchdog5-omap heartbeat; the real V850 resets the HU without it
            if self.heartbeats <= 3:
                self.log(f"watchdog heartbeat {self.heartbeats}")
        else:
            self.log(f"channel {channel}: {data[:64].hex()}")

    def connect(self, timeout=300.0):
        deadline = time.monotonic() + timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(self.uart_socket))
                return sock
            except OSError:
                sock.close()
                time.sleep(0.5)
        return None

    def run(self):
        threading.Thread(target=self.bridge.pump, daemon=True, name="v850-most-tx").start()
        threading.Thread(target=self._car_loop, daemon=True, name="v850-car").start()
        sock = self.connect()
        if sock is None:
            self.log(f"no connection to the V850 UART socket {self.uart_socket}")
            return
        with self._write_lock:
            self.sock = sock
        self.connected = True
        self.log(f"connected to {self.uart_socket}")
        junk_logged = 0
        try:
            while not self._stop.is_set():
                data = sock.recv(4096)
                if not data:
                    break
                for frame_type, payload in self.parser.feed(data):
                    try:
                        self.on_frame(frame_type, payload)
                    except (ValueError, struct.error) as error:
                        self.log(f"bad frame {frame_type:#x}: {error}")
                if len(self.parser.junk) > junk_logged:
                    # Not our framing: show what else is talking on UART2.
                    chunk = bytes(self.parser.junk[junk_logged:])
                    junk_logged = len(self.parser.junk)
                    self.log(f"non-frame bytes from guest: {chunk!r}")
        except OSError as error:
            self.log(f"link ended: {error}")
        finally:
            self.connected = False
            with self._write_lock:
                self.sock = None
            sock.close()
            self.log("V850 UART disconnected")

    def _car_loop(self):
        while not self._stop.wait(0.25):
            try:
                self.car.tick()
                self.bridge.publish_changes()
            except Exception as error:      # never let one bad value stop the feed
                self.log(f"car feed: {error!r}")

    def stop(self):
        self._stop.set()
        self.bridge.close()
        with self._write_lock:
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def snapshot(self):
        return {"connected": self.connected, "watchdog_heartbeats": self.heartbeats,
                "channels": {str(k): v for k, v in sorted(self.channels.items())},
                "bad_bytes_from_guest": self.parser.skipped,
                "bad_bytes_sample": bytes(self.parser.junk[:256]).decode("latin-1"), "guest_log": list(self.guest_log)[-12:],
                "most": self.bridge.snapshot()}
