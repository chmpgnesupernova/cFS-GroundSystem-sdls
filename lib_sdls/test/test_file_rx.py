"""lib_sdls/file_rx.py: a file sent down as FILE_DL segments, through TM frames that split
the segments (SatTmSegmentingFramer), rebuilt and saved by FileReceiver; the commands the
receiving station sends; and the stand-alone receiver (main) over UDP."""

import contextlib
import os
import pathlib
import random
import socket
import sys
import tempfile
import threading
import time
import unittest
import zlib

from sat_reference import SatTmSegmentingFramer, sat_tc_extract_packet
from test_sdlp_link import SDLP_ONLY, private_link

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import file_rx  # noqa: E402
import sdlp_link  # noqa: E402

SEG_DATA = 1024  # FILE_DL_SEGMENT_SIZE


def seg_packet(mid, seq, seg_type, dest_tag, offset, file_size, crc, data=b""):
    """One segment as FILE_DL_NewSegment builds it: 16 B TLM header + 16 B segment header."""
    body = (bytes(6) + bytes(4)  # time 6, spare 4
            + bytes([seg_type, dest_tag]) + len(data).to_bytes(2, "big") + offset.to_bytes(4, "big")
            + file_size.to_bytes(4, "big") + crc.to_bytes(4, "big") + data)
    return (mid.to_bytes(2, "big") + (0xC000 | (seq & 0x3FFF)).to_bytes(2, "big")
            + (len(body) - 1).to_bytes(2, "big") + body)


def file_segments(name, content, dest_tag=2, mid=file_rx.FILE_RELAY_SEG_TLM_MID):
    """START, DATA (1024 B each), END, as the satellite sends one file."""
    size, crc, seq, out = len(content), 0, 0, []
    out.append(seg_packet(mid, seq, 0, dest_tag, 0, size, 0, name.encode()))
    for off in range(0, size, SEG_DATA):
        chunk = content[off : off + SEG_DATA]
        crc = zlib.crc32(chunk, crc)
        seq += 1
        out.append(seg_packet(mid, seq, 1, dest_tag, off, size, crc, chunk))
    out.append(seg_packet(mid, seq + 1, 2, dest_tag, size, size, crc))
    return out


def hk(seq):
    return (0x08D0).to_bytes(2, "big") + (0xC000 | seq).to_bytes(2, "big") + (35).to_bytes(2, "big") + bytes(36)


def frames(packets, flush_every=5):
    f = SatTmSegmentingFramer(sdlp_only=SDLP_ONLY)
    for i, p in enumerate(packets, 1):
        f.forward_packet(p)
        if i % flush_every == 0:
            f.send_frame()
    f.send_frame()
    return f.sent


class Base(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(private_link())
        sdlp_link.reset_tm_streams()
        self.dir = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.events = []
        self.rx = file_rx.FileReceiver(self.dir, dest_tag=2, on_event=lambda e, r: self.events.append(e))
        self.content = random.Random(7).randbytes(150_000)

    def feed(self, frame_list, host="sat"):
        for frame in frame_list:
            for packet in sdlp_link.decapsulate_tm(frame, host=host):
                self.rx.handle_packet(packet)


class FileReceiverTest(Base):
    def test_file_through_split_segments(self):
        packets = file_segments("photo.jpg", self.content)
        for i in range(0, len(packets), 20):  # housekeeping in between, as on the satellite
            packets.insert(i, hk(i))
        self.feed(frames(packets))
        self.assertEqual(self.rx.state, file_rx.COMPLETE)
        self.assertEqual((self.dir / "photo.jpg").read_bytes(), self.content)
        self.assertFalse((self.dir / "photo.jpg.part").exists())
        self.assertEqual(self.rx.file_crc, zlib.crc32(self.content))
        self.assertEqual(self.events, ["start", "end", "complete"])
        self.assertTrue(self.rx.verified)

    def test_lost_frames_then_resent_data(self):
        packets = file_segments("big.bin", self.content)
        sent = frames(packets)
        self.feed(sent[:10] + sent[13:])  # 3 frames lost
        self.assertEqual(self.rx.state, file_rx.RECEIVING)
        gaps = self.rx.missing()
        self.assertTrue(gaps)
        self.assertTrue(self.rx.end_seen)
        # the satellite answers RESEND with DATA segments of the asked ranges
        resent = []
        for off, length in gaps:
            for o in range(off, off + length, SEG_DATA):
                n = min(SEG_DATA, off + length - o)
                resent.append(seg_packet(file_rx.FILE_RELAY_SEG_TLM_MID, 0, 1, 2, o, len(self.content), 0,
                                         self.content[o : o + n]))
        self.feed(frames(resent))
        self.assertEqual(self.rx.state, file_rx.COMPLETE)
        self.assertEqual((self.dir / "big.bin").read_bytes(), self.content)

    def test_end_lost_file_kept_unverified(self):
        self.feed(frames(file_segments("noend.bin", self.content)[:-1]))
        self.assertEqual(self.rx.state, file_rx.RECEIVING)
        self.assertFalse(self.rx.end_seen)
        self.assertTrue(self.rx.finish_without_end())
        self.assertEqual((self.rx.state, self.rx.verified), (file_rx.COMPLETE, False))
        self.assertEqual((self.dir / "noend.bin").read_bytes(), self.content)

    def test_other_dest_tag_and_other_mids_are_ignored(self):
        self.feed(frames(file_segments("theirs.bin", self.content, dest_tag=3)
                         + file_segments("dl.bin", self.content, mid=file_rx.FILE_DL_SEG_TLM_MID)))
        self.assertEqual(self.rx.state, file_rx.IDLE)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_crc_mismatch_keeps_the_part_file(self):
        packets = file_segments("bad.bin", self.content)
        end = bytearray(packets[-1])
        end[28:32] = (zlib.crc32(self.content) ^ 1).to_bytes(4, "big")
        packets[-1] = bytes(end)
        self.feed(frames(packets))
        self.assertEqual(self.rx.state, file_rx.CRC_MISMATCH)
        self.assertTrue((self.dir / "bad.bin.part").exists())
        self.assertFalse((self.dir / "bad.bin").exists())

    def test_name_cannot_leave_the_output_directory(self):
        self.assertEqual(file_rx.safe_name("/cf/relay/../../etc/passwd"), "passwd")
        self.assertEqual(file_rx.safe_name("../"), "file.bin")
        self.assertEqual(file_rx.safe_name("x.bin\0junk"), "x.bin")
        self.feed(frames(file_segments("/cf/relay/../up.bin", self.content[:3000])))
        self.assertTrue((self.dir / "up.bin").exists())

    def test_small_file_and_empty_last_segment_rules(self):
        self.feed(frames(file_segments("one.txt", b"hello")))
        self.assertEqual((self.dir / "one.txt").read_bytes(), b"hello")
        self.assertIn("one.txt: 5/5 B (100.0%) COMPLETE", self.rx.status_line())

    def test_parse_segment_rejects_other_packets(self):
        self.assertIsNone(file_rx.parse_segment(hk(1)))
        self.assertIsNone(file_rx.parse_segment(b"\x08\xc3"))
        seg = file_rx.parse_segment(seg_packet(0x08C3, 0, 1, 2, 2048, 9000, 0xDEADBEEF, b"abc"))
        self.assertEqual((seg.type_name, seg.dest_tag, seg.offset, seg.file_size, seg.crc32, seg.data),
                         ("DATA", 2, 2048, 9000, 0xDEADBEEF, b"abc"))


class CommandTest(unittest.TestCase):
    @staticmethod
    def xor(packet):
        x = 0
        for byte in packet:
            x ^= byte
        return x

    def test_done(self):
        p = file_rx.done_command(0x12345678)
        self.assertEqual(p[:7].hex(), "18c0c000000503")
        self.assertEqual(p[8:], bytes.fromhex("12345678"))
        self.assertEqual(len(p), 12)
        self.assertEqual(self.xor(p), 0xFF)

    def test_resend(self):
        p = file_rx.resend_command(4096, 1024)
        self.assertEqual((len(p), p[6], p[8:]), (16, 2, bytes.fromhex("00001000 00000400")))
        self.assertEqual(self.xor(p), 0xFF)

    def test_add_and_remove_dest(self):
        p = file_rx.add_dest_command("192.168.0.20", 1245)
        self.assertEqual((len(p), p[:2], p[6]), (26, b"\x18\x80", 7))
        self.assertEqual(p[8:24], b"192.168.0.20".ljust(16, b"\0"))
        self.assertEqual(p[24:], (1245).to_bytes(2, "big"))
        self.assertEqual(file_rx.remove_dest_command("1.2.3.4", 1)[6], 8)
        self.assertEqual(self.xor(p), 0xFF)
        with self.assertRaises(ValueError):
            file_rx.add_dest_command("1234.1234.1234.1234", 1)


class StandaloneReceiverTest(Base):
    """file_rx.main as ground station B: TM frames over UDP in, the file saved, and the
    commands (ADD_DEST, RESEND, DONE, REMOVE_DEST) sent to a stand-in satellite."""

    def start_station(self, extra=()):
        """Runs main in a thread; returns the TM port it listens on."""
        self.sat_tc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sat_tc.bind(("127.0.0.1", 0))
        self.addCleanup(self.sat_tc.close)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        tm_port = probe.getsockname()[1]
        probe.close()
        old_port = file_rx.TC_PORT
        file_rx.TC_PORT = self.sat_tc.getsockname()[1]
        self.addCleanup(setattr, file_rx, "TC_PORT", old_port)
        self.result = {}
        args = ["--port", str(tm_port), "--bind", "127.0.0.1", "--tag", "2", "--out", str(self.dir),
                "--sat", "127.0.0.1", "--my-ip", "127.0.0.1", "--once", *extra]
        quiet = open(os.devnull, "w")
        self.addCleanup(quiet.close)

        def run():
            with contextlib.redirect_stdout(quiet):
                self.result["rc"] = file_rx.main(args)

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        time.sleep(0.2)  # let it bind
        return tm_port

    @staticmethod
    def send(frame_list, port):
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for frame in frame_list:
            tx.sendto(frame, ("127.0.0.1", port))
            time.sleep(0.002)  # the satellite sends about 10 frames a second
        tx.close()

    def command(self, timeout=5.0):
        """Next command the station sent, as a Space Packet; None after `timeout` quiet."""
        self.sat_tc.settimeout(timeout)
        try:
            return sat_tc_extract_packet(self.sat_tc.recvfrom(2048)[0])
        except socket.timeout:
            return None

    def finish(self, name):
        self.worker.join(10)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.result.get("rc"), 0)
        self.assertEqual((self.dir / name).read_bytes(), self.content)

    def test_registers_receives_and_sends_done(self):
        tm_port = self.start_station(["--register"])
        self.assertEqual(self.command(), file_rx.add_dest_command("127.0.0.1", tm_port))
        self.send(frames(file_segments("relay.bin", self.content)), tm_port)
        self.assertEqual(self.command(), file_rx.done_command(zlib.crc32(self.content)))
        self.assertEqual(self.command(), file_rx.remove_dest_command("127.0.0.1", tm_port))
        self.finish("relay.bin")

    def test_asks_for_lost_ranges_then_sends_done(self):
        tm_port = self.start_station(["--resend-after", "0.3"])
        sent = frames(file_segments("lossy.bin", self.content))
        self.send(sent[:20] + sent[24:], tm_port)  # 4 frames lost on the way down

        resends = [self.command()]
        while (packet := self.command(timeout=0.2)) is not None:
            resends.append(packet)
        self.assertTrue(all(p[:2] == b"\x18\xc0" and p[6] == file_rx.FILE_DL_RESEND_CC for p in resends))

        # the satellite answers every RESEND with DATA segments of that range
        f = SatTmSegmentingFramer(sdlp_only=SDLP_ONLY)
        f.mc_frame_count = sent[-1][2]  # its frame count goes on
        for p in resends:
            off, length = int.from_bytes(p[8:12], "big"), int.from_bytes(p[12:16], "big")
            for o in range(off, off + length, SEG_DATA):
                n = min(SEG_DATA, off + length - o)
                f.forward_packet(seg_packet(file_rx.FILE_RELAY_SEG_TLM_MID, 0, 1, 2, o, len(self.content), 0,
                                            self.content[o : o + n]))
        f.send_frame()
        self.send(f.sent, tm_port)

        done = self.command()
        while done is not None and done[6] == file_rx.FILE_DL_RESEND_CC:  # a round sent before the answer
            done = self.command()
        self.assertEqual(done, file_rx.done_command(zlib.crc32(self.content)))
        self.finish("lossy.bin")


if __name__ == "__main__":
    unittest.main()
