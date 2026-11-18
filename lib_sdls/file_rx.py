#!/usr/bin/env python3
"""Receives a file sent down by the satellite FILE_DL app and saves it.

FILE_DL sends a file as segments, one Space Packet each (apps/file_dl/README.md 3.1):

    0x08C0 downlink segments (DestTag 0)      0x08C3 relay segments (DestTag from relay START)

    byte 0-15   cFS telemetry header
    byte 16     Type        0 START, 1 DATA, 2 END
    byte 17     DestTag     ground station the file is for
    byte 18-19  DataLength  bytes in Data
    byte 20-23  Offset      DATA: byte offset in the file
    byte 24-27  FileSize
    byte 28-31  Crc32       END: CRC-32 (zlib) of the whole file
    byte 32-    Data        START: file name, DATA: file bytes, END: empty
    (all big endian)

FileReceiver puts DATA at its offset in <out>/<name>.part, keeps the missing byte ranges,
and once every byte is there and the CRC equals END's, renames it to <out>/<name>.
The packets come from sdlp_link.decapsulate_tm, which joins segments that run over a TM
frame boundary (First Header Pointer).

Commands the receiving station sends (TC, through sdlp_link.encapsulate_tc):
    TO_LAB  0x1880 CC 7 ADD_DEST / CC 8 REMOVE_DEST   get / stop the TM frames on own port
    FILE_DL 0x18C0 CC 2 RESEND(offset, length)        ask for a missing range again
    FILE_DL 0x18C0 CC 3 DONE(crc32)                   file received: the satellite closes it

Command line, a stand-alone receiving station (ground station B of the relay demo):

    python3 lib_sdls/file_rx.py --port 1245 --tag 2 --out received \\
        --sat 127.0.0.1 --my-ip 127.0.0.1 --register

    --port      UDP port the TM frames arrive on (1235 for the main destination)
    --tag       keep only segments with this DestTag (default: any)
    --mid       segment MID: 0x08C3 relay (default) or 0x08C0 downlink
    --sat       satellite address: send RESEND for missing ranges (after --resend-after quiet
                seconds) and DONE when complete
    --register  with --sat: ADD_DEST (my-ip, port) at start, REMOVE_DEST at the end
    --once      stop after the first complete file
    --dl-loss   test only: drop this fraction of received frames (exercises RESEND)
"""

import argparse
import os
import random
import socket
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdlp_link  # noqa: E402

# ---------------------------------------------------------------- packet layouts

FILE_DL_SEG_TLM_MID = 0x08C0
FILE_RELAY_SEG_TLM_MID = 0x08C3
SEGMENT_MIDS = (FILE_DL_SEG_TLM_MID, FILE_RELAY_SEG_TLM_MID)

TLM_HDR_LENGTH = 16  # cFS telemetry header: primary 6 + time 6 + spare 4
SEG_HDR_LENGTH = 16
TYPE_START, TYPE_DATA, TYPE_END = 0, 1, 2
TYPE_NAMES = {TYPE_START: "START", TYPE_DATA: "DATA", TYPE_END: "END"}

FILE_DL_CMD_MID = 0x18C0
FILE_DL_RESEND_CC = 2
FILE_DL_DONE_CC = 3
FILE_DL_MAX_RESENDS = 64  # ranges the satellite can queue

TO_LAB_CMD_MID = 0x1880
TO_ADD_DEST_CC = 7
TO_REMOVE_DEST_CC = 8

TC_PORT = 1234
CMD_GAP = 0.02  # seconds between commands (README 5.6)


def packet_mid(packet):
    return int.from_bytes(packet[0:2], "big") if len(packet) >= 2 else None


@dataclass
class Segment:
    mid: int
    type: int
    dest_tag: int
    offset: int
    file_size: int
    crc32: int
    data: bytes

    @property
    def type_name(self):
        return TYPE_NAMES.get(self.type, f"type {self.type}")


def parse_segment(packet):
    """Space Packet -> Segment, or None if it is not a well formed FILE_DL segment."""
    mid = packet_mid(packet)
    if mid not in SEGMENT_MIDS or len(packet) < TLM_HDR_LENGTH + SEG_HDR_LENGTH:
        return None
    h = packet[TLM_HDR_LENGTH : TLM_HDR_LENGTH + SEG_HDR_LENGTH]
    data_length = int.from_bytes(h[2:4], "big")
    data = packet[TLM_HDR_LENGTH + SEG_HDR_LENGTH :]
    if len(data) < data_length:
        return None
    return Segment(mid=mid, type=h[0], dest_tag=h[1], offset=int.from_bytes(h[4:8], "big"),
                   file_size=int.from_bytes(h[8:12], "big"), crc32=int.from_bytes(h[12:16], "big"),
                   data=bytes(data[:data_length]))


FILE_UL_STATUS_TLM_MID = 0x08C1
FILE_RELAY_STATUS_TLM_MID = 0x08C2
STATUS_MIDS = (FILE_UL_STATUS_TLM_MID, FILE_RELAY_STATUS_TLM_MID)
UL_STATES = ("IDLE", "RECEIVING", "VERIFYING", "COMPLETE", "FAILED")
STATUS_MAX_GAPS = 32


def parse_status(packet):
    """Upload / relay status packet (0x08C1 / 0x08C2, README 3.2) -> dict, or None.

    state, relay, dest_tag, size, received, expected_crc, file_crc, total_gaps, name,
    gaps [(offset, length)] (at most 32: the first missing ranges)."""
    if packet_mid(packet) not in STATUS_MIDS or len(packet) < TLM_HDR_LENGTH + 88:
        return None
    p = packet[TLM_HDR_LENGTH:]
    u32 = lambda i: int.from_bytes(p[i : i + 4], "big")  # noqa: E731
    gap_count = min(p[3], STATUS_MAX_GAPS, max(0, (len(p) - 88) // 8))
    return {
        "mid": packet_mid(packet),
        "state": UL_STATES[p[0]] if p[0] < len(UL_STATES) else f"state {p[0]}",
        "relay": p[1],
        "dest_tag": p[2],
        "size": u32(4),
        "received": u32(8),
        "expected_crc": u32(12),
        "file_crc": u32(16),
        "total_gaps": u32(20),
        "name": p[24:88].split(b"\0", 1)[0].decode("utf-8", "replace"),
        "gaps": [(u32(88 + 8 * k), u32(92 + 8 * k)) for k in range(gap_count)],
    }


def command(mid, cc, payload=b"", seq=0):
    """cFS command Space Packet: 8 byte header (checksum: XOR of all bytes is 0xFF)."""
    packet = bytearray(mid.to_bytes(2, "big") + (0xC000 | (seq & 0x3FFF)).to_bytes(2, "big")
                       + (len(payload) + 1).to_bytes(2, "big") + bytes([cc, 0]) + payload)
    checksum = 0xFF
    for byte in packet:
        checksum ^= byte
    packet[7] = checksum
    return bytes(packet)


def done_command(crc32):
    return command(FILE_DL_CMD_MID, FILE_DL_DONE_CC, crc32.to_bytes(4, "big"))


def resend_command(offset, length):
    return command(FILE_DL_CMD_MID, FILE_DL_RESEND_CC, offset.to_bytes(4, "big") + length.to_bytes(4, "big"))


def _dest_payload(ip, port):
    raw = ip.encode()
    if len(raw) > 15:
        raise ValueError(f"IP address too long: {ip}")
    return raw.ljust(16, b"\0") + port.to_bytes(2, "big")


def add_dest_command(ip, port):
    return command(TO_LAB_CMD_MID, TO_ADD_DEST_CC, _dest_payload(ip, port))


def remove_dest_command(ip, port):
    return command(TO_LAB_CMD_MID, TO_REMOVE_DEST_CC, _dest_payload(ip, port))


# ---------------------------------------------------------------- byte ranges


class Ranges:
    """Sorted, merged [start, end) ranges of received bytes."""

    def __init__(self):
        self.items = []

    def add(self, start, end):
        if end <= start:
            return
        merged = []
        for s, e in self.items:
            if e < start or s > end:
                merged.append((s, e))
            else:
                start, end = min(s, start), max(e, end)
        merged.append((start, end))
        self.items = sorted(merged)

    def total(self):
        return sum(e - s for s, e in self.items)

    def missing(self, size):
        """[(offset, length)] not yet received in [0, size)."""
        gaps, pos = [], 0
        for s, e in self.items:
            if s > pos:
                gaps.append((pos, s - pos))
            pos = max(pos, e)
        if pos < size:
            gaps.append((pos, size - pos))
        return gaps


# ---------------------------------------------------------------- receiver

IDLE, RECEIVING, COMPLETE, CRC_MISMATCH = "IDLE", "RECEIVING", "COMPLETE", "CRC_MISMATCH"


def safe_name(name):
    """File name only: no directories, no '..', no NUL; empty -> 'file.bin'."""
    base = os.path.basename(name.replace("\\", "/").split("\0", 1)[0]).strip()
    return base if base not in ("", ".", "..") else "file.bin"


class FileReceiver:
    """Rebuilds files from FILE_DL segments.

    out_dir  : where files are written (created if missing)
    dest_tag : keep only segments with this DestTag; None keeps all
    mids     : segment MIDs to use
    on_event : called with (event, receiver) for "start", "end", "complete", "crc_mismatch"

    One file at a time, as the satellite sends them. A new START closes the file before it
    (its .part file stays if it was not complete)."""

    def __init__(self, out_dir, dest_tag=None, mids=(FILE_RELAY_SEG_TLM_MID,), on_event=None):
        self.out_dir = Path(out_dir)
        self.dest_tag = dest_tag
        self.mids = tuple(mids)
        self.on_event = on_event
        self.state = IDLE
        self.name = None
        self.size = 0
        self.end_crc = None  # CRC from the END segment
        self.file_crc = None  # CRC of the rebuilt file
        self.verified = False  # COMPLETE and the CRC matched END (False: END never came)
        self.ranges = Ranges()
        self.path = None
        self.part_path = None
        self._fh = None
        self.last_segment_time = None

    # --- state ---

    @property
    def received(self):
        return self.ranges.total()

    def missing(self):
        return self.ranges.missing(self.size) if self.state != IDLE else []

    @property
    def end_seen(self):
        return self.end_crc is not None

    def status_line(self):
        if self.state == IDLE:
            return "idle"
        pct = 100.0 * self.received / self.size if self.size else 100.0
        text = f"{self.name}: {self.received}/{self.size} B ({pct:.1f}%) {self.state}"
        gaps = self.missing()
        if gaps:
            text += f", {len(gaps)} missing range(s)"
        if self.end_seen:
            text += f", END crc {self.end_crc:08x}"
        return text

    # --- input ---

    def handle_packet(self, packet):
        """Feeds one Space Packet. Returns the Segment it used, or None (not ours)."""
        seg = parse_segment(packet)
        if seg is None or seg.mid not in self.mids:
            return None
        if self.dest_tag is not None and seg.dest_tag != self.dest_tag:
            return None
        self.last_segment_time = time.monotonic()
        if seg.type == TYPE_START:
            self._start(seg)
        elif seg.type == TYPE_DATA:
            self._data(seg)
        elif seg.type == TYPE_END:
            self._end(seg)
        return seg

    def _emit(self, event):
        if self.on_event:
            self.on_event(event, self)

    def _start(self, seg):
        name = safe_name(seg.data.decode("utf-8", "replace"))
        if self.state == RECEIVING and name == self.name and seg.file_size == self.size:
            return  # the same START again
        self.close()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.name, self.size = name, seg.file_size
        self.path = self.out_dir / name
        self.part_path = self.out_dir / (name + ".part")
        self.ranges = Ranges()
        self.end_crc = self.file_crc = None
        self.verified = False
        self._fh = open(self.part_path, "w+b")
        self._fh.truncate(self.size)
        self.state = RECEIVING
        self._emit("start")
        if self.size == 0:
            self._check_complete()

    def _data(self, seg):
        if self.state != RECEIVING or self._fh is None:
            return  # DATA before START (START lost) or after completion: ignored
        end = seg.offset + len(seg.data)
        if end > self.size or not seg.data:
            return
        self._fh.seek(seg.offset)
        self._fh.write(seg.data)
        self.ranges.add(seg.offset, end)
        self._check_complete()

    def _end(self, seg):
        if self.state == IDLE:
            return
        self.end_crc = seg.crc32
        self._emit("end")
        self._check_complete()

    def _check_complete(self):
        if self.state != RECEIVING or not self.end_seen or self.missing():
            return
        self._finish()

    def finish_without_end(self):
        """Every byte is here but END never came (it was lost and is not sent again):
        keep the file with its own CRC, unverified. The satellite still compares that CRC
        when it gets DONE. Returns False if bytes are missing."""
        if self.state != RECEIVING or self.missing():
            return False
        self._finish()
        return True

    def _finish(self):
        self._fh.flush()
        self._fh.seek(0)
        crc = 0
        while chunk := self._fh.read(1 << 16):
            crc = zlib.crc32(chunk, crc)
        self.file_crc = crc & 0xFFFFFFFF
        self._fh.close()
        self._fh = None
        if self.end_crc is None or self.file_crc == self.end_crc:
            os.replace(self.part_path, self.path)
            self.verified = self.end_crc is not None
            self.state = COMPLETE
            self._emit("complete")
        else:
            self.state = CRC_MISMATCH
            self._emit("crc_mismatch")

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- command line


def _send_tc(sock, sat, packet, what):
    try:
        frame = sdlp_link.encapsulate_tc(packet, source="file_rx")
    except sdlp_link.SdlpError as exc:
        print(f"  {what}: not sent ({exc})")
        return False
    sock.sendto(frame, (sat, TC_PORT))
    time.sleep(CMD_GAP)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Receive a FILE_DL file from TM frames and save it")
    parser.add_argument("--port", type=int, default=1235, help="UDP port the TM frames arrive on")
    parser.add_argument("--bind", default="", help="local address to listen on (default: all)")
    parser.add_argument("--tag", type=int, default=None, help="keep only this DestTag")
    parser.add_argument("--mid", type=lambda v: int(v, 0), default=FILE_RELAY_SEG_TLM_MID,
                        help="segment MID (0x08C3 relay, 0x08C0 downlink)")
    parser.add_argument("--out", default="received", help="output directory")
    parser.add_argument("--sat", help="satellite address: send RESEND and DONE")
    parser.add_argument("--my-ip", default="127.0.0.1", help="own address for ADD_DEST")
    parser.add_argument("--register", action="store_true", help="ADD_DEST at start, REMOVE_DEST at the end")
    parser.add_argument("--resend-after", type=float, default=3.0,
                        help="seconds without segments after END before asking for missing ranges")
    parser.add_argument("--once", action="store_true", help="stop after the first complete file")
    parser.add_argument("--dl-loss", type=float, default=0.0,
                        help="test only: fraction of received TM frames to drop on purpose")
    parser.add_argument("--seed", type=int, default=1, help="random seed for --dl-loss")
    args = parser.parse_args(argv)
    loss_rng = random.Random(args.seed)
    frames_dropped = 0

    def report(event, rx):
        print(f"[{event}] {rx.status_line()}")
        if event in ("complete", "crc_mismatch"):
            end = "never came (unverified)" if rx.end_crc is None else f"{rx.end_crc:08x}"
            print(f"  file crc {rx.file_crc:08x}, END crc {end} -> "
                  f"{rx.path if event == 'complete' else rx.part_path}")

    rx = FileReceiver(args.out, dest_tag=args.tag, mids=(args.mid,), on_event=report)
    tm = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tm.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)  # frames come in bursts
    tm.bind((args.bind, args.port))
    tm.settimeout(0.5)
    tc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"listening for TM frames on UDP {args.port}, segments {args.mid:#06x}"
          f"{'' if args.tag is None else f' with DestTag {args.tag}'}, saving to {args.out}/")
    if args.sat and args.register:
        _send_tc(tc, args.sat, add_dest_command(args.my_ip, args.port), "ADD_DEST")
        print(f"sent ADD_DEST {args.my_ip}:{args.port} to {args.sat}")

    done_sent, last_progress = set(), 0.0
    try:
        while True:
            try:
                frame, (host, _) = tm.recvfrom(4096)
            except socket.timeout:
                frame = None
            if frame is not None and args.dl_loss and loss_rng.random() < args.dl_loss:
                frames_dropped += 1  # as if lost on the way down
                frame = None
            if frame is not None:
                try:
                    packets = sdlp_link.decapsulate_tm(frame, host=host, source="file_rx")
                except sdlp_link.SdlpError:
                    packets = []
                for packet in packets:
                    rx.handle_packet(packet)

            now = time.monotonic()
            if rx.state == "RECEIVING" and now - last_progress > 1.0:
                print(f"  {rx.status_line()}")
                last_progress = now

            key = (rx.name, rx.end_crc)
            if args.sat and rx.state == COMPLETE and key not in done_sent:
                _send_tc(tc, args.sat, done_command(rx.file_crc), "DONE")
                print(f"sent DONE crc {rx.file_crc:08x}")
                done_sent.add(key)
            elif (rx.state == RECEIVING and rx.last_segment_time
                  and now - rx.last_segment_time > args.resend_after):
                # Quiet for a while: ask for what is missing, or, with every byte here but
                # END lost, keep the file with its own CRC.
                gaps = rx.missing()[:FILE_DL_MAX_RESENDS]
                if not gaps:
                    rx.finish_without_end()
                    continue
                if args.sat:
                    print(f"asking for {len(gaps)} missing range(s) again")
                    for off, length in gaps:
                        _send_tc(tc, args.sat, resend_command(off, length), "RESEND")
                rx.last_segment_time = now  # wait again before the next round

            if args.once and rx.state in (COMPLETE, CRC_MISMATCH) and (not args.sat or key in done_sent):
                break
    except KeyboardInterrupt:
        pass
    finally:
        rx.close()
        if args.dl_loss:
            print(f"dropped {frames_dropped} TM frame(s) on purpose (--dl-loss {args.dl_loss})")
        if args.sat and args.register:
            _send_tc(tc, args.sat, remove_dest_command(args.my_ip, args.port), "REMOVE_DEST")
            print(f"sent REMOVE_DEST {args.my_ip}:{args.port}")
        tm.close()
        tc.close()
    return 0 if rx.state in (COMPLETE, IDLE) else 1


if __name__ == "__main__":
    sys.exit(main())
