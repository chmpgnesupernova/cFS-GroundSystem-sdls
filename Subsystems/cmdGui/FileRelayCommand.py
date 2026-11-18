#!/usr/bin/env python3
"""File Relay commands: one window, uplink buttons on top, downlink buttons below.

    Uplink   OUTPUT_ENABLE  START  DATA  END  STATUS  CANCEL        (send a file to relay)
    Downlink ADD_DEST  DONE  REMOVE_DEST  RESEND                      (get a relayed file)

Opened from the Command System main page ("File Relay"). Every command goes through the
same SDLP path as the other pages (sdlp_link.encapsulate_tc -> UDP --host:--port).
DATA sends every chunk of the chosen file, or only the missing ones when the latest relay
status (File Relay telemetry window) lists gaps for it. DONE and RESEND take the CRC and
the missing ranges of the file the telemetry window is receiving.

    python3 FileRelayCommand.py --host=127.0.0.1 --port=1234
"""

import argparse
import sys
import time
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QSpinBox, QVBoxLayout)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib_sdls"))
import file_relay  # noqa: E402
import gs_instance  # noqa: E402
import file_rx  # noqa: E402

DEFAULT_TM_PORT = gs_instance.tm_port()  # this instance's RoutingService listens here
MAX_RESENDS = file_rx.FILE_DL_MAX_RESENDS


class ChunkSender(QThread):
    """Sends the DATA commands off the GUI thread (one per 20 ms)."""

    progress = pyqtSignal(int, int)
    done = pyqtSignal(int, str)

    def __init__(self, sender, upload, offsets):
        super().__init__()
        self.sender, self.upload, self.offsets = sender, upload, offsets
        self.stop = False

    def run(self):
        sent, error = file_relay.send_chunks(self.sender.send, self.upload, self.offsets, time.sleep,
                                             self.progress.emit, lambda: self.stop)
        self.done.emit(sent, error or "")


class FileRelayCommand(QDialog):

    def __init__(self, host, port, sender=None, parent=None):
        super().__init__(parent)
        self.host, self.port = host, port
        self.sender = sender or file_relay.TcSender(host, port)
        self.upload = None
        self.worker = None
        self.setWindowTitle(gs_instance.title("File Relay"))
        self.resize(640, 520)

        # --- uplink ---
        self.edit_my_ip = QLineEdit("127.0.0.1")
        self.edit_my_ip.setToolTip("This station's IP as the satellite sees it")
        self.edit_file = QLineEdit()
        self.edit_file.setPlaceholderText("file to relay (name at most 14 bytes, at most 4 MiB)")
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self.browse)
        self.spin_tag = QSpinBox()
        self.spin_tag.setRange(0, 255)
        self.spin_tag.setValue(2)
        self.spin_tag.setToolTip("DestTag: the station that gets the file")
        self.lbl_file = QLabel("")

        self.btn_output_enable = QPushButton("OUTPUT_ENABLE")
        self.btn_start = QPushButton("START")
        self.btn_data = QPushButton("DATA")
        self.btn_end = QPushButton("END")
        self.btn_status = QPushButton("STATUS")
        self.btn_cancel = QPushButton("CANCEL")
        self.btn_output_enable.setToolTip("TO_LAB: send the TM frames to this station (port 1235)")
        self.btn_start.setToolTip("Relay START: name, size and CRC-32 of the file, DestTag")
        self.btn_data.setToolTip("Send the file in 960 B chunks (only the missing ones after a status with gaps)")
        self.btn_end.setToolTip("All chunks sent: the satellite lists what is missing or checks the CRC")
        self.btn_status.setToolTip("Ask for the relay status again")
        self.btn_cancel.setToolTip("Drop the transfer on board")
        self.btn_output_enable.clicked.connect(self.output_enable)
        self.btn_start.clicked.connect(self.start)
        self.btn_data.clicked.connect(self.data)
        self.btn_end.clicked.connect(lambda: self._send(file_relay.end_command(), "END"))
        self.btn_status.clicked.connect(lambda: self._send(file_relay.status_command(), "STATUS"))
        self.btn_cancel.clicked.connect(lambda: self._send(file_relay.cancel_command(), "CANCEL"))

        up = QGridLayout()
        up.addWidget(QLabel("My IP"), 0, 0)
        up.addWidget(self.edit_my_ip, 0, 1)
        up.addWidget(QLabel("File"), 1, 0)
        up.addWidget(self.edit_file, 1, 1)
        up.addWidget(self.btn_browse, 1, 2)
        up.addWidget(QLabel("DestTag"), 2, 0)
        up.addWidget(self.spin_tag, 2, 1)
        up.addWidget(self.lbl_file, 3, 0, 1, 3)
        up_buttons = QHBoxLayout()
        for button in (self.btn_output_enable, self.btn_start, self.btn_data, self.btn_end, self.btn_status,
                       self.btn_cancel):
            up_buttons.addWidget(button)
        up_box = QGroupBox("Uplink: send a file to relay")
        up_layout = QVBoxLayout(up_box)
        up_layout.addLayout(up)
        up_layout.addLayout(up_buttons)

        # --- downlink ---
        self.spin_tm_port = QSpinBox()
        self.spin_tm_port.setRange(1, 65535)
        self.spin_tm_port.setValue(DEFAULT_TM_PORT)
        self.spin_tm_port.setToolTip("UDP port this station gets TM frames on")
        self.edit_crc = QLineEdit()
        self.edit_crc.setPlaceholderText("CRC-32 of the received file (hex)")
        self.lbl_received = QLabel("")
        self.lbl_missing = QLabel("")
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setToolTip("Read the received file's CRC and missing ranges from the File Relay telemetry")
        self.btn_refresh.clicked.connect(self.refresh)

        self.btn_add_dest = QPushButton("ADD_DEST")
        self.btn_done = QPushButton("DONE")
        self.btn_remove_dest = QPushButton("REMOVE_DEST")
        self.btn_resend = QPushButton("RESEND")
        self.btn_add_dest.setToolTip("TO_LAB: also send the TM frames to My IP : TM port")
        self.btn_done.setToolTip("File received: the satellite compares the CRC and closes the file")
        self.btn_remove_dest.setToolTip("TO_LAB: stop sending the TM frames to My IP : TM port")
        self.btn_resend.setToolTip("Ask for the missing ranges again (up to 64)")
        self.btn_add_dest.clicked.connect(lambda: self.dest(True))
        self.btn_done.clicked.connect(self.done_)
        self.btn_remove_dest.clicked.connect(lambda: self.dest(False))
        self.btn_resend.clicked.connect(self.resend)

        down = QGridLayout()
        down.addWidget(QLabel("TM port"), 0, 0)
        down.addWidget(self.spin_tm_port, 0, 1)
        down.addWidget(QLabel("CRC-32"), 1, 0)
        down.addWidget(self.edit_crc, 1, 1)
        down.addWidget(self.btn_refresh, 1, 2)
        down.addWidget(self.lbl_received, 2, 0, 1, 3)
        down.addWidget(self.lbl_missing, 3, 0, 1, 3)
        down_buttons = QHBoxLayout()
        for button in (self.btn_add_dest, self.btn_done, self.btn_remove_dest, self.btn_resend):
            down_buttons.addWidget(button)
        down_box = QGroupBox("Downlink: get a relayed file")
        down_layout = QVBoxLayout(down_box)
        down_layout.addLayout(down)
        down_layout.addLayout(down_buttons)

        # --- status line, close ---
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel(f"Target: {host}:{port}"))
        bottom.addStretch(1)
        bottom.addWidget(self.btn_close)

        layout = QVBoxLayout(self)
        layout.addWidget(up_box)
        layout.addWidget(down_box)
        layout.addWidget(self.lbl_status)
        layout.addStretch(1)
        layout.addLayout(bottom)

        # Enter in a field must not press a button.
        for button in (self.btn_browse, self.btn_output_enable, self.btn_start, self.btn_data, self.btn_end,
                       self.btn_status, self.btn_cancel, self.btn_refresh, self.btn_add_dest, self.btn_done,
                       self.btn_remove_dest, self.btn_resend, self.btn_close):
            button.setAutoDefault(False)
        self.missing = []
        self.refresh()

    # --- helpers ---

    def _status(self, text, ok=True):
        print(text)
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color: green;" if ok else "color: red;")

    def _send(self, packet, what):
        error = self.sender.send(packet)
        if error:
            self._status(f"{what} not sent: {error}", ok=False)
            return False
        self._status(f"{what} sent to {self.host}:{self.port}")
        return True

    def _my_ip(self):
        return self.edit_my_ip.text().strip()

    def _load_upload(self):
        try:
            self.upload = file_relay.UploadFile(self.edit_file.text().strip())
        except ValueError as exc:
            self.upload = None
            self.lbl_file.setText("")
            self._status(str(exc), ok=False)
            return None
        u = self.upload
        self.lbl_file.setText(f"{u.board_name}: {u.size} B, {len(file_relay.plan_chunks(u.size))} chunks, "
                              f"crc32 {u.crc32:08x}")
        return u

    # --- uplink ---

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "File to relay")
        if path:
            self.edit_file.setText(path)
            self._load_upload()

    def output_enable(self):
        try:
            packet = file_relay.output_enable_command(self._my_ip())
        except ValueError as exc:
            self._status(str(exc), ok=False)
            return False
        return self._send(packet, f"OUTPUT_ENABLE {self._my_ip()}")

    def start(self):
        u = self._load_upload()
        if u is None:
            return False
        return self._send(file_relay.relay_start_command(u.name, u.size, u.crc32, self.spin_tag.value()),
                          f"START {u.board_name} ({u.size} B, tag {self.spin_tag.value()})")

    def data(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop = True
            return False
        u = self.upload if self.upload and str(self.upload.path) == self.edit_file.text().strip() else None
        u = u or self._load_upload()
        if u is None:
            return False
        state = file_relay.read_state() or {}
        gaps = file_relay.gaps_for(u, state.get("transmit"))
        offsets = file_relay.plan_chunks(u.size, gaps)
        what = f"{len(offsets)} missing chunk(s)" if gaps else f"all {len(offsets)} chunk(s)"
        self._status(f"DATA: sending {what}")
        self.btn_data.setText("STOP DATA")
        self.worker = ChunkSender(self.sender, u, offsets)
        self.worker.progress.connect(lambda i, n: self.lbl_status.setText(f"DATA {i}/{n}"))
        self.worker.done.connect(self._data_done)
        self.worker.start()
        return True

    def _data_done(self, sent, error):
        self.btn_data.setText("DATA")
        if error:
            self._status(f"DATA: {sent} chunk(s) sent, then {error}", ok=False)
        else:
            self._status(f"DATA: {sent} chunk(s) sent. Send END next.")

    # --- downlink ---

    def refresh(self):
        """The received file's CRC and missing ranges, from the telemetry window."""
        rx = (file_relay.read_state() or {}).get("receive")
        if not rx:
            self.lbl_received.setText("Received: nothing yet (File Relay telemetry window)")
            self.lbl_missing.setText("")
            self.missing = []
            return
        self.missing = [tuple(g) for g in rx.get("missing") or []]
        self.lbl_received.setText(f"Received: {rx['name']} {rx['received']}/{rx['size']} B, {rx['state']}")
        self.lbl_missing.setText(f"Missing: {len(self.missing)} range(s)" if self.missing else "Missing: none")
        crc = rx.get("file_crc")
        if crc is not None and rx.get("state") == file_rx.COMPLETE:
            self.edit_crc.setText(f"{crc:08x}")

    def dest(self, add):
        build = file_rx.add_dest_command if add else file_rx.remove_dest_command
        try:
            packet = build(self._my_ip(), self.spin_tm_port.value())
        except ValueError as exc:
            self._status(str(exc), ok=False)
            return False
        name = "ADD_DEST" if add else "REMOVE_DEST"
        return self._send(packet, f"{name} {self._my_ip()}:{self.spin_tm_port.value()}")

    def done_(self):
        self.refresh()
        text = self.edit_crc.text().strip().lower().removeprefix("0x")
        try:
            crc = int(text, 16)
            if not 0 <= crc <= 0xFFFFFFFF:
                raise ValueError
        except ValueError:
            self._status("DONE needs the CRC-32 of the received file (8 hex digits)", ok=False)
            return False
        return self._send(file_rx.done_command(crc), f"DONE crc {crc:08x}")

    def resend(self):
        self.refresh()
        if not self.missing:
            self._status("RESEND: nothing is missing", ok=False)
            return False
        ranges = self.missing[:MAX_RESENDS]
        for off, length in ranges:
            if not self._send(file_rx.resend_command(off, length), f"RESEND {off}+{length}"):
                return False
            time.sleep(file_relay.CMD_GAP)
        self._status(f"RESEND: {len(ranges)} range(s) asked again")
        return True

    def closeEvent(self, event):  # noqa: N802 - Qt name
        if self.worker is not None:
            self.worker.stop = True
            self.worker.wait(2000)
        self.sender.close()
        super().closeEvent(event)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="File Relay commands")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1234)
    args, _ = parser.parse_known_args(argv)
    return args


if __name__ == "__main__":
    args = parse_args()
    app = QApplication(sys.argv)
    page = FileRelayCommand(args.host, args.port)
    page.show()
    page.raise_()
    sys.exit(app.exec_())
