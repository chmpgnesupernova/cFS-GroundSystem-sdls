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
MODE = "SDLP-only" if SDLP_ONLY else "SDLS"
TC_MAX_PACKET = b.SDLP_ONLY_TC_MAX_PACKET_LENGTH if SDLP_ONLY else b.TC_MAX_PACKET_LENGTH
TC_HDR, TC_TRL, TM_HDR, TM_TRL = b.frame_layout(SDLP_ONLY)
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
        self.assertEqual(entry["mode"], MODE)
        self.assertEqual(len(frame), b.TCTF_PRIHDR_LENGTH + TC_HDR + len(packet) + TC_TRL)
        self.assertEqual((entry["in"], entry["out"]), (packet.hex(), frame.hex()))
        self.assertEqual(int(entry["pid"]), os.getpid())

    def test_tc_failure_raises_logs_and_returns_no_frame(self):
        packet = make_spp(0x18, bytes(TC_MAX_PACKET))  # one byte too long
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

    def test_a_blank_line_follows_each_message(self):
        for args in ((), ("--no-hex",), ("--color", "always")):
            with self.subTest(args=args):
                blocks = self.run_monitor(*args).split("\n\n")
                self.assertEqual(blocks[-1], "")  # the output ends with the blank line
                messages = [blk for blk in blocks[:-1] if not blk.startswith("Legend:")]
                self.assertEqual(len(messages), 3)  # TC OK, TM OK, TM FAIL
                for blk in messages:
                    self.assertIn(" pid ", blk.splitlines()[0])  # each block starts at its head line

    def test_raw_prints_the_log_lines(self):
        self.assertEqual(self.run_monitor("--raw"), self.log.read_text())

    def test_colors_mark_each_frame_field(self):
        """Every field run (split at the 16 byte rows of the dump) comes in its color."""
        text = self.run_monitor("--color", "always")
        c, end = sdlp_link.COLORS, sdlp_link.RESET
        self.assertIn("Legend:", text)

        def runs(data, regions):
            for start, stop, kind in regions:
                for row in range(start - start % 16, stop, 16):
                    lo, hi = max(start, row), min(stop, row + 16)
                    if lo < hi:
                        yield kind, data[lo:hi]

        size = len(self.frame)
        if SDLP_ONLY:  # [PriHdr 5][SPP]
            regions = [(0, 5, "prihdr"), (5, size, "data")]
        else:
            regions = [(0, 5, "prihdr"), (5, 7, "spi"), (7, 19, "iv"), (19, size - 16, "data"), (size - 16, size, "mac")]
        self.assertEqual(sdlp_link.tc_frame_regions(size, MODE), regions)
        for kind, part in [*runs(self.frame, regions), *runs(self.spp, sdlp_link.spp_regions(len(self.spp)))]:
            with self.subTest(kind=kind, part=part.hex()):
                self.assertIn(f"{c[kind]}{part.hex(' ')}{end}", text)
        self.assertEqual(sdlp_link.spp_regions(len(self.spp))[1][2], "data")
        self.assertEqual(c["data"], "\033[97m")  # the data field is white
        self.assertIn(f"{c['fail']}FAIL", text)

    def test_no_color_unless_on_a_terminal_or_asked(self):
        self.assertNotIn("\033", self.run_monitor())  # StringIO is not a terminal
        self.assertNotIn("\033", self.run_monitor("--color", "never"))
        self.assertNotIn("\033", self.run_monitor("--color", "always", "--raw"))

    def test_cut_frame_still_shows_its_mac(self):
        text = self.run_monitor("--dir", "TM", "--hex-limit", "32")
        if SDLP_ONLY:  # no MAC: only the head is shown
            self.assertIn(f"{b.TM_FRAME_SIZE - 32} more bytes", text)
            self.assertIn("no MAC", text)
            return
        self.assertIn(f"{b.TM_FRAME_SIZE - 48} bytes not shown", text)
        self.assertIn(f"{b.TM_FRAME_SIZE - 16:04x}  " + "00 " * 15 + "00", text)  # the 16 MAC bytes

    def test_layout_follows_the_mode_in_the_log_line(self):
        """The monitor draws each line with that line's mode, whatever this build is."""
        tc_only, tc_sdls = sdlp_link.tc_frame_regions(20, "SDLP-only"), sdlp_link.tc_frame_regions(50, "SDLS")
        self.assertEqual(tc_only, [(0, 5, "prihdr"), (5, 20, "data")])
        self.assertEqual([k for *_, k in tc_sdls], ["prihdr", "spi", "iv", "data", "mac"])
        tm_only = sdlp_link.tm_frame_regions(b.TM_FRAME_SIZE, "SDLP-only")
        self.assertEqual(tm_only, [(0, 6, "prihdr"), (6, b.TM_FRAME_SIZE, "data")])
        self.assertEqual(sdlp_link.tm_frame_regions(b.TM_FRAME_SIZE, "SDLS")[-1], (1770, 1786, "mac"))
        frame = bytes.fromhex("2003000c00" "1882c000000100a4")
        self.assertIn("no Security Header no MAC", sdlp_link.decode_tc_frame(frame, mode="SDLP-only"))
        self.assertIn("(13 B, too short for a TC frame)", sdlp_link.decode_tc_frame(frame, mode="SDLS"))
        self.assertEqual(sdlp_link.layout("garbage"), sdlp_link.layout(sdlp_link.MODE_SDLS))

    def test_short_frame_is_shown_whole(self):
        lines = sdlp_link._hexdump(self.frame, 32, tail=16)
        self.assertFalse(any("not shown" in line or "more bytes" in line for line in lines))
        self.assertEqual(len(lines), (len(self.frame) + 15) // 16)

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
