"""TM receive stream (TM_SDLP_myRx*, sdlp_link.decapsulate_tm): packets that run over frame
boundaries, joined with the First Header Pointer, as the current satellite TO_LAB frames
them (sat_reference.SatTmSegmentingFramer, byte for byte the same as to_lab 476f4dc).
Runs in both builds (SDLS build: SA10 frames)."""

import contextlib
import ctypes
import unittest

import sdls_binding as b
from sat_reference import SatTmSegmentingFramer
from test_sdlp_link import SDLP_ONLY, private_link, read_log

import sdlp_link

FHP_NONE = 0x7FF


def spp(mid, data, seq=0):
    """Telemetry Space Packet with the full 16 bit MID as its first two bytes."""
    return (mid.to_bytes(2, "big") + (0xC000 | (seq & 0x3FFF)).to_bytes(2, "big")
            + (len(data) - 1).to_bytes(2, "big") + bytes(data))


def framer():
    return SatTmSegmentingFramer(sdlp_only=SDLP_ONLY)  # SDLS build: SA10, the always open EP SA


def frames_of(packets, flush_every=None):
    f = framer()
    for i, packet in enumerate(packets, 1):
        f.forward_packet(packet)
        if flush_every and i % flush_every == 0:
            f.send_frame()
    f.send_frame()
    return f.sent


def fhp(frame):
    return ((frame[4] & 0x07) << 8) | frame[5]


def segment(i, size=1056):
    return spp(0x08C3, bytes([i & 0xFF]) * (size - 6), seq=i)


class StreamTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.log = stack.enter_context(private_link())
        sdlp_link.reset_tm_streams()

    def receive(self, frames, host="sat"):
        out = []
        for frame in frames:
            out += sdlp_link.decapsulate_tm(frame, host=host)
        return out

    def test_segments_that_run_over_frames(self):
        """1056 B file segments with HK in between, flushed every 5 packets as TO does."""
        packets = []
        for i in range(12):
            packets.append(segment(i))
            if i % 3 == 0:
                packets.append(spp(0x08D0, b"\xAA" * 30, seq=i))
        frames = frames_of(packets, flush_every=5)
        self.assertTrue(any(fhp(f) not in (0, FHP_NONE) for f in frames), "test needs continued packets")
        self.assertEqual(self.receive(frames), packets)
        self.assertTrue(all(e["result"] == "OK" and "drop" not in e for e in read_log(self.log)))

    def test_packet_longer_than_a_frame(self):
        """A frame holding only the middle of a packet has FHP 0x7FF."""
        big = spp(0x08C3, bytes(range(256)) * 16)  # 4102 B: over two whole data fields
        packets = [spp(0x0880, b"a" * 10), big, spp(0x0881, b"b" * 10)]
        frames = frames_of(packets)
        self.assertIn(FHP_NONE, [fhp(f) for f in frames])
        self.assertEqual(self.receive(frames), packets)

    def test_header_split_over_two_frames(self):
        f = framer()
        first = spp(0x0880, bytes(f.data_field_length - 6 - 3))  # leaves 3 bytes in frame 1
        second = spp(0x0881, b"split header" * 5)
        for p in (first, second):
            f.forward_packet(p)
        f.send_frame()
        self.assertEqual(len(f.sent), 2)
        self.assertEqual(fhp(f.sent[1]), FHP_NONE if len(second) - 3 > f.data_field_length else len(second) - 3)
        self.assertEqual(self.receive(f.sent), [first, second])

    def test_short_idle_split_into_the_next_frame(self):
        """Fewer than 7 octets left at a flush: the 7 octet idle packet runs on."""
        f = framer()
        first = spp(0x0880, bytes(f.data_field_length - 6 - 4))  # 4 octets left
        f.forward_packet(first)
        f.send_frame()
        second = spp(0x0881, b"after the idle")
        f.forward_packet(second)
        f.send_frame()
        self.assertEqual(fhp(f.sent[1]), 3)  # the idle's 3 last octets come first
        self.assertEqual(self.receive(f.sent), [first, second])

    def test_lost_frame_drops_only_the_packet_it_cut(self):
        packets = [segment(i) for i in range(8)]
        frames = frames_of(packets)
        self.assertGreater(len(frames), 3)
        received = self.receive(frames[:2] + frames[3:])  # frame 3 (MC 3) never arrives
        # Lost with the frame: the packet that ran into it, any packet inside it, and the
        # one that started in it (its start is gone). At most 3 of these 1056 B packets.
        lost_in = [packets.index(p) for p in packets if p not in received]
        self.assertTrue(1 <= len(lost_in) <= 3, lost_in)
        self.assertEqual([p for p in packets if p in received], received)  # rest in order
        entry = [e for e in read_log(self.log) if "lost" in e][0]
        self.assertEqual(entry["lost"], "1")
        self.assertGreaterEqual(int(entry.get("drop", 0)), 1)

    def test_senders_have_their_own_streams(self):
        a = frames_of([segment(i) for i in range(5)])
        c = frames_of([segment(100 + i) for i in range(5)])
        out_a, out_c = [], []
        for fa, fc in zip(a, c):  # interleaved, as from two satellites
            out_a += sdlp_link.decapsulate_tm(fa, host="10.0.0.1")
            out_c += sdlp_link.decapsulate_tm(fc, host="10.0.0.2")
        self.assertEqual(out_a, [segment(i) for i in range(5)])
        self.assertEqual(out_c, [segment(100 + i) for i in range(5)])

    def test_first_frame_heard_mid_packet(self):
        """Joining late: the bytes before the first header are skipped, the rest is read."""
        packets = [segment(i) for i in range(6)]
        frames = frames_of(packets)
        received = self.receive(frames[1:])
        self.assertEqual(received, packets[len(packets) - len(received):])
        self.assertGreaterEqual(len(received), 3)

    def test_bad_first_header_pointer_is_refused(self):
        frame = bytearray(frames_of([segment(1)])[0])
        frame[4] = (frame[4] & 0xF8) | 0x07
        frame[5] = 0xF0  # 0x7F0: past the data field, not a special value
        with self.assertRaises(sdlp_link.SdlpError) as ctx:
            sdlp_link.decapsulate_tm(bytes(frame), host="sat")
        self.assertEqual(ctx.exception.name, "TM_SDLP_MY_INVALID_HEADER")

    def test_garbage_header_resyncs_at_the_next_frame(self):
        packets = [spp(0x0880 + i, bytes(300)) for i in range(12)]
        frames = [bytearray(f) for f in frames_of(packets)]
        start = (SatTmSegmentingFramer(sdlp_only=SDLP_ONLY).data_field_offset + fhp(frames[0]))
        frames[0][start] = 0xE0  # first packet header: version 7
        received = self.receive([bytes(f) for f in frames])
        self.assertNotIn(packets[0], received)
        later = [p for p in packets if p in received]
        self.assertEqual(received, later)
        self.assertGreater(len(later), 5)  # everything from frame 2 on

    def test_old_style_frames_still_work(self):
        """Frames whose packets all start and end inside them (FHP 0)."""
        from sat_reference import SatTmFramer  # noqa: PLC0415

        f = SatTmFramer(sdlp_only=SDLP_ONLY)
        packets = [spp(0x0880, bytes(500)) for _ in range(7)]
        for p in packets:
            f.forward_packet(p)
        f.send_frame()
        self.assertEqual(self.receive(f.sent), packets)


class RxApiTest(unittest.TestCase):
    """The C stream API directly."""

    @classmethod
    def setUpClass(cls):
        cls.sdls = b.Sdls()
        lib = cls.lib = cls.sdls.lib
        p = ctypes.c_void_p
        lib.TM_SDLP_myRxSize.restype = ctypes.c_size_t
        lib.TM_SDLP_myRxInit.argtypes = [p]
        lib.TM_SDLP_myRxNextPacket.argtypes = [p, ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        lib.TM_SDLP_myRxNextPacket.restype = ctypes.c_int32
        lib.TM_SDLP_myRxPutFrame.argtypes = [p, p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint16)]
        lib.TM_SDLP_myRxPutFrame.restype = ctypes.c_int32

    def test_size_and_no_frame(self):
        self.assertGreater(self.lib.TM_SDLP_myRxSize(), 65542)  # holds the largest packet
        rx = ctypes.create_string_buffer(self.lib.TM_SDLP_myRxSize())
        self.lib.TM_SDLP_myRxInit(rx)
        ptr = ctypes.POINTER(ctypes.c_ubyte)()
        self.assertEqual(self.lib.TM_SDLP_myRxNextPacket(rx, ctypes.byref(ptr)), -7)  # TM_SDLP_MY_NO_FRAME
        self.assertEqual(self.lib.TM_SDLP_myRxNextPacket(None, ctypes.byref(ptr)), b.TM_INVALID_ARGUMENT)

    def test_wrong_size_frame(self):
        rx = ctypes.create_string_buffer(self.lib.TM_SDLP_myRxSize())
        self.lib.TM_SDLP_myRxInit(rx)
        frame = ctypes.create_string_buffer(100)
        self.assertEqual(self.lib.TM_SDLP_myRxPutFrame(rx, frame, 100, None), b.TM_INVALID_LENGTH)


if __name__ == "__main__":
    unittest.main()
