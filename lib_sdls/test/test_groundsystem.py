"""GroundSystem integration: the real MiniCmdUtil.py and RoutingService.py over UDP.

MiniCmdUtil must send TC frames (and nothing when no frame can be built); RoutingService
must take the packets out of each TM frame and publish them one by one, exactly as the
GUI expects plain Space Packets. RoutingService runs with stand-ins for PyQt5 and zmq,
so the test needs neither a display nor a running GUI (and never binds the GUI's
ipc:///tmp/GroundSystem endpoint).
"""

import contextlib
import os
import pathlib
import socket
import sys
import threading
import time
import types
import unittest

import sdls_binding as b
from sat_reference import SatTmFramer, make_spp, sat_tc_extract_packet
from test_sdlp_link import SDLP_ONLY, private_link, read_log

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "Subsystems" / "cmdGui"))

OFFSET_FILE = pathlib.Path("/tmp/OffsetData")  # MiniCmdUtil maps it in its constructor


@contextlib.contextmanager
def offset_file():
    """GroundSystem writes /tmp/OffsetData at startup; make a zero one if it is missing."""
    created = False
    if not OFFSET_FILE.exists():
        OFFSET_FILE.write_bytes(bytes(4))
        created = True
    try:
        yield
    finally:
        if created:
            OFFSET_FILE.unlink(missing_ok=True)


class MiniCmdUtilTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.log = stack.enter_context(private_link())
        stack.enter_context(offset_file())
        from MiniCmdUtil import MiniCmdUtil  # noqa: PLC0415 - after the path setup

        self.MiniCmdUtil = MiniCmdUtil
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(0.5)
        self.addCleanup(self.rx.close)
        self.port = self.rx.getsockname()[1]

    def mcu(self, parameters=None):
        return self.MiniCmdUtil("127.0.0.1", self.port, "LE", "0x1880", 0, parameters)

    def test_command_goes_out_as_a_tc_frame(self):
        mcu = self.mcu("--uint16=7")
        self.assertTrue(mcu.send_packet())
        frame, _ = self.rx.recvfrom(2048)
        self.assertEqual(sat_tc_extract_packet(frame), bytes(mcu.packet))
        (entry,) = read_log(self.log)
        self.assertEqual((entry["dir"], entry["result"], entry["src"]), ("TC", "OK", "MiniCmdUtil"))
        self.assertEqual((entry["in"], entry["out"]), (bytes(mcu.packet).hex(), frame.hex()))

    def test_no_frame_no_send_when_the_library_is_missing(self):
        os.environ["SDLS_LIB"] = "/nonexistent/libsdls.so"
        import sdlp_link  # noqa: PLC0415

        sdlp_link._lib = None
        self.assertFalse(self.mcu().send_packet())
        with self.assertRaises(socket.timeout):
            self.rx.recvfrom(2048)
        self.assertEqual(read_log(self.log)[0]["result"], "FAIL")

    def test_no_send_for_a_command_too_long_for_one_frame(self):
        self.assertFalse(self.mcu('--string="1000:A"').send_packet())
        with self.assertRaises(socket.timeout):
            self.rx.recvfrom(2048)
        self.assertEqual(read_log(self.log)[0]["err"], "TC_SDLP_MY_INVALID_LENGTH")


# --- RoutingService with PyQt5 / zmq stand-ins ----------------------------------------------


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _Publisher:
    def __init__(self):
        self.sent = []

    def bind(self, endpoint):
        self.endpoint = endpoint

    def send_multipart(self, parts):
        self.sent.append(parts)


class _Context:
    def __init__(self):
        self.publisher = _Publisher()

    def socket(self, kind):
        return self.publisher

    def destroy(self):
        pass


@contextlib.contextmanager
def routing_service_module():
    qtcore = types.ModuleType("PyQt5.QtCore")
    qtcore.QThread = type("QThread", (), {"__init__": lambda self: None})
    signal = _Signal()
    qtcore.pyqtSignal = lambda *types_: signal
    pyqt5 = types.ModuleType("PyQt5")
    pyqt5.QtCore = qtcore
    zmq = types.ModuleType("zmq")
    zmq.Context, zmq.PUB = _Context, 1
    stubs = {"PyQt5": pyqt5, "PyQt5.QtCore": qtcore, "zmq": zmq}
    saved = {name: sys.modules.get(name) for name in [*stubs, "RoutingService"]}
    sys.modules.update(stubs)
    sys.modules.pop("RoutingService", None)
    sys.path.insert(0, str(REPO))
    try:
        import RoutingService  # noqa: PLC0415

        yield RoutingService, signal
    finally:
        sys.path.remove(str(REPO))
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class RoutingServiceTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.log = stack.enter_context(private_link())
        module, self.signal = stack.enter_context(routing_service_module())
        module.udp_recv_port = 0  # any free port
        self.service = module.RoutingService()
        self.publisher = self.service.publisher
        threading.Thread(target=self.service.run, daemon=True).start()
        self._wait(lambda: self.service.sock.getsockname()[1] != 0)
        self.addr = ("127.0.0.1", self.service.sock.getsockname()[1])
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.tx.close)
        self.addCleanup(self.service.sock.close)

    def _wait(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def frame(self, packets):
        framer = SatTmFramer(sdlp_only=SDLP_ONLY)
        for packet in packets:
            framer.forward_packet(packet)
        framer.send_frame()
        (frame,) = framer.sent
        return frame

    def test_each_packet_of_a_frame_is_published_as_before(self):
        packets = [make_spp(0x100, b"\xAA" * 10), make_spp(0x101, b"\xBB" * 20), make_spp(0x0FF, b"\xCC")]
        self.tx.sendto(self.frame(packets), self.addr)
        self._wait(lambda: len(self.publisher.sent) == 3)
        topics = [parts[0].decode() for parts in self.publisher.sent]
        self.assertEqual(topics, [f"GroundSystem.Spacecraft1.TelemetryPackets.{hex(int.from_bytes(p[:2], 'big'))}"
                                  for p in packets])
        self.assertEqual([parts[1] for parts in self.publisher.sent], packets)  # plain Space Packets
        self.assertEqual(self.signal.emitted, [("127.0.0.1", b"Spacecraft1")])
        entry = read_log(self.log)[-1]
        self.assertEqual((entry["dir"], entry["result"], entry["src"], entry["host"]),
                         ("TM", "OK", "RoutingService", "127.0.0.1"))

    def test_a_datagram_that_is_not_a_frame_is_dropped(self):
        self.tx.sendto(b"Test tlm message", self.addr)  # what TlmUDPSender.py sends
        self._wait(lambda: any(e and e["result"] == "FAIL" for e in read_log(self.log)))
        self.tx.sendto(self.frame([make_spp(0x100, b"ok")]), self.addr)
        self._wait(lambda: len(self.publisher.sent) == 1)
        self.assertEqual(len(self.signal.emitted), 1)  # the bad datagram registered no spacecraft
        self.assertEqual(read_log(self.log)[0]["err"], "TM_SDLP_MY_INVALID_LENGTH")


if __name__ == "__main__":
    unittest.main()
