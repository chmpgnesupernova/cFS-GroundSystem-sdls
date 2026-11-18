"""File Relay windows: Subsystems/cmdGui/FileRelayCommand.py (uplink buttons on top,
downlink buttons below) and Subsystems/tlmGUI/FileRelayTelemetry.py (Transmit above
Receive), with lib_sdls/file_relay.py behind them.

The windows are built with small PyQt5 / zmq stand-ins (no display needed): the tests
press their buttons, check the commands that go out over UDP as TC frames, and feed
relay packets to the telemetry display."""

import contextlib
import json
import os
import pathlib
import random
import runpy
import socket
import sys
import tempfile
import types
import unittest
import zlib

from sat_reference import sat_tc_extract_packet
from test_file_rx import file_segments, seg_packet
from test_groundsystem import REPO
from test_sdlp_link import private_link

sys.path.insert(0, str(REPO / "lib_sdls"))
import file_relay  # noqa: E402
import file_rx  # noqa: E402
import sdlp_link  # noqa: E402

CMD_PAGE = REPO / "Subsystems" / "cmdGui" / "FileRelayCommand.py"
TLM_PAGE = REPO / "Subsystems" / "tlmGUI" / "FileRelayTelemetry.py"


def status_packet(state=1, name="/cf/relay/x.bin", size=5000, received=0, expected=0, file_crc=0,
                  gaps=(), total=None, tag=2):
    """Relay status 0x08C2 as FILE_UL_SendStatus builds it (README 3.2)."""
    p = bytearray(360)
    p[0:2] = (0x08C2).to_bytes(2, "big")
    p[2:4] = b"\xc0\x00"
    p[4:6] = (360 - 7).to_bytes(2, "big")
    b = 16
    p[b], p[b + 1], p[b + 2], p[b + 3] = state, 1, tag, len(gaps)
    for i, v in enumerate((size, received, expected, file_crc, len(gaps) if total is None else total)):
        p[b + 4 + 4 * i : b + 8 + 4 * i] = v.to_bytes(4, "big")
    p[b + 24 : b + 24 + len(name)] = name.encode()
    for k, (off, length) in enumerate(gaps):
        p[b + 88 + 8 * k : b + 92 + 8 * k] = off.to_bytes(4, "big")
        p[b + 92 + 8 * k : b + 96 + 8 * k] = length.to_bytes(4, "big")
    return bytes(p)


# ---------------------------------------------------------------- PyQt5 / zmq stand-ins


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _SignalAttr:
    def __set_name__(self, owner, name):
        self.key = "_sig_" + name

    def __get__(self, obj, owner):
        return self if obj is None else obj.__dict__.setdefault(self.key, _Signal())


class _Widget:
    """Any widget or layout: keeps text / value, accepts every other call."""

    clicked = _SignalAttr()
    valueChanged = _SignalAttr()

    def __init__(self, *args, **kwargs):
        self._text = args[0] if args and isinstance(args[0], str) else ""
        self._value = 0
        self._max = 100
        self._enabled = True

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    setPlainText, toPlainText = setText, text

    def setValue(self, value):
        self._value = value
        self.valueChanged.emit(value)

    def value(self):
        return self._value

    def setMaximum(self, value):
        self._max = value

    def maximum(self):
        return self._max

    def setEnabled(self, flag):
        self._enabled = flag

    def isEnabled(self):
        return self._enabled

    def click(self):
        self.clicked.emit()


class _QDialog(_Widget):
    def closeEvent(self, event):
        pass


class _QThread:
    finished = _SignalAttr()

    def __init__(self):
        self._running = False

    def start(self):  # synchronous in tests: run() completes before start() returns
        self._running = True
        self.run()
        self._running = False

    def isRunning(self):
        return self._running

    def wait(self, *_):
        return True


class _QFileDialog:
    next_path = ""

    @classmethod
    def getOpenFileName(cls, *_):
        return cls.next_path, ""

    @classmethod
    def getExistingDirectory(cls, *_):
        return cls.next_path


@contextlib.contextmanager
def qt_stubs():
    qtcore = types.ModuleType("PyQt5.QtCore")
    qtcore.QThread, qtcore.pyqtSignal = _QThread, lambda *_: _SignalAttr()
    qtwidgets = types.ModuleType("PyQt5.QtWidgets")
    for name in ("QGridLayout", "QGroupBox", "QHBoxLayout", "QLabel", "QLineEdit", "QPlainTextEdit",
                 "QProgressBar", "QPushButton", "QSpinBox", "QVBoxLayout", "QApplication"):
        setattr(qtwidgets, name, type(name, (_Widget,), {}))
    qtwidgets.QDialog, qtwidgets.QFileDialog = _QDialog, _QFileDialog
    pyqt5 = types.ModuleType("PyQt5")
    pyqt5.QtCore, pyqt5.QtWidgets = qtcore, qtwidgets
    zmq = types.ModuleType("zmq")
    zmq.SUB, zmq.SUBSCRIBE = 2, 6
    zmq.Context = lambda: types.SimpleNamespace(
        socket=lambda *_: types.SimpleNamespace(connect=lambda *_: None, setsockopt_string=lambda *_: None))
    stubs = {"PyQt5": pyqt5, "PyQt5.QtCore": qtcore, "PyQt5.QtWidgets": qtwidgets, "zmq": zmq}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def load(page):
    with qt_stubs():
        return types.SimpleNamespace(**runpy.run_path(str(page), run_name="file_relay_page"))


# ---------------------------------------------------------------- pure logic


class CommandBytesTest(unittest.TestCase):
    @staticmethod
    def xor(packet):
        x = 0
        for byte in packet:
            x ^= byte
        return x

    def test_relay_start(self):
        p = file_relay.relay_start_command("nasa2.jpg", 123456, 0xCAFEBABE, 2)
        self.assertEqual(len(p), 84)  # README: relay START is 84 B
        self.assertEqual((p[:2], p[6]), (b"\x18\xc3", 1))
        self.assertEqual(p[8:72], b"/cf/relay/nasa2.jpg".ljust(64, b"\0"))
        self.assertEqual(p[72:80], (123456).to_bytes(4, "big") + bytes.fromhex("cafebabe"))
        self.assertEqual(p[80:], bytes([2, 0, 0, 0]))
        self.assertEqual(self.xor(p), 0xFF)

    def test_data_end_status_cancel(self):
        p = file_relay.data_command(1920, b"x" * 960)
        self.assertEqual((len(p), p[6], p[8:12], p[12:14], p[14:16]),
                         (16 + 960, 2, (1920).to_bytes(4, "big"), (960).to_bytes(2, "big"), b"\0\0"))
        for build, cc in ((file_relay.end_command, 3), (file_relay.status_command, 4),
                          (file_relay.cancel_command, 5)):
            self.assertEqual((len(build()), build()[:2], build()[6]), (8, b"\x18\xc3", cc))
        for bad in ((100, b"x"), (0, b""), (0, b"x" * 961)):
            with self.subTest(bad=bad[0]), self.assertRaises(ValueError):
                file_relay.data_command(*bad)

    def test_output_enable(self):
        p = file_relay.output_enable_command("10.0.0.5")
        self.assertEqual((len(p), p[:2], p[6], p[8:]), (24, b"\x18\x80", 6, b"10.0.0.5".ljust(16, b"\0")))


class PlanTest(unittest.TestCase):
    def test_all_chunks_or_only_the_gaps(self):
        self.assertEqual(file_relay.plan_chunks(2000), [0, 960, 1920])
        self.assertEqual(file_relay.plan_chunks(5000, [(960, 960), (3840, 1160)]), [960, 3840, 4800])
        self.assertEqual(file_relay.plan_chunks(5000, [(1000, 10)]), [960])  # a gap inside a chunk

    def test_gaps_only_for_the_same_file_still_receiving(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "x.bin"
            path.write_bytes(bytes(5000))
            u = file_relay.UploadFile(path)
            status = file_rx.parse_status(status_packet(gaps=[(960, 960)]))
            self.assertEqual(file_relay.gaps_for(u, status), [(960, 960)])
            self.assertIsNone(file_relay.gaps_for(u, dict(status, state="COMPLETE")))
            self.assertIsNone(file_relay.gaps_for(u, dict(status, name="/cf/relay/y.bin")))
            self.assertIsNone(file_relay.gaps_for(u, None))

    def test_upload_file_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "fifteen_chars_x").write_bytes(b"x")
            (d / "empty.bin").write_bytes(b"")
            for name in ("fifteen_chars_x", "empty.bin", "missing.bin"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    file_relay.UploadFile(d / name)
            (d / "ok.bin").write_bytes(b"hello")
            u = file_relay.UploadFile(d / "ok.bin")
            self.assertEqual((u.board_name, u.size, u.crc32), ("/cf/relay/ok.bin", 5, zlib.crc32(b"hello")))

    def test_parse_status(self):
        s = file_rx.parse_status(status_packet(state=3, size=9000, received=9000, expected=0x11, file_crc=0x11,
                                               gaps=[(0, 960), (1920, 5)], total=7))
        self.assertEqual((s["state"], s["size"], s["received"], s["total_gaps"], s["gaps"], s["dest_tag"]),
                         ("COMPLETE", 9000, 9000, 7, [(0, 960), (1920, 5)], 2))
        self.assertIsNone(file_rx.parse_status(seg_packet(0x08C3, 0, 1, 2, 0, 1, 0, b"x")))

    def test_state_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.json"
            self.assertIsNone(file_relay.read_state(path))
            file_relay.write_state({"time": 1, "transmit": None, "receive": None}, path)
            self.assertEqual(file_relay.read_state(path)["time"], 1)
            path.write_text("{broken")
            self.assertIsNone(file_relay.read_state(path))


# ---------------------------------------------------------------- windows


class WindowBase(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(private_link())
        self.tmp = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.tmp / "state.json"
        old = os.environ.get("GS_FILE_RELAY_STATE")
        os.environ["GS_FILE_RELAY_STATE"] = str(self.state)
        self.addCleanup(lambda: os.environ.__setitem__("GS_FILE_RELAY_STATE", old) if old is not None
                        else os.environ.pop("GS_FILE_RELAY_STATE", None))


class CommandWindowTest(WindowBase):
    def setUp(self):
        super().setUp()
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(1)
        self.addCleanup(self.rx.close)
        self.mod = load(CMD_PAGE)
        self.page = self.mod.FileRelayCommand("127.0.0.1", self.rx.getsockname()[1])
        self.addCleanup(self.page.sender.close)
        self.content = random.Random(3).randbytes(5000)
        self.file = self.tmp / "relay.bin"
        self.file.write_bytes(self.content)
        self.page.edit_file.setText(str(self.file))

    def sent(self):
        out = []
        self.rx.settimeout(0.2)
        with contextlib.suppress(socket.timeout):
            while True:
                out.append(sat_tc_extract_packet(self.rx.recvfrom(2048)[0]))
        return out

    def test_buttons_in_order_without_station_names(self):
        page = self.page
        up = [page.btn_output_enable, page.btn_start, page.btn_data, page.btn_end, page.btn_status, page.btn_cancel]
        down = [page.btn_add_dest, page.btn_done, page.btn_remove_dest, page.btn_resend]
        self.assertEqual([b.text() for b in up], ["OUTPUT_ENABLE", "START", "DATA", "END", "STATUS", "CANCEL"])
        self.assertEqual([b.text() for b in down], ["ADD_DEST", "DONE", "REMOVE_DEST", "RESEND"])
        source = CMD_PAGE.read_text() + TLM_PAGE.read_text()
        for word in ("Station A", "Station B", "station A", "station B", "(A)", "(B)", "Ground A", "Ground B"):
            self.assertNotIn(word, source)

    def test_uplink_sequence(self):
        page = self.page
        page.edit_my_ip.setText("10.0.0.7")
        page.btn_output_enable.click()
        page.btn_start.click()
        page.btn_data.click()
        page.btn_end.click()
        page.btn_status.click()
        page.btn_cancel.click()
        packets = self.sent()
        self.assertEqual(packets[0], file_relay.output_enable_command("10.0.0.7"))
        self.assertEqual(packets[1], file_relay.relay_start_command("relay.bin", 5000, zlib.crc32(self.content), 2))
        data = packets[2:8]
        self.assertEqual([int.from_bytes(p[8:12], "big") for p in data], [0, 960, 1920, 2880, 3840, 4800])
        self.assertEqual(b"".join(p[16:] for p in data), self.content)
        self.assertEqual(packets[8:], [file_relay.end_command(), file_relay.status_command(),
                                       file_relay.cancel_command()])
        self.assertIn("CANCEL sent", page.lbl_status.text())

    def test_data_sends_only_the_gaps_of_the_latest_status(self):
        status = file_rx.parse_status(status_packet(name="/cf/relay/relay.bin", size=5000,
                                                    gaps=[(960, 960), (4800, 200)]))
        file_relay.write_state({"time": 0, "transmit": status, "receive": None})
        self.page.btn_data.click()
        packets = self.sent()
        self.assertEqual([int.from_bytes(p[8:12], "big") for p in packets], [960, 4800])
        self.assertIn("2 chunk(s) sent", self.page.lbl_status.text())

    def test_bad_file_name_is_refused_before_sending(self):
        bad = self.tmp / "a_name_too_long.bin"
        bad.write_bytes(b"x")
        self.page.edit_file.setText(str(bad))
        self.page.btn_start.click()
        self.assertEqual(self.sent(), [])
        self.assertIn("must be 1 to 14 bytes", self.page.lbl_status.text())

    def test_downlink_buttons_use_the_received_file(self):
        page = self.page
        file_relay.write_state({"time": 0, "transmit": None, "receive": {
            "name": "relay.bin", "size": 5000, "received": 3000, "missing": [[1000, 500], [4000, 1000]],
            "end_crc": None, "file_crc": None, "state": "RECEIVING", "verified": False, "path": None,
            "dest_tag": 2}})
        page.edit_my_ip.setText("10.0.0.8")
        page.spin_tm_port.setValue(1245)
        page.btn_add_dest.click()
        page.btn_resend.click()
        page.btn_done.click()  # no CRC yet: refused
        self.assertIn("DONE needs the CRC-32", page.lbl_status.text())
        file_relay.write_state({"time": 1, "transmit": None, "receive": {
            "name": "relay.bin", "size": 5000, "received": 5000, "missing": [], "end_crc": 0x1234,
            "file_crc": 0x1234, "state": "COMPLETE", "verified": True, "path": "/x", "dest_tag": 2}})
        page.btn_done.click()
        page.btn_remove_dest.click()
        packets = self.sent()
        self.assertEqual(packets, [file_rx.add_dest_command("10.0.0.8", 1245),
                                   file_rx.resend_command(1000, 500), file_rx.resend_command(4000, 1000),
                                   file_rx.done_command(0x1234), file_rx.remove_dest_command("10.0.0.8", 1245)])
        self.assertEqual(page.edit_crc.text(), "00001234")

    def test_command_system_opens_the_window(self):
        with qt_stubs():
            sys.modules.pop("CommandSystem", None)
            source = (REPO / "Subsystems" / "cmdGui" / "CommandSystem.py").read_text()
        self.assertIn('self._open_tool("FileRelayCommand.py", "File Relay")', source)
        self.assertIn("self.btn_file_relay", source)


class TelemetryWindowTest(WindowBase):
    def setUp(self):
        super().setUp()
        self.mod = load(TLM_PAGE)
        self.out = self.tmp / "received"
        self.page = self.mod.FileRelayTelemetry((), out_dir=self.out, dest_tag=2)
        self.addCleanup(self.page.receiver.close)
        self.content = random.Random(5).randbytes(20_000)

    def test_transmit_above_receive(self):
        source = TLM_PAGE.read_text()
        self.assertLess(source.index("layout.addWidget(tx_box)"), source.index("layout.addWidget(rx_box)"))
        self.assertIn('QGroupBox(f"Transmit', source)
        self.assertIn('QGroupBox(f"Receive', source)

    def test_transmit_shows_the_relay_status(self):
        self.page.handle_packet(status_packet(state=1, name="/cf/relay/up.bin", size=9600, received=7680,
                                              expected=0xABCD, gaps=[(960, 960), (5760, 960)]))
        self.assertEqual(self.page.tx_state.text(), "RECEIVING")
        self.assertEqual(self.page.tx_bytes.text(), "7680 / 9600 B")
        self.assertEqual((self.page.tx_bar.maximum(), self.page.tx_bar.value()), (9600, 7680))
        self.assertEqual(self.page.tx_gaps.text(), "2 range(s)")
        self.assertIn("960  +960", self.page.tx_gap_list.text())
        self.page.handle_packet(status_packet(state=3, name="/cf/relay/up.bin", size=9600, received=9600,
                                              expected=0xABCD, file_crc=0xABCD))
        self.assertIn("(match)", self.page.tx_crc.text())
        self.assertEqual(json.loads(self.state.read_text())["transmit"]["state"], "COMPLETE")

    def test_receive_saves_the_file(self):
        for packet in file_segments("down.bin", self.content):
            self.page.handle_packet(packet)
        self.assertEqual((self.out / "down.bin").read_bytes(), self.content)
        self.assertEqual(self.page.rx_state.text(), "COMPLETE")
        self.assertEqual(self.page.rx_saved.text(), str(self.out / "down.bin"))
        self.assertIn("START 1, DATA 20, END 1", self.page.rx_segments.text())
        rx = json.loads(self.state.read_text())["receive"]
        self.assertEqual((rx["state"], rx["file_crc"], rx["missing"]), ("COMPLETE", zlib.crc32(self.content), []))

    def test_receive_shows_missing_ranges_and_other_tags_are_ignored(self):
        segs = file_segments("gap.bin", self.content)
        for packet in segs[:5] + segs[8:]:  # 3 DATA segments lost
            self.page.handle_packet(packet)
        self.assertEqual(self.page.rx_state.text(), "RECEIVING")
        self.assertEqual(self.page.rx_gaps.text(), "1 range(s)")
        self.assertEqual(json.loads(self.state.read_text())["receive"]["missing"], [[4096, 3072]])
        self.page.spin_tag.setValue(3)  # now only DestTag 3
        self.page.handle_packet(file_segments("x.bin", b"abc", dest_tag=2)[0])
        self.assertEqual(self.page.receiver.name, "gap.bin")

    def test_subscriptions_from_the_telemetry_system(self):
        sub = self.mod.subscriptions
        self.assertEqual(sub("GroundSystem.0x8c2"),
                         ["GroundSystem.Spacecraft1.TelemetryPackets.0x8c2",
                          "GroundSystem.Spacecraft1.TelemetryPackets.0x8c3"])
        self.assertEqual(sub("GroundSystem.Spacecraft2.TelemetryPackets.0x8c2")[1],
                         "GroundSystem.Spacecraft2.TelemetryPackets.0x8c3")

    def test_listed_in_the_telemetry_pages(self):
        rows = (REPO / "Subsystems" / "tlmGUI" / "telemetry-pages.txt").read_text()
        self.assertIn("File Relay,                FileRelayTelemetry.py,   0x8C2,   null", rows)


class TogetherTest(WindowBase):
    """Both windows on one machine: the receive side's state drives DONE."""

    def test_received_file_then_done(self):
        tlm = load(TLM_PAGE).FileRelayTelemetry((), out_dir=self.tmp / "in", dest_tag=2)
        content = random.Random(9).randbytes(7000)
        for packet in file_segments("both.bin", content):
            tlm.handle_packet(packet)
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(1)
        self.addCleanup(rx.close)
        cmd = load(CMD_PAGE).FileRelayCommand("127.0.0.1", rx.getsockname()[1])
        self.addCleanup(cmd.sender.close)
        self.addCleanup(tlm.receiver.close)
        cmd.btn_done.click()
        self.assertEqual(sat_tc_extract_packet(rx.recvfrom(2048)[0]), file_rx.done_command(zlib.crc32(content)))
        sdlp_link.reset_tm_streams()


if __name__ == "__main__":
    unittest.main()
