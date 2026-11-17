"""Hand built Space Packet (TC) from hex fields, for HandcraftCommand.py.

    Packet ID (2 B) | Sequence Control (2 B) | Packet Data Length (2 B)   primary header
    Function Code (1 B) | Checksum (1 B)                                 cFS command secondary header
    Data (0..n B)                                                        parameters, as typed

Every field is taken as typed, so malformed packets can be built on purpose; calc_length()
and calc_checksum() give the correct values when wanted. No Qt here, so it can be tested
on its own.
"""

from dataclasses import dataclass

# (name, size in bytes) of the fixed fields, in packet order
FIXED_FIELDS = (
    ("packet_id", 2),
    ("seq_ctrl", 2),
    ("length", 2),
    ("func_code", 1),
    ("checksum", 1),
)
HEADER_LENGTH = 6  # SPP primary header
CHECKSUM_OFFSET = 7  # byte index of the cFS command checksum
SEQ_FLAGS = {0: "continuation", 1: "first segment", 2: "last segment", 3: "unsegmented"}


def _clean(text):
    """Hex digits only: drops spaces, ':' and '_' separators, and 0x prefixes."""
    text = text.strip().lower()
    for sep in ("0x", " ", "\t", ":", "_", ","):
        text = text.replace(sep, "")
    return text


def parse_fixed(text, size):
    """Hex text -> int of exactly `size` bytes. Short input is padded on the left
    ("1" -> 0x0001); raises ValueError for empty, non hex or too large input."""
    digits = _clean(text)
    if not digits:
        raise ValueError("empty")
    if any(c not in "0123456789abcdef" for c in digits):
        raise ValueError(f"not hex: '{text.strip()}'")
    if len(digits) > 2 * size:
        raise ValueError(f"more than {size} byte{'s' if size > 1 else ''}")
    return int(digits, 16)


def parse_data(text):
    """Hex text -> bytes (any length, may be empty). Separators allowed; the number of
    hex digits must be even."""
    digits = _clean(text)
    if any(c not in "0123456789abcdef" for c in digits):
        raise ValueError(f"not hex: '{text.strip()}'")
    if len(digits) % 2:
        raise ValueError("odd number of hex digits")
    return bytes.fromhex(digits)


@dataclass
class Fields:
    packet_id: int
    seq_ctrl: int
    length: int
    func_code: int
    checksum: int
    data: bytes = b""

    def to_bytes(self):
        return (self.packet_id.to_bytes(2, "big") + self.seq_ctrl.to_bytes(2, "big")
                + self.length.to_bytes(2, "big") + bytes([self.func_code, self.checksum]) + self.data)


def parse_fields(texts):
    """{field name: hex text} -> (Fields or None, {field name: error}). Keys: the
    FIXED_FIELDS names and "data"."""
    values, errors = {}, {}
    for name, size in FIXED_FIELDS:
        try:
            values[name] = parse_fixed(texts.get(name, ""), size)
        except ValueError as exc:
            errors[name] = str(exc)
    try:
        values["data"] = parse_data(texts.get("data", ""))
    except ValueError as exc:
        errors["data"] = str(exc)
    return (None if errors else Fields(**values)), errors


def calc_length(data):
    """Packet Data Length for a secondary header (2 B) plus data: data field size - 1."""
    return 2 + len(data) - 1


def calc_checksum(fields):
    """cFS command checksum: XOR of every byte of the packet, checksum included, is 0xFF."""
    packet = bytearray(fields.to_bytes())
    packet[CHECKSUM_OFFSET] = 0
    value = 0xFF
    for byte in packet:
        value ^= byte
    return value


def xor_all(packet):
    value = 0
    for byte in packet:
        value ^= byte
    return value


# --- what the fields say (shown next to them) ---------------------------------------------


def describe_packet_id(value):
    kind = "cmd" if value & 0x1000 else "tlm"
    return (f"ver {value >> 13}, {kind}, sec hdr {(value >> 11) & 1}, "
            f"APID 0x{value & 0x07FF:03x}")


def describe_seq_ctrl(value):
    flags = value >> 14
    return f"flags {flags:02b} ({SEQ_FLAGS[flags]}), count {value & 0x3FFF}"


def describe_length(fields):
    """Total size the field announces, and whether the packet really has that size."""
    announced = fields.length + 7
    actual = len(fields.to_bytes())
    if announced == actual:
        return f"total {announced} B (matches)", True
    return f"says {announced} B, packet is {actual} B (should be 0x{actual - 7:04x})", False


def describe_checksum(fields):
    packet = fields.to_bytes()
    total = xor_all(packet)
    if total == 0xFF:
        return "OK (XOR of all bytes = ff)", True
    return f"XOR of all bytes = {total:02x}, should be ff (checksum 0x{calc_checksum(fields):02x})", False


def preview(packet):
    """One line: the packet bytes in hex."""
    return packet.hex(" ")
