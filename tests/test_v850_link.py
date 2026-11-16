from pathlib import Path
import runpy
import struct
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import v850_link  # noqa: E402
from v850_link import (Car, FrameParser, MostBridge, MostMessage, V850, encode_frame,  # noqa: E402
                       ioipc_header, parse_ioipc)

ARCHIVER = runpy.run_path(str(TOOLS / "archive-idrive-rootfs.py"))


def bridge_message(message_id, payload=b""):
    header = bytearray(20)
    header[0] = 1
    struct.pack_into("<H", header, 2, 0x04F1)
    header[4], header[5] = 4, 5
    struct.pack_into("<HH", header, 6, message_id, 20 + len(payload))
    return bytes(header) + payload


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, channel, payload):
        self.sent.append((channel, payload))

    def ids(self):
        return [struct.unpack_from("<H", payload, 6)[0] for _channel, payload in self.sent]


def drain(bridge):
    frames = []
    while bridge._pending:
        frames.append(bridge._pending.popitem(last=False)[1])
    return frames


STATE = {"ignition": True, "speed_kph": 87.5, "engine_rpm": 2300, "gear": "D", "fuel_pct": 50}


class FramingTests(unittest.TestCase):
    def test_host_frames_carry_a_byte_sum(self):
        frame = encode_frame(2, b"\x01\x02\xff", checksum=True)
        self.assertEqual(frame[:5], b"\xa5\x5a\x02\x05\x00")
        self.assertEqual(frame[5:8], b"\x01\x02\xff")
        self.assertEqual(struct.unpack("<H", frame[8:])[0], 0x102)

    def test_parser_resynchronises_and_splits(self):
        parser = FrameParser()
        stream = b"junk" + encode_frame(1, b"abc", False) + encode_frame(0x10, b"hi", False)
        frames = parser.feed(stream[:7]) + parser.feed(stream[7:])
        self.assertEqual(frames, [(1, b"abc"), (0x10, b"hi")])
        self.assertEqual(parser.skipped, 4)
        self.assertEqual(bytes(parser.junk), b"junk")

    def test_ioipc_header_round_trip(self):
        message = ioipc_header(3, 9, 25, 1) + b"xyz"
        self.assertEqual(parse_ioipc(message), (25, 0x80, 9, b"xyz"))
        crc = ioipc_header(2, 0, 26, 0, 0x40) + b"\0\0\0\0" + b"ok"
        self.assertEqual(parse_ioipc(crc)[3], b"ok")

    def test_most_message_layout(self):
        message = MostMessage(0x0110, 0x2002, 0x05, 0, 0x621, MostMessage.OP_STATUS, b"\x22\x2e")
        packed = message.pack()
        self.assertEqual(len(packed), 25 + 2)
        self.assertEqual(struct.unpack_from("<iiHHBBHBII", packed),
                         (2, 0, 0x0110, 0x2002, 0x05, 0, 0x621, 0x0C, 1, 2))
        again = MostMessage.parse(packed)
        self.assertEqual((again.func, again.data), (0x621, b"\x22\x2e"))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.car = Car(lambda: STATE, {"e5:1:c00": "0000"})
        self.bridge = MostBridge(lambda *_: None, self.car, lambda _text: None)

    def test_bring_up_registers_client_controller_and_devices(self):
        self.bridge.on_message(bridge_message(0x8005, b"hello"))
        self.bridge.on_message(bridge_message(0x8000, struct.pack("<IH", 7, 0x2007)))
        frames = drain(self.bridge)
        ids = [struct.unpack_from("<H", frame, 6)[0] for frame in frames]
        self.assertEqual(ids, [5, 3, 2])
        self.assertEqual((self.bridge.client, self.bridge.device_id), (7, 0x2007))
        count = struct.unpack_from("<I", frames[2], 26)[0]
        keys = struct.unpack_from(f"<{count}H", frames[2], 30)
        self.assertIn(0xE000, keys)
        self.assertIn(0x0500, keys)
        self.assertEqual(struct.unpack_from("<H", frames[0], 8)[0], 20)

    def subscribe(self, fblock, inst, fkt, listener=0x0110):
        data = bytes([1]) + struct.pack(">HH", listener, fkt)
        telegram = MostMessage(0x2002, 0x0110, fblock, inst, 1, MostMessage.OP_SET, data).pack()
        self.bridge.hello = True
        self.bridge.on_message(bridge_message(0x8001, struct.pack("<I", 2) + telegram))
        return [MostMessage.parse(frame, 24) for frame in drain(self.bridge)]

    def test_subscription_gets_ack_and_speed_status(self):
        ack, status = self.subscribe(0x05, 0, 0x621)
        self.assertEqual((ack.func, ack.op), (1, MostMessage.OP_STATUS))
        self.assertEqual((status.func, status.dst, status.data), (0x621, 0x0110, struct.pack(">H", 8750)))

    def test_neutral_default_answers_known_property(self):
        _ack, status = self.subscribe(0xE5, 1, 0xC00)
        self.assertEqual(status.data, b"\0\0")

    def test_unknown_property_is_acknowledged_rarely(self):
        self.assertEqual(len(self.subscribe(0xE5, 1, 0x999)), 1)
        self.assertEqual(self.subscribe(0xE5, 1, 0x999), [])

    def test_changes_are_published_once(self):
        state = dict(STATE)
        self.car.state = lambda: state
        self.subscribe(0x05, 0, 0x228)
        self.assertEqual(self.bridge.publish_changes(), 0)
        state["gear"] = "R"
        self.assertEqual(self.bridge.publish_changes(), 1)
        self.assertEqual(MostMessage.parse(drain(self.bridge)[0], 24).data, b"\x01")

    def test_queued_status_is_replaced_by_a_newer_one(self):
        message = lambda value: MostMessage(1, 2, 0x05, 0, 0x621, MostMessage.OP_STATUS, value)
        self.bridge.send_most(message(b"\0\1"))
        self.bridge.send_most(message(b"\0\2"))
        frames = drain(self.bridge)
        self.assertEqual(len(frames), 1)
        self.assertEqual(MostMessage.parse(frames[0], 24).data, b"\0\2")

    def test_hmi_writes_are_recorded(self):
        telegram = MostMessage(0x2002, 0x0110, 0xE1, 0, 0x200, MostMessage.OP_SET, b"\x05").pack()
        self.bridge.hello = True
        self.bridge.on_message(bridge_message(0x8001, struct.pack("<I", 2) + telegram))
        self.assertEqual(self.bridge.snapshot()["from_hmi"], {"E1.0.200": {"op": 0, "data": "05"}})


class CarTests(unittest.TestCase):
    def test_terminal_and_power_follow_ignition(self):
        state = dict(STATE)
        car = Car(lambda: state)
        self.assertEqual(car.status_for(0x05, 0, 0x21F), bytes((12, 1, 0)))
        self.assertEqual(car.status_for(0xD1, 2, 0x430), b"\x03")
        state["ignition"] = False
        self.assertEqual(car.status_for(0x05, 0, 0x21F), bytes((6, 0, 0)))
        self.assertEqual(car.status_for(0xD1, 2, 0x434), b"\x00")

    def test_range_and_trip_layouts(self):
        car = Car(lambda: STATE)
        self.assertEqual(len(car.status_for(0xE0, 0, 0x509)), 12)
        self.assertEqual(len(car.status_for(0xE0, 0, 0x512)), 9)
        self.assertIsNone(car.status_for(0xE0, 0, 0x200))


class LinkTests(unittest.TestCase):
    def test_flow_control_grants_every_channel(self):
        link = V850(Path("/nonexistent"), lambda: STATE, defaults_file=None)
        sent = []
        link.write_frame = lambda frame_type, data: sent.append((frame_type, data))
        request = b"\x10\x02" + bytes((25, 3, 0, 0, 26, 1, 0, 0))
        link.on_frame(1, ioipc_header(len(request), 0, 0, 0) + request)
        frame_type, data = sent[0]
        self.assertEqual(frame_type, 2)
        self.assertEqual(parse_ioipc(data)[3], b"\x10\x02" + bytes((25, 0, 1, 0, 26, 0, 1, 0)))

    def test_end_to_end_over_a_socket(self):
        import socket
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v850.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            link = V850(path, lambda: STATE, defaults_file=None)
            thread = threading.Thread(target=link.run, daemon=True)
            thread.start()
            guest, _ = server.accept()
            hello = bridge_message(0x8005, b"")
            guest.sendall(encode_frame(1, ioipc_header(len(hello), 0, 25, 0) + hello, False))
            guest.settimeout(3)
            received = b""
            deadline = time.monotonic() + 3
            frames = []
            parser = FrameParser()
            while not frames and time.monotonic() < deadline:
                frames = parser.feed(guest.recv(4096))
            link.stop()
            guest.close()
            server.close()
        frame_type, data = frames[0]
        self.assertEqual(frame_type, 2)
        self.assertEqual(sum(data[:-2]) & 0xFFFF, struct.unpack("<H", data[-2:])[0])
        channel, _flags, _sequence, body = parse_ioipc(data[:-2])
        self.assertEqual((channel, struct.unpack_from("<H", body, 6)[0]), (25, 5))


STARTER = """<?xml version="1.0"?>
<Starter>
  <EnvironmentCount>1</EnvironmentCount>
  <EnvironmentTable>
    <Environment><Number>1</Number><Name>A</Name><EnvName>A</EnvName><Value>1</Value></Environment>
  </EnvironmentTable>
  <Package><Number>36</Number><Name>SPI_IPC</Name><RequestState>RUN</RequestState><HostsProcess>4</HostsProcess><HostsProcess>5</HostsProcess></Package>
  <Process><Number>4</Number><Name>/bin/dev-spi-omap543x</Name><Args>-c /etc/dev-spi_v850_omap543x.cfg</Args></Process>
  <Process><Number>5</Number><Name>/bin/io-ipc</Name><Args>-c /etc/io-ipc-o5.cfg -v</Args><ResArgs>-c /etc/io-ipc-o5.cfg</ResArgs><VariantCode>82000000</VariantCode><VariantMask>82000000</VariantMask><ProvidesTimedInterface>20</ProvidesTimedInterface><RequiresTimedInterface>53</RequiresTimedInterface></Process>
  <Process><Number>6</Number><Name>/opt/sys/bin/most</Name><Args>--devmode=client --serverRoleName=MOSTSubSystemServer --heartbeat-interval=500 --SIPCProtocol=tcp</Args><RequiresTimedInterface /><HighMem>1</HighMem></Process>
  <Process><Number>7</Number><Name>/bin/devc-sernuke</Name><Args>-N -u4 -b921600 0x4806C000^2,105</Args><ResArgs>-u4 0x4806c000</ResArgs></Process>
</Starter>
"""


class ArchiveTests(unittest.TestCase):
    def test_starter_runs_most_as_master_with_the_v850_config(self):
        root = ET.fromstring(STARTER)
        notes = ARCHIVER["_v850_link_starter"](root, "/etc/io-ipc-idrive-v850.cfg")
        self.assertTrue(notes)
        spi, ioipc, most, serial = root.findall("Process")
        self.assertEqual([entry.text for entry in root.find("Package").findall("HostsProcess")], ["5"])
        self.assertEqual(ioipc.findtext("Args"), "-c /etc/io-ipc-idrive-v850.cfg -v")
        self.assertEqual((ioipc.findtext("VariantCode"), ioipc.findtext("VariantMask")), ("0", "0"))
        self.assertFalse(ioipc.findtext("RequiresTimedInterface"))
        self.assertFalse(most.findtext("RequiresTimedInterface"))
        self.assertEqual(serial.findtext("Args"), "-N -u4 -b921600 0x4806E000^2,102")
        self.assertEqual(serial.findtext("ResArgs"), "-u4 0x4806E000")
        self.assertEqual(ioipc.findtext("ResArgs"), "-c /etc/io-ipc-idrive-v850.cfg")
        self.assertEqual(most.findtext("Args"), "--devmode=master --heartbeat-interval=0 --SIPCProtocol=tcp")
        number = most.findtext("RequiresEnvironment")
        environment = [env for env in root.iter("Environment") if env.findtext("Number") == number][0]
        self.assertEqual((environment.findtext("EnvName"), environment.findtext("Value")),
                         ("V850_MOSTSUBNET_OMAP_DIRECT_REGISTRATION", "1"))
        self.assertEqual(root.findtext("EnvironmentCount"), "2")
        self.assertLess(list(most).index(most.find("RequiresEnvironment")), list(most).index(most.find("HighMem")))

    def test_ioipc_config_swaps_only_the_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            (root / "lib").mkdir()
            (root / "lib/ioipc-spi-omap543x.so").write_bytes(b"")
            (root / "etc/io-ipc-o5.cfg").write_text(
                "decoder=hp\ntchannel=t_index=0,filename=ioipc-spi-omap543x.so,subopts=\"spi=2\"\nchannel=25\n")
            original, new_path, data, directory_name = ARCHIVER["_v850_ioipc_config"](root, ET.fromstring(STARTER))
        self.assertEqual((original, new_path, directory_name), ("/etc/io-ipc-o5.cfg", "/etc/io-ipc-idrive-v850.cfg", "lib"))
        self.assertEqual(data.decode(), 'decoder=hp\ntchannel=t_index=0,filename=ioipc-idrive-v850.so,subopts="v850=1"\nchannel=25\n')

    def test_rejects_a_starter_without_most_client(self):
        root = ET.fromstring(STARTER.replace("--devmode=client", "--devmode=master"))
        with self.assertRaisesRegex(RuntimeError, "no stock MOST client"):
            ARCHIVER["_v850_link_starter"](root, "/etc/x.cfg")


if __name__ == "__main__":
    unittest.main()
