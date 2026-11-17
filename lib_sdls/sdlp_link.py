"""lib_sdls link for the cFS GroundSystem: one module for framing, logging and monitoring.

    MiniCmdUtil.py     frame   = sdlp_link.encapsulate_tc(spp)           before sendto()
    RoutingService.py  packets = sdlp_link.decapsulate_tm(frame, host)   after recvfrom()
    monitor            python3 lib_sdls/sdlp_link.py [options]          (see --help)
                       frame fields in color on a terminal: Primary Header cyan, SPI/IV
                       yellow, Data white, MAC magenta, Space Packet header green

Every call writes one line to the log file with the raw bytes before and after processing.
The monitor follows that file and prints each line decoded. The GUI is not involved:
RoutingService still publishes plain Space Packets, and a failure here only drops that
frame or refuses that command (and logs why).

Environment
    SDLS_LIB      libsdls.so path         default: build/libsdls.so next to this file
    SDLP_LOG      log file path, or "off" default: /tmp/sdlp_link.log
    SDLP_TC_SPI   TC SPI (SDLS build only) default: 0x0000 (SA00)
    SDLS_SA_DB    SA DB name (SDLS build)  default: /sdls_sa_db

Log line (text, one per call, space separated, values never contain spaces):
    <time> <TC|TM> <OK|FAIL> pid=<pid> src=<caller> mode=<SDLP-only|SDLS> [host=<ip>]
        [spi=0x....] [code=<n> err=<NAME>] in=<hex> out=<hex>[,<hex>...]
    TC: in = Space Packet, out = TC frame.  TM: in = TM frame, out = extracted packets.
    "-" stands for no bytes.
"""

import argparse
import ctypes
import datetime
import os
import pathlib
import sys
import threading
import time

# --- constants mirrored from lib_sdls/include (ctypes cannot read #define) ----------------

MODE_SDLP_ONLY = 1  # SDLP_MY_MODE_SDLP_ONLY
MODE_SDLS = 2  # SDLP_MY_MODE_SDLS
MODE_NAMES = {MODE_SDLP_ONLY: "SDLP-only", MODE_SDLS: "SDLS"}

TC_PRIHDR_LENGTH = 5
TC_MAX_FRAME_LENGTH = 1024
TM_PRIHDR_LENGTH = 6
TM_FRAME_SIZE = 1786
SECHDR_LENGTH = 14  # SPI 2 + IV 12
TRAILER_LENGTH = 16  # MAC
SPP_HDR_LENGTH = 6

TC_ERRORS = {
    -2: "TC_SDLP_MY_INVALID_ARGUMENT",
    -3: "TC_SDLP_MY_INVALID_LENGTH",
    -4: "TC_SDLP_MY_INVALID_PACKET",
    -6: "TC_SDLP_MY_SECURITY_ERROR",
    -7: "TC_SDLP_MY_BUFFER_TOO_SMALL",
    -8: "TC_SDLP_MY_NOT_INITIALIZED",
}
TM_ERRORS = {
    -2: "TM_SDLP_MY_INVALID_ARGUMENT",
    -3: "TM_SDLP_MY_INVALID_LENGTH",
    -4: "TM_SDLP_MY_INVALID_PACKET",
    -5: "TM_SDLP_MY_INVALID_HEADER",
    -6: "TM_SDLP_MY_SECURITY_ERROR",
}
LIB_ERROR = "LIB_NOT_LOADED"

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_LIB = HERE / "build" / "libsdls.so"
DEFAULT_LOG = "/tmp/sdlp_link.log"


class SdlpError(Exception):
    """A frame could not be built or taken apart. code is the library's return code
    (None when the library itself could not be loaded)."""

    def __init__(self, message, code=None, name=LIB_ERROR):
        super().__init__(message)
        self.code = code
        self.name = name


# --- library ------------------------------------------------------------------------------

_lock = threading.Lock()  # TC_SDLP_myBuildFrame keeps a static sequence counter
_lib = None
_mode = None


def _load():
    """Loads libsdls.so once per process (and opens the SA DB in an SDLS build)."""
    global _lib, _mode
    if _lib is not None:
        return _lib
    path = os.environ.get("SDLS_LIB") or str(DEFAULT_LIB)
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise SdlpError(f"cannot load {path}: {exc}") from exc

    p, n, i32 = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int32
    lib.SDLP_myGetMode.argtypes, lib.SDLP_myGetMode.restype = [], i32
    lib.TC_SDLP_myInit.argtypes, lib.TC_SDLP_myInit.restype = [], i32
    lib.TC_SDLP_myBuildFrame.argtypes = [ctypes.c_uint16, p, n, p, n]
    lib.TC_SDLP_myBuildFrame.restype = i32
    lib.TM_SDLP_myExtractPacket.argtypes = [p, n, ctypes.POINTER(n), ctypes.POINTER(n), ctypes.POINTER(ctypes.c_uint16)]
    lib.TM_SDLP_myExtractPacket.restype = i32

    mode = lib.SDLP_myGetMode()
    if lib.TC_SDLP_myInit() != 0:
        raise SdlpError(f"TC_SDLP_myInit failed in {path}")
    if mode == MODE_SDLS:
        # Full SDLS build: frames go through the shared SA DB (same name in every process).
        lib.SA_myOpen.argtypes, lib.SA_myOpen.restype = [ctypes.c_char_p], i32
        name = os.environ.get("SDLS_SA_DB", "/sdls_sa_db")
        status = lib.SA_myOpen(name.encode())
        if status != 0:
            raise SdlpError(f"SA_myOpen({name}) failed ({status})")
    _lib, _mode = lib, mode
    return lib


def mode_name():
    """'SDLP-only' or 'SDLS' as built, or 'unloaded' if the library cannot be loaded."""
    try:
        with _lock:
            _load()
        return MODE_NAMES.get(_mode, f"mode{_mode}")
    except SdlpError:
        return "unloaded"


def _tc_spi():
    try:
        return int(os.environ.get("SDLP_TC_SPI", "0"), 0) & 0xFFFF
    except ValueError:
        return 0


# --- TC / TM --------------------------------------------------------------------------------


def encapsulate_tc(spp, source="MiniCmdUtil"):
    """Space Packet -> TC frame. Raises SdlpError (and logs it) if no frame can be built;
    the caller must then not send anything."""
    spp = bytes(spp)
    with _lock:
        try:
            lib = _load()
        except SdlpError as exc:
            _log("TC", False, source, {"code": "-", "err": exc.name}, spp, b"")
            raise
        spi = _tc_spi()
        buf = ctypes.create_string_buffer(TC_MAX_FRAME_LENGTH)
        status = lib.TC_SDLP_myBuildFrame(spi, spp, len(spp), buf, len(buf))
    extra = {"spi": f"0x{spi:04x}"} if _mode == MODE_SDLS else {}
    if status < 0:
        name = TC_ERRORS.get(status, "UNKNOWN")
        _log("TC", False, source, dict(extra, code=status, err=name), spp, b"")
        raise SdlpError(f"TC frame not built: {name} ({status})", status, name)
    frame = buf.raw[:status]
    _log("TC", True, source, extra, spp, frame)
    return frame


def decapsulate_tm(frame, host=None, source="RoutingService"):
    """TM frame -> list of Space Packets (idle packets left out). Raises SdlpError (and
    logs it) for a frame that cannot be taken apart; the caller drops it."""
    frame = bytes(frame)
    base = {"host": host} if host else {}
    with _lock:
        try:
            lib = _load()
        except SdlpError as exc:
            _log("TM", False, source, dict(base, code="-", err=exc.name), frame, [])
            raise
    buf = ctypes.create_string_buffer(frame, max(len(frame), 1))  # writable: SDLS decrypts in place
    cursor, offset, spi = ctypes.c_size_t(0), ctypes.c_size_t(0), ctypes.c_uint16(0)
    packets = []
    while True:
        status = lib.TM_SDLP_myExtractPacket(buf, len(frame), ctypes.byref(cursor), ctypes.byref(offset), ctypes.byref(spi))
        if status <= 0:
            break
        packets.append(buf.raw[offset.value : offset.value + status])
    fields = dict(base, spi=f"0x{spi.value:04x}")
    if status < 0:
        name = TM_ERRORS.get(status, "UNKNOWN")
        _log("TM", False, source, dict(fields, code=status, err=name), frame, [])
        raise SdlpError(f"TM frame dropped: {name} ({status})", status, name)
    _log("TM", True, source, fields, frame, packets)
    return packets


# --- log ------------------------------------------------------------------------------------


def log_path():
    return os.environ.get("SDLP_LOG", DEFAULT_LOG)


def _hex(data):
    return data.hex() if data else "-"


def _log(direction, ok, source, fields, data_in, data_out):
    """Appends one line. Never raises: logging must not affect commanding or telemetry."""
    path = log_path()
    if not path or path.lower() == "off":
        return
    try:
        now = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
        parts = [now, direction, "OK" if ok else "FAIL", f"pid={os.getpid()}", f"src={source}",
                 f"mode={MODE_NAMES.get(_mode, 'unloaded')}"]
        parts += [f"{k}={v}" for k, v in fields.items()]
        if isinstance(data_out, (list, tuple)):
            out = ",".join(_hex(p) for p in data_out) or "-"
        else:
            out = _hex(data_out)
        parts += [f"in={_hex(data_in)}", f"out={out}"]
        line = (" ".join(parts) + "\n").encode()
        # One write() on an O_APPEND descriptor: lines from several processes (each command
        # dialog is its own process) do not interleave.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - a broken log must never stop a command or a frame
        pass


def parse_line(line):
    """One log line -> dict(time, dir, result, <key>: <value> ...). None if malformed."""
    tokens = line.split()
    if len(tokens) < 3 or tokens[1] not in ("TC", "TM"):
        return None
    entry = {"time": tokens[0], "dir": tokens[1], "result": tokens[2]}
    for token in tokens[3:]:
        key, sep, value = token.partition("=")
        if sep:
            entry[key] = value
    return entry


def _unhex(value):
    return b"" if value in (None, "", "-") else bytes.fromhex(value)


# --- colors (monitor) ----------------------------------------------------------------------
# ANSI colors, one per frame field. Used only when the monitor writes to a terminal
# (--color auto) or when asked (--color always); never in the log file itself.

RESET = "\033[0m"
COLORS = {
    "prihdr": "\033[1;36m",  # Primary Header      bold cyan
    "spi": "\033[1;33m",  # Security Header SPI    bold yellow
    "iv": "\033[33m",  # Security Header IV        yellow
    "data": "\033[97m",  # Data field              bright white
    "mac": "\033[1;35m",  # Security Trailer (MAC)  bold magenta
    "spphdr": "\033[32m",  # Space Packet header   green
    "offset": "\033[2m",  # hex dump offsets       dim
    "ok": "\033[1;32m",
    "fail": "\033[1;31m",
}
LEGEND = (("prihdr", "Primary Header"), ("spi", "SPI"), ("iv", "IV"), ("data", "Data"),
          ("mac", "MAC"), ("spphdr", "SPP Header"))


class _Paint:
    """paint(kind, text): text wrapped in the color for kind, or unchanged when off."""

    def __init__(self, on):
        self.on = on

    def __call__(self, kind, text):
        return f"{COLORS[kind]}{text}{RESET}" if self.on and text else text


def legend(paint):
    return "Legend: " + "  ".join(paint(kind, f"[{name}]") for kind, name in LEGEND)


def _frame_regions(size, prihdr_len):
    """(start, end, kind) for [PriHdr][SPI 2][IV 12][Data][MAC 16]."""
    sec_end = prihdr_len + SECHDR_LENGTH
    if size < sec_end + TRAILER_LENGTH:
        return [(0, size, "data")]
    return [(0, prihdr_len, "prihdr"), (prihdr_len, prihdr_len + 2, "spi"), (prihdr_len + 2, sec_end, "iv"),
            (sec_end, size - TRAILER_LENGTH, "data"), (size - TRAILER_LENGTH, size, "mac")]


def tc_frame_regions(size):
    return _frame_regions(size, TC_PRIHDR_LENGTH)


def tm_frame_regions(size):
    return _frame_regions(size, TM_PRIHDR_LENGTH)


def spp_regions(size):
    """[SPP header 6][Data]."""
    return [(0, min(SPP_HDR_LENGTH, size), "spphdr"), (SPP_HDR_LENGTH, size, "data")]


# --- decoders (for the monitor) -------------------------------------------------------------


def decode_spp(data):
    if len(data) < SPP_HDR_LENGTH:
        return f"({len(data)} B, too short for a Space Packet header)"
    word0 = int.from_bytes(data[0:2], "big")
    word1 = int.from_bytes(data[2:4], "big")
    apid = word0 & 0x07FF
    kind = "cmd" if word0 & 0x1000 else "tlm"
    return (f"MID 0x{word0:04x} APID 0x{apid:03x} {kind} seq {word1 & 0x3FFF} "
            f"len {int.from_bytes(data[4:6], 'big') + 7}")


def _sechdr_parts(sec, trailer):
    spi = int.from_bytes(sec[0:2], "big")
    iv = int.from_bytes(sec[2:14], "big")
    zero = "zero" if not any(trailer) else "non-zero"
    return f"SPI 0x{spi:04x}", f"IV {iv}", f"MAC {zero}"


def _tc_frame_parts(frame):
    o0, o1, o2, o3, seq = frame[0:5]
    head = (f"TFVN {o0 >> 6} bypass {(o0 >> 5) & 1} ctl {(o0 >> 4) & 1} "
            f"SCID 0x{((o0 & 3) << 8) | o1:03x} VCID {o2 >> 2} "
            f"len {(((o2 & 3) << 8) | o3) + 1} seq {seq}")
    sec = frame[TC_PRIHDR_LENGTH : TC_PRIHDR_LENGTH + SECHDR_LENGTH]
    return (head, *_sechdr_parts(sec, frame[-TRAILER_LENGTH:]))


def _tm_frame_parts(frame):
    id0, id1, mc, vc, dfs0, dfs1 = frame[0:6]
    head = (f"TFVN {id0 >> 6} SCID 0x{((id0 & 0x3F) << 4) | (id1 >> 4):03x} VCID {(id1 >> 1) & 7} "
            f"MC {mc} VC {vc} FHP {((dfs0 & 7) << 8) | dfs1}")
    sec = frame[TM_PRIHDR_LENGTH : TM_PRIHDR_LENGTH + SECHDR_LENGTH]
    return (head, *_sechdr_parts(sec, frame[-TRAILER_LENGTH:]))


def _decode_frame(frame, parts, prihdr_len, name, paint):
    if len(frame) < prihdr_len + SECHDR_LENGTH + TRAILER_LENGTH:
        return f"({len(frame)} B, too short for a {name} frame)"
    head, spi, iv, mac = parts(frame)
    return f"{paint('prihdr', head)} | {paint('spi', spi)} {paint('iv', iv)} {paint('mac', mac)}"


def decode_tc_frame(frame, paint=_Paint(False)):
    return _decode_frame(frame, _tc_frame_parts, TC_PRIHDR_LENGTH, "TC", paint)


def decode_tm_frame(frame, paint=_Paint(False)):
    return _decode_frame(frame, _tm_frame_parts, TM_PRIHDR_LENGTH, "TM", paint)


def _hex_row(data, start, end, regions, paint):
    """Hex of data[start:end], one color per run of bytes in the same field."""
    runs, kind, run = [], None, []
    for i in range(start, end):
        k = next((r[2] for r in regions if r[0] <= i < r[1]), None)
        if k != kind and run:
            runs.append((kind, run))
            run = []
        kind = k
        run.append(f"{data[i]:02x}")
    if run:
        runs.append((kind, run))
    return " ".join(paint(k, " ".join(r)) if k else " ".join(r) for k, r in runs)


def _hexdump(data, limit, paint=_Paint(False), regions=(), tail=0, indent="        "):
    """Hex dump, 16 bytes a row. With limit > 0 only the first limit bytes are shown,
    plus the last tail bytes (so a frame's MAC stays visible when it is cut)."""
    if not data:
        return [f"{indent}-"]
    size = len(data)
    spans = [(0, size)]
    if 0 < limit < size and size - limit > tail:  # otherwise the tail joins the head: show it all
        spans = [(0, limit)] + ([(size - tail, size)] if tail else [])
    lines = []
    for n, (start, end) in enumerate(spans):
        if n == 1:
            lines.append(f"{indent}... {spans[1][0] - spans[0][1]} bytes not shown (--hex-limit 0 shows all)")
        for off in range(start, end, 16):
            row = _hex_row(data, off, min(off + 16, end), regions, paint)
            lines.append(f"{indent}{paint('offset', f'{off:04x}')}  {row}")
    if len(spans) == 1 and spans[0][1] < size:
        lines.append(f"{indent}... {size - spans[0][1]} more bytes (--hex-limit 0 shows all)")
    return lines


def format_entry(entry, show_hex=True, hex_limit=128, color=False):
    """Human readable block for one parsed log line (ANSI colored when color is True)."""
    paint = _Paint(color)
    direction, ok = entry["dir"], entry["result"] == "OK"
    result = paint("ok" if ok else "fail", f"{entry['result']:<4}")
    head = (f"[{entry['time']}] {direction} {result} pid {entry.get('pid', '?')} "
            f"{entry.get('src', '?')} {entry.get('mode', '?')}")
    if "host" in entry:
        head += f" from {entry['host']}"
    if not ok:
        head += "  " + paint("fail", f"{entry.get('err', '?')} ({entry.get('code', '?')})")
    lines = [head]
    data_in = _unhex(entry.get("in"))
    outs = [_unhex(v) for v in entry.get("out", "-").split(",") if v != "-"]

    def dump(data, regions, tail=0):
        return _hexdump(data, hex_limit, paint, regions, tail) if show_hex else []

    if direction == "TC":
        lines.append(f"  before  Space Packet {len(data_in):4d} B  {paint('spphdr', decode_spp(data_in))}")
        lines += dump(data_in, spp_regions(len(data_in)))
        for frame in outs:
            lines.append(f"  after   TC frame     {len(frame):4d} B  {decode_tc_frame(frame, paint)}")
            lines += dump(frame, tc_frame_regions(len(frame)), TRAILER_LENGTH)
    else:
        lines.append(f"  before  TM frame     {len(data_in):4d} B  {decode_tm_frame(data_in, paint)}")
        lines += dump(data_in, tm_frame_regions(len(data_in)), TRAILER_LENGTH)
        for i, packet in enumerate(outs, 1):
            lines.append(f"  after   packet {i:<3d}   {len(packet):4d} B  {paint('spphdr', decode_spp(packet))}")
            lines += dump(packet, spp_regions(len(packet)))
        if ok and not outs:
            lines.append("  after   (no packets: idle frame)")
    return "\n".join(lines)


# --- monitor --------------------------------------------------------------------------------


def _follow(path, from_start, poll=0.2):
    """Yields lines appended to path (tail -f), waiting for the file to appear and
    starting over if it is truncated or replaced."""
    handle, inode, pending = None, None, b""
    while True:
        if handle is None:
            try:
                handle = open(path, "rb")
                inode = os.fstat(handle.fileno()).st_ino
                if not from_start:
                    handle.seek(0, os.SEEK_END)
                from_start = True  # a file created or replaced later is read from its start
                pending = b""
            except FileNotFoundError:
                time.sleep(poll)
                continue
        chunk = handle.readline()
        if chunk:
            pending += chunk
            if pending.endswith(b"\n"):
                yield pending.decode("utf-8", errors="replace")
                pending = b""
            continue  # a partial line waits in pending for the rest
        time.sleep(poll)
        try:
            stat = os.stat(path)
            if stat.st_ino != inode or stat.st_size < handle.tell():
                handle.close()
                handle = None
        except FileNotFoundError:
            handle.close()
            handle = None


def monitor(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(
        prog="sdlp_link.py",
        description="Follow the lib_sdls log: TC/TM raw bytes before and after framing.")
    parser.add_argument("--log", default=log_path(), help=f"log file (default: $SDLP_LOG or {DEFAULT_LOG})")
    parser.add_argument("--all", action="store_true", help="show the existing log first (default: new lines only)")
    parser.add_argument("--once", action="store_true", help="print the log and exit (no follow)")
    parser.add_argument("--dir", choices=("TC", "TM"), help="only this direction")
    parser.add_argument("--fail", action="store_true", help="only failures")
    parser.add_argument("--no-hex", action="store_true", help="decoded headers only")
    parser.add_argument("--hex-limit", type=int, default=128, metavar="N",
                        help="bytes of hex shown per field (default 128, 0 = all)")
    parser.add_argument("--raw", action="store_true", help="print log lines unchanged")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="color the frame fields (default auto: only on a terminal, and not if $NO_COLOR is set)")
    args = parser.parse_args(argv)
    color = not args.raw and (
        args.color == "always"
        or (args.color == "auto" and getattr(out, "isatty", lambda: False)()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"))

    if args.once:
        try:
            with open(args.log, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            lines = []
    else:
        print(f"sdlp_link monitor: following {args.log} (Ctrl+C to stop)", file=out, flush=True)
        lines = _follow(args.log, from_start=args.all)
    if color:
        print(legend(_Paint(True)), file=out, flush=True)

    try:
        for line in lines:
            entry = parse_line(line)
            if entry is None:
                continue
            if (args.dir and entry["dir"] != args.dir) or (args.fail and entry["result"] == "OK"):
                continue
            text = line.rstrip("\n") if args.raw else format_entry(entry, not args.no_hex, args.hex_limit, color)
            print(text, file=out, flush=True)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(monitor())
