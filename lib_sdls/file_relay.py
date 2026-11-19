"""File relay for the Ground System windows: the uplink commands, the chunk plan, and the
small state file the telemetry window shares with the command window.

Uplink (FILE_DL relay engine, MID 0x18C3, apps/file_dl/README.md 2.3), all big endian:
    CC 1 START   Filename[64] (/cf/relay/<name>), FileSize[4], Crc32[4], DestTag, Spare[3]   84 B
    CC 2 DATA    Offset[4] (multiple of 960), DataLength[2], Spare[2], Data[<= 960]        16 + n
    CC 3 END     -> status 0x08C2: missing ranges, or the CRC check starts                       8 B
    CC 4 STATUS  -> status 0x08C2 again                                                          8 B
    CC 5 CANCEL  drops the transfer and its .part file                                           8 B
plus TO_LAB 0x1880 CC 6 OUTPUT_ENABLE dest_IP[16] (main TM destination, port 1235).
The receiving side's commands (ADD_DEST, DONE, REMOVE_DEST, RESEND) are in file_rx.py.

State file (JSON, written by the File telemetry window, read by the command window):
    {"time": <unix s>, "transmit": <file_rx.parse_status dict or null>,
     "receive": {"name", "size", "received", "missing": [[off, len], ...], "end_crc",
                 "file_crc", "state", "verified", "path", "dest_tag"} or null}
Path: $GS_FILE_RELAY_STATE, default /tmp/gs_file_relay_<uid>.json.
"""

import json
import os
import tempfile
import zlib
from pathlib import Path

import file_rx

FILE_RELAY_CMD_MID = 0x18C3
RELAY_START_CC, RELAY_DATA_CC, RELAY_END_CC, RELAY_STATUS_CC, RELAY_CANCEL_CC = 1, 2, 3, 4, 5
TO_OUTPUT_ENABLE_CC = 6

CHUNK = 960  # FILE_UL_CHUNK_SIZE
MAX_FILE_SIZE = 4 * 1024 * 1024  # FILE_UL_MAX_FILE_SIZE
MAX_NAME = 14  # OS_MAX_FILE_NAME 20 with ".part" and its NUL
RELAY_DIR = "/cf/relay/"
CMD_GAP = 0.02  # seconds between commands (README 5.6)


# ---------------------------------------------------------------- commands


def output_enable_command(ip):
    raw = ip.encode()
    if len(raw) > 15:
        raise ValueError(f"IP address too long: {ip}")
    return file_rx.command(file_rx.TO_LAB_CMD_MID, TO_OUTPUT_ENABLE_CC, raw.ljust(16, b"\0"))


def relay_start_command(name, size, crc32, dest_tag):
    path = (RELAY_DIR + name).encode()
    if len(path) > 63:
        raise ValueError(f"path too long: {RELAY_DIR + name}")
    payload = (path.ljust(64, b"\0") + size.to_bytes(4, "big") + crc32.to_bytes(4, "big")
               + bytes([dest_tag & 0xFF]) + bytes(3))
    return file_rx.command(FILE_RELAY_CMD_MID, RELAY_START_CC, payload)


def data_command(offset, chunk):
    if not 0 < len(chunk) <= CHUNK or offset % CHUNK:
        raise ValueError(f"bad chunk: offset {offset}, {len(chunk)} B")
    payload = offset.to_bytes(4, "big") + len(chunk).to_bytes(2, "big") + bytes(2) + chunk
    return file_rx.command(FILE_RELAY_CMD_MID, RELAY_DATA_CC, payload)


def end_command():
    return file_rx.command(FILE_RELAY_CMD_MID, RELAY_END_CC)


def status_command():
    return file_rx.command(FILE_RELAY_CMD_MID, RELAY_STATUS_CC)


def cancel_command():
    return file_rx.command(FILE_RELAY_CMD_MID, RELAY_CANCEL_CC)


# ---------------------------------------------------------------- upload file


class UploadFile:
    """A ground file checked for the relay: name, data, size, crc32."""

    def __init__(self, path):
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"no such file: {path}")
        name = path.name
        if not name or len(name.encode()) > MAX_NAME:
            raise ValueError(f"file name '{name}' must be 1 to {MAX_NAME} bytes (on board it gets .part)")
        data = path.read_bytes()
        if not 0 < len(data) <= MAX_FILE_SIZE:
            raise ValueError(f"file size must be 1 B to 4 MiB, not {len(data)} B")
        self.path, self.name, self.data = path, name, data
        self.size = len(data)
        self.crc32 = zlib.crc32(data) & 0xFFFFFFFF

    @property
    def board_name(self):
        return RELAY_DIR + self.name

    def chunk(self, offset):
        return self.data[offset : offset + CHUNK]


def plan_chunks(size, gaps=None):
    """Chunk offsets to send: every chunk, or only the chunks that cover `gaps`
    [(offset, length)] (the status after END lists what is missing)."""
    if not gaps:
        return list(range(0, size, CHUNK))
    offsets = set()
    for off, length in gaps:
        start = (off // CHUNK) * CHUNK
        for o in range(start, min(off + length, size), CHUNK):
            offsets.add(o)
    return sorted(offsets)


def gaps_for(upload, status):
    """The missing ranges a status lists for this upload (same file, still RECEIVING),
    else None (send everything)."""
    if not status or upload is None:
        return None
    if status.get("name") != upload.board_name or status.get("size") != upload.size:
        return None
    if status.get("state") != "RECEIVING" or not status.get("gaps"):
        return None
    return [tuple(g) for g in status["gaps"]]


# ---------------------------------------------------------------- state file


def state_path():
    return Path(os.environ.get("GS_FILE_RELAY_STATE")
                or Path(tempfile.gettempdir()) / f"gs_file_relay_{os.getuid()}.json")


def write_state(state, path=None):
    """Atomic write (tmp file + rename): the command window never reads half a file."""
    path = Path(path or state_path())
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def read_state(path=None):
    try:
        return json.loads(Path(path or state_path()).read_text())
    except (OSError, ValueError):
        return None


def receive_state(rx):
    """file_rx.FileReceiver -> the "receive" part of the state file."""
    if rx is None or rx.state == file_rx.IDLE:
        return None
    return {"name": rx.name, "size": rx.size, "received": rx.received,
            "missing": [list(g) for g in rx.missing()], "end_crc": rx.end_crc,
            "file_crc": rx.file_crc, "state": rx.state, "verified": rx.verified,
            "path": str(rx.path) if rx.path else None, "dest_tag": rx.dest_tag}


# ---------------------------------------------------------------- sending


class TcSender:
    """Sends command packets as TC frames (sdlp_link.encapsulate_tc) to CI_LAB over UDP."""

    def __init__(self, host, port=1234, source="FileRelay"):
        import socket  # noqa: PLC0415

        import sdlp_link  # noqa: PLC0415

        self.host, self.port, self.source = host, port, source
        self._link = sdlp_link
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, packet):
        """Returns None when sent, else the reason it was not."""
        try:
            frame = self._link.encapsulate_tc(packet, source=self.source)
        except self._link.SdlpError as exc:
            return str(exc)
        try:
            self._sock.sendto(frame, (self.host, self.port))
        except OSError as exc:
            return str(exc)
        return None

    def close(self):
        self._sock.close()


def send_chunks(send, upload, offsets, sleep, progress=None, stopped=lambda: False):
    """DATA for each offset, CMD_GAP apart. send(packet) -> None or an error text.
    progress(done, total) after each chunk. Returns (sent, error or None)."""
    for i, off in enumerate(offsets, 1):
        if stopped():
            return i - 1, "stopped"
        error = send(data_command(off, upload.chunk(off)))
        if error:
            return i - 1, error
        if progress:
            progress(i, len(offsets))
        sleep(CMD_GAP)
    return len(offsets), None
