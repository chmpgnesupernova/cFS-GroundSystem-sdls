#!/usr/bin/env python3
"""Handcraft TC: type every SPP field in hex, see the packet on one line, send it.

Opened from the Command System main page ("Handcraft TC"). The packet goes out exactly
as typed (wrong length or checksum included, on purpose if wanted) through the same SDLP
path as every other command: sdlp_link.encapsulate_tc() -> UDP to --host:--port.

    python3 HandcraftCommand.py --host=127.0.0.1 --port=1234
"""

import argparse
import socket
import sys
from pathlib import Path

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QApplication, QDialog, QGridLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QVBoxLayout)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib_sdls"))
import gs_instance  # noqa: E402
import sdlp_link  # noqa: E402

import spp_craft  # noqa: E402

# Example: TO_LAB NOOP (MID 0x1882, func 0), length and checksum already right.
DEFAULTS = {
    "packet_id": "1882",
    "seq_ctrl": "c000",
    "length": "0001",
    "func_code": "00",
    "checksum": "a4",
    "data": "",
}
LABELS = (
    ("packet_id", "Packet ID (2 B)"),
    ("seq_ctrl", "Seq Control (2 B)"),
    ("length", "Length (2 B)"),
    ("func_code", "Func Code (1 B)"),
    ("checksum", "Checksum (1 B)"),
    ("data", "Data / params (n B)"),
)
OK_STYLE = ""
BAD_STYLE = "background-color: #ffd6d6;"
WARN_COLOR = "#b36b00"
SOURCE = "Handcraft"


class HandcraftCommand(QDialog):

    def __init__(self, host, port, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.packet = None
        self.setWindowTitle(gs_instance.title("Handcraft TC"))
        self.resize(760, 360)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.TypeWriter)

        grid = QGridLayout()
        self.edits, self.hints = {}, {}
        for row, (name, text) in enumerate(LABELS):
            edit = QLineEdit(DEFAULTS[name])
            edit.setFont(mono)
            edit.textChanged.connect(self.update_preview)
            hint = QLabel()
            hint.setFont(mono)
            grid.addWidget(QLabel(text), row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(hint, row, 3)
            self.edits[name], self.hints[name] = edit, hint
        self.edits["data"].setPlaceholderText("e.g. 01 02 0a0b (empty = no params)")

        self.btn_calc_length = QPushButton("Calc")
        self.btn_calc_length.setToolTip("Set Length to (total bytes - 7)")
        self.btn_calc_length.clicked.connect(self.calc_length)
        self.btn_calc_checksum = QPushButton("Calc")
        self.btn_calc_checksum.setToolTip("Set Checksum so the XOR of all bytes is 0xFF")
        self.btn_calc_checksum.clicked.connect(self.calc_checksum)
        grid.addWidget(self.btn_calc_length, 2, 2)
        grid.addWidget(self.btn_calc_checksum, 4, 2)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.preview = QLineEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono)
        self.lbl_size = QLabel()
        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel("SPP:"))
        preview_row.addWidget(self.preview, 1)
        preview_row.addWidget(self.lbl_size)

        self.lbl_status = QLabel()
        self.lbl_status.setWordWrap(True)
        self.btn_send = QPushButton("Send")
        self.btn_send.clicked.connect(self.send)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        for button in (self.btn_calc_length, self.btn_calc_checksum, self.btn_close):
            button.setAutoDefault(False)
        buttons = QHBoxLayout()
        buttons.addWidget(QLabel(f"Target: {host}:{port}  (SDLP, {sdlp_mode()})"))
        buttons.addStretch(1)
        buttons.addWidget(self.btn_send)
        buttons.addWidget(self.btn_close)

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addSpacing(8)
        layout.addLayout(preview_row)
        layout.addWidget(self.lbl_status)
        layout.addStretch(1)
        layout.addLayout(buttons)
        self.update_preview()

    def texts(self):
        return {name: edit.text() for name, edit in self.edits.items()}

    def update_preview(self):
        fields, errors = spp_craft.parse_fields(self.texts())
        for name, edit in self.edits.items():
            edit.setStyleSheet(BAD_STYLE if name in errors else OK_STYLE)
            edit.setToolTip(errors.get(name, ""))
            self.hints[name].setText(errors.get(name, ""))
            self.hints[name].setStyleSheet("color: red;" if name in errors else "")
        if fields is None:
            self.packet = None
            self.preview.setText("")
            self.lbl_size.setText("")
            self.btn_send.setEnabled(False)
            return

        self._hint("packet_id", spp_craft.describe_packet_id(fields.packet_id))
        self._hint("seq_ctrl", spp_craft.describe_seq_ctrl(fields.seq_ctrl))
        self._hint("length", *spp_craft.describe_length(fields))
        self._hint("checksum", *spp_craft.describe_checksum(fields))
        self._hint("data", f"{len(fields.data)} B" if fields.data else "none")
        self.packet = fields.to_bytes()
        self.preview.setText(spp_craft.preview(self.packet))
        self.lbl_size.setText(f"{len(self.packet)} B")
        self.btn_send.setEnabled(True)

    def _hint(self, name, text, ok=True):
        self.hints[name].setText(text)
        self.hints[name].setStyleSheet("" if ok else f"color: {WARN_COLOR};")

    def calc_length(self):
        try:
            data = spp_craft.parse_data(self.edits["data"].text())
        except ValueError:
            return
        self.edits["length"].setText(f"{spp_craft.calc_length(data):04x}")

    def calc_checksum(self):
        texts = self.texts()
        texts["checksum"] = "00"
        fields, _ = spp_craft.parse_fields(texts)
        if fields is not None:
            self.edits["checksum"].setText(f"{spp_craft.calc_checksum(fields):02x}")

    def send(self):
        if self.packet is None:
            return False
        try:
            frame = sdlp_link.encapsulate_tc(self.packet, source=SOURCE)
        except sdlp_link.SdlpError as exc:
            self._status(f"Not sent: {exc}", ok=False)
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(frame, (self.host, self.port))
        except OSError as exc:
            self._status(f"Not sent: {exc}", ok=False)
            return False
        self._status(f"Sent {len(self.packet)} B SPP in a {len(frame)} B frame "
                     f"to {self.host}:{self.port}", ok=True)
        return True

    def _status(self, text, ok):
        print(text)
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color: green;" if ok else "color: red;")


def sdlp_mode():
    try:
        return sdlp_link.mode_name()
    except Exception:  # library missing: shown again as the Send error
        return "library not loaded"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Handcraft a TC space packet")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1234)
    args, _ = parser.parse_known_args(argv)
    return args


if __name__ == "__main__":
    args = parse_args()
    app = QApplication(sys.argv)
    page = HandcraftCommand(args.host, args.port)
    page.show()
    page.raise_()
    sys.exit(app.exec_())
