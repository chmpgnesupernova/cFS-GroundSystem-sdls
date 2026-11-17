"""SDLP-only build (CMake SDLS_SDLP_ONLY=ON): frames only, Security Header and Trailer zero.

The frame layout is the SDLS one ([PriHdr][SecHdr 14][data][Trailer 16]); only the
security fields are dummies. No SA DB is needed, and the satellite reference (an
SDLP-only satellite) must exchange frames with the library byte for byte.
"""

import ctypes
import pathlib
import shutil
import tempfile
import unittest

import sdls_binding as b
from sat_reference import SatTmFramer, make_spp, sat_tc_extract_packet, tc_header_fields

SDLP_ONLY = b.sdlp_only_build()
TC_DATA = b.TCTF_PRIHDR_LENGTH + b.SECHDR_LENGTH
TRL = b.TC_SDLS_TRL_LENGTH


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

    def test_security_header_and_trailer_are_zero(self):
        packet = make_spp(0x18, bytes(range(30)))
        frame = self.sdls.tc_build_frame(b.SPI_SA00, packet)
        self.assertEqual(len(frame), TC_DATA + len(packet) + TRL)
        self.assertEqual(frame[b.TCTF_PRIHDR_LENGTH:TC_DATA], bytes(b.SECHDR_LENGTH))
        self.assertEqual(frame[TC_DATA:-TRL], packet)  # in clear
        self.assertEqual(frame[-TRL:], bytes(TRL))

    def test_spi_argument_is_ignored(self):
        packet = make_spp(0x18, b"\x01\x02")
        a = self.sdls.tc_build_frame(b.SPI_SA00, packet)
        c = self.sdls.tc_build_frame(b.SPI_SA01, packet)
        self.assertEqual(a[b.TCTF_PRIHDR_LENGTH:], c[b.TCTF_PRIHDR_LENGTH:])  # only the sequence number differs

    def test_primary_header_as_before(self):
        packet = make_spp(0x18, b"\x00" * 5)
        frame = self.sdls.tc_build_frame(0, packet)
        hdr = tc_header_fields(frame)
        self.assertEqual((hdr["tfvn"], hdr["bypass"], hdr["ctlcmd"], hdr["scid"], hdr["vcid"]),
                         (b.TC_DUMMY_TFVN, b.TC_BYPASS_FLG, b.TC_CTLCMD_FLG, b.TC_DUMMY_SCID, b.TC_DUMMY_VCID))
        self.assertEqual(hdr["framelen"] + 1, len(frame))

    def test_satellite_takes_the_packet_out(self):
        for size in (1, 100, b.TC_MAX_PACKET_LENGTH - b.TC_SPP_HDR_LENGTH):
            with self.subTest(size=size):
                packet = make_spp(0x18, bytes(i & 0xFF for i in range(size)))
                self.assertEqual(sat_tc_extract_packet(self.sdls.tc_build_frame(0, packet)), packet)

    def test_frame_errors_still_apply(self):
        with self.assertRaises(b.SdlsError) as ctx:
            self.sdls.tc_build_frame(0, make_spp(0x18, bytes(b.TC_MAX_PACKET_LENGTH)))  # one byte too long
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
            self.assertEqual(build(), TC_DATA + len(packet) + TRL)


@unittest.skipUnless(SDLP_ONLY, "SDLS build (see test_tm_sdlp.py)")
class SdlpOnlyTmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sdls = b.Sdls()

    def test_every_packet_comes_out_without_an_sa_db(self):
        packets = [make_spp(0x100 + i, bytes([i]) * (5 + 40 * i)) for i in range(6)]
        (frame,) = tm_frame(packets)
        self.assertEqual(frame[b.TMTF_PRIHDR_LENGTH : b.TM_DATA_OFFSET], bytes(b.SECHDR_LENGTH))
        self.assertEqual(self.sdls.tm_extract(frame), (0, packets))

    def test_stream_over_several_frames(self):
        packets = [make_spp(0x200, bytes(300)) for _ in range(20)]
        frames = tm_frame(packets)
        self.assertGreater(len(frames), 1)
        out = [p for frame in frames for p in self.sdls.tm_extract_packets(frame)]
        self.assertEqual(out, packets)

    def test_security_fields_are_not_checked(self):
        """Dummy fields: whatever they hold, the frame is accepted and the SPI reported."""
        packet = make_spp(0x101, b"x")
        frame = bytearray(tm_frame([packet])[0])
        frame[b.TMTF_PRIHDR_LENGTH : b.TM_DATA_OFFSET] = bytes([0x12, 0x34]) + b"\xEE" * 12
        frame[-b.TM_SDLS_TRL_LENGTH :] = b"\x99" * b.TM_SDLS_TRL_LENGTH
        self.assertEqual(self.sdls.tm_extract(bytes(frame)), (0x1234, [packet]))

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
