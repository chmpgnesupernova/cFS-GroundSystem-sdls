"""Ground System instance: several Ground Systems on one PC, each named by its TM port.

    python3 GroundSystem.py                    # instance 1235: TM on UDP 1235 (as before)
    python3 GroundSystem.py --instance 1245    # instance 1245: TM on UDP 1245

Every per-instance resource follows the port, so two instances never share one:

    resource                    instance 1235 (default)            instance <port>
    TM receive port             1235                               <port>
    zmq (RoutingService -> GUI) ipc:///tmp/GroundSystem            ipc:///tmp/GroundSystem-<port>
    header offsets              /tmp/OffsetData                    /tmp/OffsetData-<port>
    SDLP log  ($SDLP_LOG)       /tmp/sdlp_link.log                 /tmp/sdlp_link-<port>.log
    File Relay state            /tmp/gs_file_relay_<uid>.json      /tmp/gs_file_relay_<uid>-<port>.json
    SA DB  ($SDLS_SA_DB, SDLS)  /sdls_sa_db                        /sdls_sa_db-<port>

GroundSystem.py calls apply(port) before anything else; the choice reaches every window
it opens through the environment ($GS_INSTANCE and the variables above), so the command
and telemetry windows, MiniCmdUtil and the File Relay windows all use the same instance.
A variable already set by hand ($SDLP_LOG, ...) is kept.
"""

import os
import tempfile

ENV = "GS_INSTANCE"
DEFAULT_PORT = 1235


def check_port(value):
    """Port number as int; ValueError if it is not 1-65535."""
    try:
        port = int(str(value).strip())
    except ValueError:
        raise ValueError(f"instance must be a UDP port number, not '{value}'") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"instance must be a UDP port number (1-65535), not {port}")
    return port


def tm_port():
    """This process's instance: the UDP port its RoutingService receives TM frames on."""
    value = os.environ.get(ENV)
    return check_port(value) if value else DEFAULT_PORT


def _suffix():
    port = tm_port()
    return "" if port == DEFAULT_PORT else f"-{port}"


def ipc_address():
    return f"ipc:///tmp/GroundSystem{_suffix()}"


def offset_file():
    return f"/tmp/OffsetData{_suffix()}"


def title(text):
    """Window title with the instance, so two instances are told apart on screen."""
    return f"{text} [TM {tm_port()}]"


def apply(port):
    """Makes `port` this process's instance and exports it (and the per-instance paths of
    lib_sdls) to every process started from here. Returns the port."""
    port = check_port(port)
    os.environ[ENV] = str(port)
    if port != DEFAULT_PORT:
        os.environ.setdefault("SDLP_LOG", f"/tmp/sdlp_link-{port}.log")
        os.environ.setdefault("GS_FILE_RELAY_STATE",
                              os.path.join(tempfile.gettempdir(), f"gs_file_relay_{os.getuid()}-{port}.json"))
        os.environ.setdefault("SDLS_SA_DB", f"/sdls_sa_db-{port}")
    return port
