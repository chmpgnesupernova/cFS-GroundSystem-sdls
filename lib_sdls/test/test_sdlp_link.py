"""sdlp_link.py tests: TC/TM through the module, the log file, and the monitor.

Each test class runs the module against a private copy of libsdls.so and a private log
file, so its static state and SA DB never mix with other test modules. Works with both
builds (SDLP-only and SDLS).
"""

import contextlib
import io
import multiprocessing
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import unittest

import sdls_binding as b
from sat_reference import SatTmFramer, make_spp, sat_tc_extract_packet

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import sdlp_link  # noqa: E402

SDLP_ONLY = b.sdlp_only_build()
ENV_KEYS = ("SDLS_LIB", "SDLP_LOG", "SDLS_SA_DB", "SDLP_TC_SPI")


@contextlib.contextmanager
def private_link():
    """sdlp_link on a fresh library copy, with its own log file and SA DB name.
    Yields the log path."""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    with tempfile.TemporaryDirectory() as tmp:
        lib_copy = pathlib.Path(tmp) / "libsdls_link.so"
        shutil.copy(b.find_library(), lib_copy)
        log = pathlib.Path(tmp) / "sdlp.log"
        sa_db = f"/sdls_link_test_{os.getpid()}_{time.monotonic_ns()}"
        os.environ.update(SDLS_LIB=str(lib_copy), SDLP_LOG=str(log), SDLS_SA_DB=sa_db)
        os.environ.pop("SDLP_TC_SPI", None)
        sdlp_link._lib, sdlp_link._mode = None, None
        try:
            yield log
        finally:
            lib = sdlp_link._lib
            if lib is not None and hasattr(lib, "SA_myUnlink"):  # SDLS build opened an SA DB
                lib.SA_myClose()
                lib.SA_myUnlink.argtypes = [b.ctypes.c_char_p]
                lib.SA_myUnlink(sa_db.encode())
            sdlp_link._lib, sdlp_link._mode = None, None
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def tm_frames(packets):
    framer = SatTmFramer(sdlp_only=SDLP_ONLY)  # SDLS build: SA10, the always open EP SA
    for packet in packets:
        framer.forward_packet(packet)
    framer.send_frame()
    return framer.sent


def read_log(path):
    if not path.exists():
        return []
    return [sdlp_link.parse_line(line) for line in path.read_text().splitlines()]


def _worker_log(log, sdls_lib, sa_db, count):
    """Another process (like each command dialog) writing big TM lines to the same log."""
    os.environ.update(SDLP_LOG=log, SDLS_LIB=sdls_lib, SDLS_SA_DB=sa_db)
    import sdlp_link as link  # noqa: PLC0415 - fresh import in the child

    frame = tm_frames([make_spp(0x300, bytes(range(256)) * 6)])[0]
    for _ in range(count):
        link.decapsulate_tm(frame, host="10.0.0.1")
    return os.getpid()


class LinkTest(unittest.TestCase):
    def setUp(self):
        self._ctx = private_link()
        self.log = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    # --- TC -----------------------------------------------------------------------------

    def test_tc_frame_reaches_the_satellite_and_is_logged(self):
        packet = make_spp(0x18, b"\x01\x02\x03\x04")
        frame = sdlp_link.encapsulate_tc(packet, source="unit")
        self.assertEqual(sat_tc_extract_packet(frame), packet)
        (entry,) = read_log(self.log)
        self.assertEqual((entry["dir"], entry["result"], entry["src"]), ("TC", "OK", "unit"))
        self.assertEqual(entry["mode"], "SDLP-only" if SDLP_ONLY else "SDLS")
        self.assertEqual((entry["in"], entry["out"]), (packet.hex(), frame.hex()))
        self.assertEqual(int(entry["pid"]), os.getpid())

    def test_tc_failure_raises_logs_and_returns_no_frame(self):
        packet = make_spp(0x18, bytes(b.TC_MAX_PACKET_LENGTH))  # one byte too long
        with self.assertRaises(sdlp_link.SdlpError) as ctx:
            sdlp_link.encapsulate_tc(packet)
        self.assertEqual((ctx.exception.code, ctx.exception.name), (-3, "TC_SDLP_MY_INVALID_LENGTH"))
        (entry,) = read_log(self.log)
        self.assertEqual((entry["result"], entry["err"], entry["out"]), ("FAIL", "TC_SDLP_MY_INVALID_LENGTH", "-"))

    def test_missing_library_refuses_and_logs(self):
        os.environ["SDLS_LIB"] = "/nonexistent/libsdls.so"
        sdlp_link._lib = None
        with self.assertRaises(sdlp_link.SdlpError) as ctx:
            sdlp_link.encapsulate_tc(make_spp(0x18, b"x"))
        self.assertIsNone(ctx.exception.code)
        self.assertEqual(read_log(self.log)[0]["err"], sdlp_link.LIB_ERROR)
        self.assertEqual(sdlp_link.mode_name(), "unloaded")

    # --- TM -----------------------------------------------------------------------------

    def test_tm_packets_come_out_and_are_logged(self):
        packets = [make_spp(0x100, b"\xAA" * 10), make_spp(0x101, b"\xBB" * 20), make_spp(0x102, b"\xCC")]
        (frame,) = tm_frames(packets)
        self.assertEqual(sdlp_link.decapsulate_tm(frame, host="127.0.0.1"), packets)
        (entry,) = read_log(self.log)
        self.assertEqual((entry["dir"], entry["result"], entry["host"]), ("TM", "OK", "127.0.0.1"))
        self.assertEqual(entry["in"], frame.hex())
        self.assertEqual(entry["out"].split(","), [p.hex() for p in packets])

    def test_bad_tm_frame_raises_and_logs(self):
        (frame,) = tm_frames([make_spp(0x100, b"x")])
        with self.assertRaises(sdlp_link.SdlpError) as ctx:
            sdlp_link.decapsulate_tm(frame[:-1], host="10.1.1.1")
        self.assertEqual(ctx.exception.name, "TM_SDLP_MY_INVALID_LENGTH")
        (entry,) = read_log(self.log)
        self.assertEqual((entry["result"], entry["out"], entry["host"]), ("FAIL", "-", "10.1.1.1"))

    # --- log ----------------------------------------------------------------------------

    def test_log_off(self):
        os.environ["SDLP_LOG"] = "off"
        sdlp_link.encapsulate_tc(make_spp(0x18, b"x"))
        self.assertFalse(self.log.exists())

    def test_unwritable_log_never_breaks_framing(self):
        os.environ["SDLP_LOG"] = "/nonexistent/dir/sdlp.log"
        packet = make_spp(0x18, b"x")
        self.assertEqual(sat_tc_extract_packet(sdlp_link.encapsulate_tc(packet)), packet)

    def test_lines_from_several_processes_never_interleave(self):
        ctx = multiprocessing.get_context("spawn")
        args = (str(self.log), os.environ["SDLS_LIB"], os.environ["SDLS_SA_DB"], 40)
        with ctx.Pool(4) as pool:
            pids = pool.starmap(_worker_log, [args] * 4)
        entries = read_log(self.log)
        self.assertEqual(len(entries), 160)
        self.assertTrue(all(e is not None and e["result"] == "OK" for e in entries))
        self.assertTrue(all(len(bytes.fromhex(e["in"])) == b.TM_FRAME_SIZE for e in entries))
        self.assertEqual({int(e["pid"]) for e in entries}, set(pids) if len(set(pids)) > 1 else {pids[0]})


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self._ctx = private_link()
        self.log = self._ctx.__enter__()
        self.spp = make_spp(0x18, b"\x01\x02")
        self.frame = sdlp_link.encapsulate_tc(self.spp, source="MiniCmdUtil")
        self.packets = [make_spp(0x100, b"\xAA" * 300), make_spp(0x101, b"\xBB")]
        sdlp_link.decapsulate_tm(tm_frames(self.packets)[0], host="127.0.0.1")
        with contextlib.suppress(sdlp_link.SdlpError):
            sdlp_link.decapsulate_tm(b"\x00" * 10, host="127.0.0.1")

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def run_monitor(self, *args):
        out = io.StringIO()
        sdlp_link.monitor(["--log", str(self.log), "--once", *args], out=out)
        return out.getvalue()

    def test_decoded_view(self):
        text = self.run_monitor()
        self.assertIn("TC OK", text)
        self.assertIn("before  Space Packet", text)
        self.assertIn("after   TC frame", text)
        self.assertIn("SCID 0x003", text)
        self.assertIn("after   packet 2", text)
        self.assertIn("TM FAIL", text)
        self.assertIn("TM_SDLP_MY_INVALID_LENGTH", text)
        self.assertIn(self.spp[:16].hex(" "), text)  # raw bytes shown

    def test_filters(self):
        self.assertNotIn("TM ", self.run_monitor("--dir", "TC"))
        fail_only = self.run_monitor("--fail")
        self.assertNotIn(" OK ", fail_only)
        self.assertIn("FAIL", fail_only)

    def test_hex_limit_and_no_hex(self):
        self.assertIn("more bytes", self.run_monitor("--hex-limit", "32"))
        self.assertNotIn("0000  ", self.run_monitor("--no-hex"))
        self.assertNotIn("more bytes", self.run_monitor("--hex-limit", "0"))

    def test_raw_prints_the_log_lines(self):
        self.assertEqual(self.run_monitor("--raw"), self.log.read_text())

    def test_follow_sees_new_lines_and_survives_truncation(self):
        seen = []
        gen = sdlp_link._follow(str(self.log), from_start=False, poll=0.02)
        thread = threading.Thread(target=lambda: [seen.append(line) for line in gen], daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertEqual(seen, [])  # existing lines skipped
        sdlp_link.encapsulate_tc(self.spp, source="after-start")
        self._wait(lambda: any("src=after-start" in line for line in seen))
        self.log.write_text("")  # truncated (log rotated by hand)
        sdlp_link.encapsulate_tc(self.spp, source="after-truncate")
        self._wait(lambda: any("src=after-truncate" in line for line in seen))

    def _wait(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.02)
        self.fail("monitor did not see the new line")


if __name__ == "__main__":
    unittest.main()
