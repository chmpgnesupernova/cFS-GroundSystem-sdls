"""MSN_APP command page and telemetry pages.

Commands: the MSN App page (command-pages.txt, CommandFiles/MSN_APP_CMD, ParameterFiles) must
produce exactly the example packets in msn_app/README.md section 3. The parameter string is
built the way Parameter.py builds it from the parameter table.

Telemetry: the four MSN pages in telemetry-pages.txt are started like the Telemetry System
starts them, with the TLM header offset 4 that GroundSystem.py writes for header version 1,
and fed packets laid out as msn_app_msg.h (16-byte header, LE payload).
"""

import contextlib
import csv
import os
import pathlib
import pickle
import struct
import unittest

import test_telemetry_gui as tg
from test_groundsystem import REPO

CMD_DIR = REPO / "Subsystems" / "cmdGui"
TEST_INSTANCE = "1299"  # own OffsetData file, so the shared /tmp/OffsetData is not touched


@contextlib.contextmanager
def offsets(tlm, cmd_pri=0, cmd_sec=0):
    """An OffsetData file as GroundSystem.save_offsets writes it, for a private instance."""
    saved = os.environ.get("GS_INSTANCE")
    os.environ["GS_INSTANCE"] = TEST_INSTANCE
    path = pathlib.Path(f"/tmp/OffsetData-{TEST_INSTANCE}")
    path.write_bytes(bytes((tlm, cmd_pri, cmd_sec)))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        if saved is None:
            os.environ.pop("GS_INSTANCE", None)
        else:
            os.environ["GS_INSTANCE"] = saved


def msn_page():
    with open(CMD_DIR / "command-pages.txt") as f:
        rows = [r for r in csv.reader(f, skipinitialspace=True) if r and not r[0].startswith("#")]
    (row,) = [r for r in rows if r[1].strip() == "MSN_APP_CMD"]
    return [c.strip() for c in row]


def param_string(param_file, values):
    """What Parameter.ProcessSendButton sends for the values typed into the table."""
    path = CMD_DIR / "ParameterFiles" / param_file
    if not path.exists():
        assert not values
        return None
    with open(path, "rb") as f:
        _, names, _, _, flags, str_len = pickle.load(f)
    assert len(names) == len(values), names
    parts = []
    for k, value in enumerate(values):
        if flags[k] == "--string":
            parts.append(f'{flags[k]}="{str_len[k]}:{value}"')
        else:
            parts.append(f"{flags[k]}={value}")
    return " ".join(parts)


def name20(text):
    return text.encode().ljust(20, b"\0")


class MsnCommandPageTest(unittest.TestCase):
    # (button, typed parameter values, packet from msn_app/README.md section 3)
    CASES = [
        ("NOOP", [], bytes.fromhex("18D0C000000100F6")),
        ("RESET_COUNTERS", [], bytes.fromhex("18D0C000000101F7")),
        ("SET_PARAM", ["0", "50"], bytes.fromhex("18D0C000000902CE" "0000000032000000")),
        ("SET_STARTUP", ["SAMPLE_APP", "0"],
         bytes.fromhex("18D0C000001903F5") + name20("SAMPLE_APP") + bytes(4)),
        ("RUN_DIAG", [], bytes.fromhex("18D0C000000104F2")),
        ("GET_APP_STATUS", ["SAMPLE_APP"], bytes.fromhex("18D0C000001505FF") + name20("SAMPLE_APP")),
        ("READ_TEMP", [], bytes.fromhex("18D0C000000106F0")),
    ]

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(offsets(4))
        from MiniCmdUtil import MiniCmdUtil  # noqa: PLC0415

        self.MiniCmdUtil = MiniCmdUtil
        _, def_file, self.pkt_id, self.endian, cls, self.host, self.port = msn_page()
        self.assertEqual((self.pkt_id, self.endian, cls), ("0x18D0", "LE", "UdpCommands.py"))
        with open(CMD_DIR / "CommandFiles" / def_file, "rb") as f:
            self.labels, self.codes, self.param_files = pickle.load(f)

    def packet(self, label, values):
        k = self.labels.index(label)
        mcu = self.MiniCmdUtil(self.host, self.port, self.endian, self.pkt_id, self.codes[k],
                               param_string(self.param_files[k], values))
        self.addCleanup(mcu.mm.close)
        mcu.assemble_packet()
        return bytes(mcu.packet)

    def test_every_button_matches_the_readme_packet(self):
        self.assertEqual(self.labels, [c[0] for c in self.CASES])
        for label, values, expected in self.CASES:
            with self.subTest(button=label):
                self.assertEqual(self.packet(label, values).hex(), expected.hex())

    def test_signed_value_and_enable(self):
        # TEMP_LOW -30: ParamId 1 then 3 spare bytes, Value as LE int32
        self.assertEqual(self.packet("SET_PARAM", ["1", "-30"])[8:], b"\x01\0\0\0" + struct.pack("<i", -30))
        # Enable 1, then 3 spare bytes
        self.assertEqual(self.packet("SET_STARTUP", ["FILE_DL", "1"])[8:], name20("FILE_DL") + b"\x01\0\0\0")

    def test_lengths_are_the_ones_the_satellite_checks(self):
        sizes = {"NOOP": 8, "RESET_COUNTERS": 8, "SET_PARAM": 16, "SET_STARTUP": 32,
                 "RUN_DIAG": 8, "GET_APP_STATUS": 28, "READ_TEMP": 8}
        samples = dict((c[0], c[1]) for c in self.CASES)
        for label, size in sizes.items():
            with self.subTest(button=label):
                self.assertEqual(len(self.packet(label, samples[label])), size)


def start_msn_page(title):
    with open(tg.TLM_DIR / "telemetry-pages.txt") as f:
        rows = [[c.strip() for c in r] for r in csv.reader(f, skipinitialspace=True)
                if r and not r[0].startswith("#")]
    (row,) = [r for r in rows if r[0] == title]
    with offsets(4):  # the page maps the file at start; the mapping outlives the file
        page = tg.start_page(title, row[2], row[3])
    return row[2], page


def tlm(mid, payload):
    header = bytearray(16)
    header[0:2] = mid.to_bytes(2, "big")
    header[2:4] = bytes.fromhex("c007")
    header[4:6] = (16 + len(payload) - 7).to_bytes(2, "big")
    return bytes(header) + payload


class MsnTelemetryPageTest(unittest.TestCase):
    def show(self, title, mid, payload):
        appid, page = start_msn_page(title)
        self.assertEqual(int(appid, 16), mid)
        self.assertEqual(page.mm[0], 4)
        page.thread.gt_signal_tlm_datagram.emit(tlm(mid, payload))
        tbl = page.tbl_telemetry
        return {tbl.item(k, 0).text(): tbl.item(k, 1).text() for k in range(tbl.rowCount())}

    def test_hk(self):
        payload = struct.pack("<BBBxfiii", 3, 1, 1, 75.0, 50, -20, 1)
        self.assertEqual(self.show("MSN HK Tlm", 0x08D0, payload), {
            "Command Counter": "3", "Error Counter": "1", "Temp Status": "HIGH",
            "Temperature (C last read)": "75.0", "TEMP_HIGH (C)": "50", "TEMP_LOW (C)": "-20",
            "TEMP_PERIOD (HK requests)": "1"})

    def test_temp(self):
        payload = struct.pack("<fB3xI", 21.5, 0, 12)
        self.assertEqual(self.show("MSN TEMP Tlm", 0x08D1, payload), {
            "Temperature (C)": "21.5", "Status": "NORMAL", "Sample Count": "12"})

    def test_diag(self):
        payload = struct.pack("<5BB2xI", 0, 1, 0, 0, 0, 1, 4)
        self.assertEqual(self.show("MSN DIAG Tlm", 0x08D2, payload), {
            "Temperature file read": "PASS", "Temperature in limits": "FAIL",
            "Param file matches": "PASS", "Essential apps in startup script": "PASS",
            "Essential apps running": "PASS", "Fail Count": "1", "Run Count": "4"})

    def test_app_status(self):
        # bytes after the first NUL are not part of the name (C strncpy may leave old text there)
        payload = (b"SAMPLE_APP\0junk".ljust(20, b"\0")
                   + struct.pack("<BB2xIIII", 1, 2, 50, 16384, 321, 0))
        table = self.show("MSN APP_STATUS Tlm", 0x08D3, payload)
        self.assertEqual(table, {
            "App Name": "SAMPLE_APP",  # NUL padding is not shown
            "Running": "YES", "Startup": "DISABLED", "Priority": "50", "Stack Size": "16384",
            "Execution Counter": "321", "Child Tasks": "0"})


if __name__ == "__main__":
    unittest.main()
