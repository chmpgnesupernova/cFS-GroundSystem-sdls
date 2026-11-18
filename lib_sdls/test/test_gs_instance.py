"""Several Ground Systems on one PC, each named by its TM port (lib_sdls/gs_instance.py,
GroundSystem.py --instance <port>): every per-instance resource follows the port, the
default instance (1235) keeps the old names, and two instances run side by side."""

import contextlib
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

from sat_reference import SatTmFramer, make_spp
from test_groundsystem import REPO, offset_file, routing_service_module
from test_sdlp_link import SDLP_ONLY, private_link

sys.path.insert(0, str(REPO / "lib_sdls"))
import gs_instance  # noqa: E402

ENV_KEYS = ("GS_INSTANCE", "SDLP_LOG", "GS_FILE_RELAY_STATE", "SDLS_SA_DB")


@contextlib.contextmanager
def clean_env(**values):
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class InstanceNamesTest(unittest.TestCase):
    def test_default_instance_keeps_the_old_names(self):
        with clean_env():
            self.assertEqual(gs_instance.tm_port(), 1235)
            self.assertEqual(gs_instance.ipc_address(), "ipc:///tmp/GroundSystem")
            self.assertEqual(gs_instance.offset_file(), "/tmp/OffsetData")
            gs_instance.apply(1235)
            self.assertNotIn("SDLP_LOG", os.environ)  # sdlp_link keeps /tmp/sdlp_link.log
            self.assertEqual(gs_instance.title("Command System"), "Command System [TM 1235]")

    def test_every_resource_follows_the_port(self):
        with clean_env():
            self.assertEqual(gs_instance.apply("1245"), 1245)
            self.assertEqual(os.environ["GS_INSTANCE"], "1245")
            self.assertEqual(gs_instance.tm_port(), 1245)
            self.assertEqual(gs_instance.ipc_address(), "ipc:///tmp/GroundSystem-1245")
            self.assertEqual(gs_instance.offset_file(), "/tmp/OffsetData-1245")
            self.assertEqual(os.environ["SDLP_LOG"], "/tmp/sdlp_link-1245.log")
            self.assertTrue(os.environ["GS_FILE_RELAY_STATE"].endswith(f"gs_file_relay_{os.getuid()}-1245.json"))
            self.assertEqual(os.environ["SDLS_SA_DB"], "/sdls_sa_db-1245")
            self.assertEqual(gs_instance.title("File Relay"), "File Relay [TM 1245]")

    def test_a_path_set_by_hand_is_kept(self):
        with clean_env(SDLP_LOG="/tmp/mine.log"):
            gs_instance.apply(1245)
            self.assertEqual(os.environ["SDLP_LOG"], "/tmp/mine.log")

    def test_only_port_numbers(self):
        for bad in ("B", "0", "70000", "-1", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gs_instance.check_port(bad)

    def test_lib_sdls_paths_follow_the_instance(self):
        import file_relay  # noqa: PLC0415

        with clean_env():
            gs_instance.apply(1245)
            self.assertTrue(str(file_relay.state_path()).endswith("-1245.json"))


class RoutingServiceInstanceTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(private_link())
        self.module, _ = stack.enter_context(routing_service_module())
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.tx.close)

    def start(self, port):
        with clean_env():
            gs_instance.apply(port)
            service = self.module.RoutingService()
        os.environ["GS_INSTANCE"] = str(port)  # run() reads the port
        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        self.addCleanup(service.sock.close)
        end = time.monotonic() + 3
        while time.monotonic() < end and (not thread.is_alive() or service.sock.getsockname()[1] != port):
            if not thread.is_alive():
                break
            time.sleep(0.01)
        os.environ.pop("GS_INSTANCE", None)
        return service, thread

    def frame(self, packet):
        framer = SatTmFramer(sdlp_only=SDLP_ONLY)
        framer.forward_packet(packet)
        framer.send_frame()
        return framer.sent[0]

    def test_two_instances_side_by_side(self):
        pa, pb = free_port(), free_port()
        a, _ = self.start(pa)
        b, _ = self.start(pb)
        self.assertEqual(a.publisher.endpoint, f"ipc:///tmp/GroundSystem-{pa}")
        self.assertEqual(b.publisher.endpoint, f"ipc:///tmp/GroundSystem-{pb}")
        self.assertEqual((a.sock.getsockname()[1], b.sock.getsockname()[1]), (pa, pb))
        self.tx.sendto(self.frame(make_spp(0x100, b"to a")), ("127.0.0.1", pa))
        self.tx.sendto(self.frame(make_spp(0x101, b"to b")), ("127.0.0.1", pb))
        end = time.monotonic() + 3
        while time.monotonic() < end and not (a.publisher.sent and b.publisher.sent):
            time.sleep(0.01)
        self.assertEqual([p[1] for p in a.publisher.sent], [make_spp(0x100, b"to a")])
        self.assertEqual([p[1] for p in b.publisher.sent], [make_spp(0x101, b"to b")])

    def test_port_in_use_says_what_to_do(self):
        busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        busy.bind(("", 0))
        self.addCleanup(busy.close)
        out = []
        self.module.print = lambda *a, **k: out.append(" ".join(map(str, a)))
        self.addCleanup(lambda: delattr(self.module, "print"))
        _, thread = self.start(busy.getsockname()[1])
        thread.join(3)
        self.assertFalse(thread.is_alive())  # returned instead of raising
        self.assertIn("--instance <port>", "\n".join(out))


@contextlib.contextmanager
def ground_system_module():
    """GroundSystem.py importable without Qt (only parse_args is used)."""
    qtwidgets = types.ModuleType("PyQt5.QtWidgets")
    for name in ("QApplication", "QMainWindow", "QMessageBox"):
        setattr(qtwidgets, name, type(name, (), {}))
    ui = types.ModuleType("UiMainWindow")
    ui.UiMainWindow = type("UiMainWindow", (), {})
    with routing_service_module():
        pyqt5 = sys.modules["PyQt5"]
        pyqt5.QtWidgets = qtwidgets
        saved = {k: sys.modules.get(k) for k in ("PyQt5.QtWidgets", "UiMainWindow", "GroundSystem")}
        sys.modules.update({"PyQt5.QtWidgets": qtwidgets, "UiMainWindow": ui})
        sys.modules.pop("GroundSystem", None)
        sys.path.insert(0, str(REPO))
        try:
            import GroundSystem  # noqa: PLC0415

            yield GroundSystem
        finally:
            sys.path.remove(str(REPO))
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class GroundSystemArgsTest(unittest.TestCase):
    def test_instance_option(self):
        with ground_system_module() as gs:
            args, rest = gs.parse_args(["--instance", "1245", "-style", "fusion"])
            self.assertEqual((args.instance, rest), (1245, ["-style", "fusion"]))
            self.assertEqual(gs.parse_args([])[0].instance, 1235)
            with open(os.devnull, "w") as quiet, contextlib.redirect_stderr(quiet), self.assertRaises(SystemExit):
                gs.parse_args(["--instance", "B"])


class WindowsFollowTheInstanceTest(unittest.TestCase):
    def test_minicmdutil_uses_the_instance_offsets(self):
        port = free_port()
        with clean_env(GS_INSTANCE=str(port)), tempfile.TemporaryDirectory():
            path = gs_instance.offset_file()
            self.assertTrue(path.endswith(f"-{port}"))
            from MiniCmdUtil import MiniCmdUtil  # noqa: PLC0415 - cmdGui is on sys.path (test_groundsystem)

            with self.assertRaises(FileNotFoundError):  # the default /tmp/OffsetData is not used
                MiniCmdUtil("127.0.0.1", 9, "LE", "0x1882", 0)
            with open(path, "wb") as f:
                f.write(bytes(4))
            self.addCleanup(os.remove, path)
            MiniCmdUtil("127.0.0.1", 9, "LE", "0x1882", 0)

    def test_file_relay_add_dest_port_is_the_instance_port(self):
        from test_file_relay import CMD_PAGE, load  # noqa: PLC0415

        with clean_env(GS_INSTANCE="1245"), offset_file():
            mod = load(CMD_PAGE)
        self.assertEqual(mod.DEFAULT_TM_PORT, 1245)


if __name__ == "__main__":
    unittest.main()
