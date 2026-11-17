"""GroundSystem network settings kept between runs (network-config.ini).

Only the TC target IP (where commands are sent) is configurable. The Command System
writes it when the IP is changed in its window and reads it at start; command pages
opened from it get the same IP. Ports stay as defined in command-pages.txt and
quick-buttons.txt (the satellite listens on a compiled-in port).

File (INI), next to GroundSystem.py by default, or $GS_NETWORK_CONFIG:

    [tc]
    target_ip = 192.168.0.20
"""

import configparser
import ipaddress
import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "network-config.ini"

SECTION = "tc"
KEY = "target_ip"


def config_path():
    return Path(os.environ.get("GS_NETWORK_CONFIG") or DEFAULT_PATH)


def validate_ip(text):
    """Returns the IPv4 address in canonical form, or raises ValueError.
    Commands go out on an IPv4 UDP socket, so host names and IPv6 are refused."""
    return str(ipaddress.IPv4Address(text.strip()))


def load_tc_target_ip():
    """The saved TC target IP, or None if there is no file, no entry, or an invalid one."""
    path = config_path()
    parser = configparser.ConfigParser()
    try:
        if not parser.read(path, encoding="utf-8"):
            return None
        value = parser.get(SECTION, KEY, fallback=None)
        return validate_ip(value) if value else None
    except (configparser.Error, ValueError) as exc:
        print(f"network config: ignoring {path}: {exc}")
        return None


def save_tc_target_ip(ip):
    """Validates and writes the TC target IP, keeping any other settings in the file.
    Written to a temporary file first, then renamed, so a crash never leaves a half
    written config. Returns the path; raises ValueError (bad IP) or OSError."""
    ip = validate_ip(ip)
    path = config_path()
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        parser = configparser.ConfigParser()  # unreadable file: start over
    if not parser.has_section(SECTION):
        parser.add_section(SECTION)
    parser.set(SECTION, KEY, ip)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".network-config.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("# cFS GroundSystem network settings, written by the Command System\n")
            parser.write(handle)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
