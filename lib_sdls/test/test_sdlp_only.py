"""SDLP-only build (CMake SDLS_SDLP_ONLY=ON): frames only, laid out like the SDLP-only
satellite (Heeseung1123/cFS ci_lab and to_lab):

    TC  [Primary Header 5][Space Packet]        no Security Header, no MAC
    TM  [Primary Header 6][packets + idle]      no Security Header, no MAC

No SA DB is needed, and the satellite reference (an SDLP-only satellite) must exchange
frames with the library byte for byte.
"""

import ctypes
import pathlib
import shutil
import tempfile
import unittest

import sdls_binding as b
from sat_reference import SatTmFramer, make_spp, sat_tc_extract_packet, tc_header_fields

SDLP_ONLY = b.sdlp_only_build()
TC_DATA = b.TCTF_PRIHDR_LENGTH  # the packet follows the Primary Header
TC_MAX = b.SDLP_ONLY_TC_MAX_PACKET_LENGTH
TM_DATA = b.SDLP_ONLY_TM_DATA_OFFSET


def tm_frame(packets, **args):
    framer = SatTmFramer(sdlp_only=True, **args)
    for packet in packets:
        framer.forward_packet(packet)
    framer.send_frame()
    return framer.sent


@unittest.skipUnless(SDLP_ONLY, "SDLS build (see test_tc_sdlp.py / test_tm_sdlp.py)")
class SdlpOnlyTcTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sdls = b.Sdls()
        cls.sdls.tc_init()

    def test_mode_is_sdlp_only(self):
        self.assertEqual(self.sdls.mode, b.MODE_SDLP_ONLY)
        self.assertFalse(hasattr(self.sdls.lib, "SA_myOpen"), "SDLS code must not be built in")

    def test_no_security_header_and_no_trailer(self):
        packet = make_spp(0x18, bytes(range(30)))
        frame = self.sdls.tc_build_frame(b.SPI_SA00, packet)
        self.assertEqual(len(frame), b.TCTF_PRIHDR_LENGTH + len(packet))
        self.assertEqual(frame[TC_DATA:], packet)

    def test_noop_frame_byte_for_byte(self):
        """What the SDLP-only satellite CI expects for TO_LAB NOOP (13 bytes, length 12)."""
        frame = self.sdls.tc_build_frame(0, bytes.fromhex("1882c000000100a4"))
        self.assertEqual(frame[:4].hex(), "2003000c")
        self.assertEqual(frame[5:].hex(), "1882c000000100a4")

    def test_spi_argument_is_ignored(self):
        packet = make_spp(0x18, b"\x01\x02")
        a = self.sdls.tc_build_frame(b.SPI_SA00, packet)
        c = self.sdls.tc_build_frame(b.SPI_SA01, packet)
        self.assertEqual(a[:4] + a[5:], c[:4] + c[5:])  # only the sequence number differs

    def test_primary_header_as_before(self):
        packet = make_spp(0x18, b"\x00" * 5)
        frame = self.sdls.tc_build_frame(0, packet)
        hdr = tc_header_fields(frame)
        self.assertEqual((hdr["tfvn"], hdr["bypass"], hdr["ctlcmd"], hdr["scid"], hdr["vcid"]),
                         (b.TC_DUMMY_TFVN, b.TC_BYPASS_FLG, b.TC_CTLCMD_FLG, b.TC_DUMMY_SCID, b.TC_DUMMY_VCID))
        self.assertEqual(hdr["framelen"] + 1, len(frame))

    def test_satellite_takes_the_packet_out(self):
        for size in (1, 100, TC_MAX - b.TC_SPP_HDR_LENGTH):
            with self.subTest(size=size):
                packet = make_spp(0x18, bytes(i & 0xFF for i in range(size)))
                frame = self.sdls.tc_build_frame(0, packet)
                self.assertEqual(sat_tc_extract_packet(frame, sdlp_only=True), packet)
        self.assertEqual(len(frame), b.TCTF_MAX_FRAME_LENGTH)  # the biggest packet fills 1024 B

    def test_frame_errors_still_apply(self):
        with self.assertRaises(b.SdlsError) as ctx:
            self.sdls.tc_build_frame(0, make_spp(0x18, bytes(TC_MAX)))  # one byte too long
        self.assertEqual(ctx.exception.code, b.TC_INVALID_LENGTH)
        bad = bytearray(make_spp(0x18, bytes(10)))
        bad[5] = 0
        with self.assertRaises(b.SdlsError) as ctx:
            self.sdls.tc_build_frame(0, bytes(bad))
        self.assertEqual(ctx.exception.code, b.TC_INVALID_PACKET)

    def test_works_without_any_sa_db_but_needs_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy_path = pathlib.Path(tmp) / "libsdls_fresh.so"
            shutil.copy(b.find_library(), copy_path)
            fresh = b.Sdls(str(copy_path))
            packet = make_spp(0x18, b"\x00")
            buf = ctypes.create_string_buffer(64)
            build = lambda: fresh.lib.TC_SDLP_myBuildFrame(0, packet, len(packet), buf, 64)  # noqa: E731
            self.assertEqual(build(), b.TC_NOT_INITIALIZED)
            fresh.tc_init()
            self.assertEqual(build(), TC_DATA + len(packet))


@unittest.skipUnless(SDLP_ONLY, "SDLS build (see test_tm_sdlp.py)")
class SdlpOnlyTmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sdls = b.Sdls()

    def test_every_packet_comes_out_without_an_sa_db(self):
        packets = [make_spp(0x100 + i, bytes([i]) * (5 + 40 * i)) for i in range(6)]
        (frame,) = tm_frame(packets)
        self.assertEqual(TM_DATA, b.TMTF_PRIHDR_LENGTH)
        self.assertEqual(frame[TM_DATA : TM_DATA + len(packets[0])], packets[0])  # right after the Primary Header
        self.assertEqual(self.sdls.tm_extract(frame), (0, packets))

    def test_satellite_frame_byte_for_byte(self):
        """A frame as the SDLP-only satellite TO (to_lab, HDR 0 / TRL 0) sends it."""
        packet = bytes.fromhex("0880c0000009") + bytes(range(1, 11))
        frame = bytearray(b.TM_FRAME_SIZE)
        frame[0:6] = bytes.fromhex("003201001800")  # SCID 0x003, VCID 1, MC 1, packet service, FHP 0
        frame[6 : 6 + len(packet)] = packet
        idle = 6 + len(packet)
        frame[idle : idle + 3] = b"\x07\xff\xc0"
        frame[idle + 4 : idle + 6] = (b.TM_FRAME_SIZE - idle - 7).to_bytes(2, "big")
        self.assertEqual(self.sdls.tm_extract(bytes(frame)), (0, [packet]))

    def test_idle_packet_runs_to_the_frame_end(self):
        """No MAC: the satellite fills the data field up to byte 1786 with the idle packet."""
        packet = make_spp(0x101, bytes(9))
        (frame,) = tm_frame([packet])
        idle = TM_DATA + len(packet)
        self.assertEqual(frame[idle : idle + 2], b"\x07\xff")
        self.assertEqual(idle + int.from_bytes(frame[idle + 4 : idle + 6], "big") + 7, b.TM_FRAME_SIZE)
        self.assertEqual(self.sdls.tm_extract_packets(frame), [packet])

    def test_packet_that_fills_the_frame_exactly(self):
        packet = make_spp(0x104, bytes(b.TM_FRAME_SIZE - TM_DATA - b.TC_SPP_HDR_LENGTH))
        (frame,) = tm_frame([packet])
        self.assertEqual(frame[-1:], packet[-1:])
        self.assertEqual(self.sdls.tm_extract_packets(frame), [packet])

    def test_stream_over_several_frames(self):
        packets = [make_spp(0x200, bytes(300)) for _ in range(20)]
        frames = tm_frame(packets)
        self.assertGreater(len(frames), 1)
        out = [p for frame in frames for p in self.sdls.tm_extract_packets(frame)]
        self.assertEqual(out, packets)

    def test_same_frame_twice_is_fine(self):
        """No ARSN in SDLP-only: a repeated frame is just a repeated frame."""
        (frame,) = tm_frame([make_spp(0x102, b"y")])
        self.assertEqual(self.sdls.tm_extract_packets(frame), self.sdls.tm_extract_packets(frame))

    def test_frame_checks_still_apply(self):
        (frame,) = tm_frame([make_spp(0x103, b"z")])
        for bad, code in ((frame[:-1], b.TM_INVALID_LENGTH), (frame[:5] + b"\x01" + frame[6:], b.TM_INVALID_HEADER)):
            with self.subTest(code=code):
                with self.assertRaises(b.SdlsError) as ctx:
                    self.sdls.tm_extract(bad)
                self.assertEqual(ctx.exception.code, code)


if __name__ == "__main__":
    unittest.main()
