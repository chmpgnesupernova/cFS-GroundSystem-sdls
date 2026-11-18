"""Telemetry GUI display pages (Subsystems/tlmGUI/GenericTelemetry.py) started the way the
Telemetry System main page starts them, with PyQt5 and zmq replaced by small stand-ins so
it runs without a display or a running GroundSystem.

Regression for the page crashing on start with
    NameError: name 'tlm_item_desc' is not defined
(and, once past that, the receiver signal being connected under a wrong name).
"""

import contextlib
import csv
import runpy
import struct
import sys
import types
import unittest

from test_groundsystem import REPO, offset_file

TLM_DIR = REPO / "Subsystems" / "tlmGUI"
PAGE = TLM_DIR / "GenericTelemetry.py"


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _SignalAttr:
    """pyqtSignal stand-in: one _Signal per instance, like a bound Qt signal."""

    def __set_name__(self, owner, name):
        self.name = "_sig_" + name

    def __get__(self, obj, owner):
        if obj is None:
            return self
        return obj.__dict__.setdefault(self.name, _Signal())


class _QThread:
    finished = _SignalAttr()

    def __init__(self):
        pass

    def start(self):  # the receive loop needs zmq; packets are fed by the test instead
        pass

    def deleteLater(self):
        pass

    def wait(self, *_):
        return True


class _Item:
    def __init__(self):
        self.value = ""

    def setText(self, text):
        self.value = text

    def text(self):
        return self.value


class _Header:
    def setSectionResizeMode(self, *_):
        pass


class _Table:
    def __init__(self):
        self.rows = {}

    def insertRow(self, row):
        self.rows.setdefault(row, {})

    def setItem(self, row, col, item):
        self.rows[row][col] = item

    def item(self, row, col):
        return self.rows[row][col]

    def rowCount(self):
        return len(self.rows)

    def horizontalHeader(self):
        return _Header()

    verticalHeader = horizontalHeader


class _Value:
    def __init__(self):
        self.value = None

    def setText(self, v):
        self.value = v

    display = setValue = setText


shown = []


class _QDialog:
    def __init__(self, *_):
        pass

    def show(self):
        shown.append(self)

    def raise_(self):
        pass

    def setWindowTitle(self, title):
        self.title = title


class _Ui:
    def setupUi(self, _dialog):
        self.tbl_telemetry = _Table()
        self.sub_system_line_edit = _Value()
        self.packet_id = _Value()
        self.sequence_count = _Value()


class _QApplication:
    def __init__(self, *_):
        pass

    def exec_(self):
        return 0


class _Socket:
    def connect(self, *_):
        pass

    def setsockopt_string(self, *_):
        pass


@contextlib.contextmanager
def qt_stubs():
    qtcore = types.ModuleType("PyQt5.QtCore")
    qtcore.QThread, qtcore.pyqtSignal = _QThread, lambda *_: _SignalAttr()
    qtwidgets = types.ModuleType("PyQt5.QtWidgets")
    qtwidgets.QApplication, qtwidgets.QDialog = _QApplication, _QDialog
    qtwidgets.QHeaderView = types.SimpleNamespace(Stretch=1)
    qtwidgets.QTableWidgetItem = _Item
    pyqt5 = types.ModuleType("PyQt5")
    pyqt5.QtCore, pyqt5.QtWidgets = qtcore, qtwidgets
    ui = types.ModuleType("UiGenerictelemetrydialog")
    ui.UiGenerictelemetrydialog = _Ui
    zmq = types.ModuleType("zmq")
    zmq.Context = lambda: types.SimpleNamespace(socket=lambda *_: _Socket())
    zmq.SUB, zmq.SUBSCRIBE = 2, 6
    stubs = {"PyQt5": pyqt5, "PyQt5.QtCore": qtcore, "PyQt5.QtWidgets": qtwidgets,
             "UiGenerictelemetrydialog": ui, "zmq": zmq}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def start_page(title, appid, def_file):
    """Runs GenericTelemetry.py as TelemetrySystem.process_button_generic launches it and
    returns the page object."""
    argv = [str(PAGE), f"--title={title}", f"--appid={appid}", "--port=0",
            f"--file={def_file}", "--endian=L", f"--sub=GroundSystem.{appid}"]
    saved_argv, sys.argv = sys.argv, argv
    shown.clear()
    try:
        with qt_stubs(), offset_file(), contextlib.redirect_stdout(None):
            with unittest.TestCase().assertRaises(SystemExit) as ctx:
                runpy.run_path(str(PAGE), run_name="__main__")
    finally:
        sys.argv = saved_argv
    assert ctx.exception.code == 0, ctx.exception.code
    (page,) = shown
    return page


def generic_pages():
    """(title, appid, file) of every GenericTelemetry page in telemetry-pages.txt."""
    with open(TLM_DIR / "telemetry-pages.txt") as f:
        for row in csv.reader(f, skipinitialspace=True):
            if row and not row[0].startswith("#") and row[1].strip() == "GenericTelemetry.py":
                yield row[0].strip(), row[2].strip(), row[3].strip()


class GenericTelemetryPageTest(unittest.TestCase):
    def test_every_display_page_starts(self):
        pages = list(generic_pages())
        self.assertGreater(len(pages), 5)
        for title, appid, def_file in pages:
            with self.subTest(page=title):
                page = start_page(title, appid, def_file)
                self.assertGreater(page.tbl_telemetry.rowCount(), 0)
                self.assertEqual(page.sub_system_line_edit.value, title)

    def test_packet_is_decoded_into_the_table(self):
        page = start_page("ES HK Tlm", "0x800", "cfe-es-hk-tlm.txt")
        datagram = bytearray(200)
        datagram[0:4] = bytes.fromhex("0800c005")  # seq count 5
        datagram[12], datagram[13] = 7, 2  # command / error counters
        datagram[48:52] = struct.pack("<I", 5)  # registered core apps
        datagram[64:68] = struct.pack("<I", 2)  # reset type (enum)
        page.thread.gt_signal_tlm_datagram.emit(bytes(datagram))  # what the receiver thread emits
        table = {page.tbl_telemetry.item(k, 0).text(): page.tbl_telemetry.item(k, 1).text()
                 for k in range(page.tbl_telemetry.rowCount())}
        self.assertEqual(page.sequence_count.value, 5)
        self.assertEqual(table["Command Counter"], "7")
        self.assertEqual(table["Error Counter"], "2")
        self.assertEqual(table["Registered Core Apps"], "5")
        self.assertEqual(table["Reset Type"], "ResetType2")


if __name__ == "__main__":
    unittest.main()
