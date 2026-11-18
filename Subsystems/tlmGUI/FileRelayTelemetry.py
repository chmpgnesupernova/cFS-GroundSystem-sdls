#!/usr/bin/env python3
"""File Relay telemetry: one display, Transmit above Receive.

    Transmit  relay status 0x08C2: how the uplinked file is doing on board (state, bytes,
              CRC check, missing ranges the uplink must send again)
    Receive   relay segments 0x08C3 with this station's DestTag: the file being sent down,
              saved to the output folder (lib_sdls/file_rx.FileReceiver)

Opened from the Telemetry System main page ("File Relay"). Packets come from RoutingService
over zmq, already joined across TM frames. The display writes a small state file
(lib_sdls/file_relay.state_path) that the File Relay command window reads for DATA (gaps),
DONE (CRC) and RESEND (missing ranges).
"""

import argparse
import os
import sys
import time
from pathlib import Path

import zmq
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QVBoxLayout)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib_sdls"))
import file_relay  # noqa: E402
import file_rx  # noqa: E402

DEFAULT_OUT = Path.home() / "gs_received"
MAX_GAPS_SHOWN = 32


def subscriptions(sub):
    """zmq topics for 0x08C2 and 0x08C3 from the --sub the Telemetry System passes
    (GroundSystem[.<spacecraft>.TelemetryPackets].0x8c2)."""
    base = sub.rsplit(".", 1)[0] if sub and sub.count(".") >= 1 else "GroundSystem"
    if base == "GroundSystem":
        base = "GroundSystem.Spacecraft1.TelemetryPackets"
    return [f"{base}.{hex(file_rx.FILE_RELAY_STATUS_TLM_MID)}", f"{base}.{hex(file_rx.FILE_RELAY_SEG_TLM_MID)}"]


def format_gaps(gaps, limit=MAX_GAPS_SHOWN):
    lines = [f"{off:>9}  +{length}" for off, length in list(gaps)[:limit]]
    if len(gaps) > limit:
        lines.append(f"... {len(gaps) - limit} more")
    return "\n".join(lines)


class TlmReceiver(QThread):
    """zmq subscriber: emits each relay packet."""

    packet = pyqtSignal(bytes)

    def __init__(self, topics):
        super().__init__()
        self.runs = True
        self.context = zmq.Context()
        self.subscriber = self.context.socket(zmq.SUB)
        self.subscriber.connect("ipc:///tmp/GroundSystem")
        for topic in topics:
            self.subscriber.setsockopt_string(zmq.SUBSCRIBE, topic)

    def run(self):
        while self.runs:
            if self.subscriber.poll(200):
                _, datagram = self.subscriber.recv_multipart()
                self.packet.emit(datagram)


class FileRelayTelemetry(QDialog):

    def __init__(self, topics=(), out_dir=DEFAULT_OUT, dest_tag=2, state_path=None, parent=None):
        super().__init__(parent)
        self.state_path = state_path
        self.transmit = None  # last relay status
        self.status_count = 0
        self.segment_counts = {file_rx.TYPE_START: 0, file_rx.TYPE_DATA: 0, file_rx.TYPE_END: 0}
        self.setWindowTitle("File Relay")
        self.resize(640, 720)

        # --- Transmit (0x08C2) ---
        self.tx_state = QLabel("-")
        self.tx_file = QLabel("-")
        self.tx_tag = QLabel("-")
        self.tx_bar = QProgressBar()
        self.tx_bytes = QLabel("-")
        self.tx_crc = QLabel("-")
        self.tx_gaps = QLabel("-")
        self.tx_gap_list = QPlainTextEdit()
        self.tx_gap_list.setReadOnly(True)
        self.tx_gap_list.setMaximumHeight(90)
        self.tx_updated = QLabel("no status yet")
        tx = QGridLayout()
        for row, (name, widget) in enumerate((("State", self.tx_state), ("File", self.tx_file),
                                              ("DestTag", self.tx_tag), ("Progress", self.tx_bar),
                                              ("Bytes on board", self.tx_bytes), ("CRC-32", self.tx_crc),
                                              ("Missing", self.tx_gaps), ("", self.tx_gap_list),
                                              ("Updated", self.tx_updated))):
            tx.addWidget(QLabel(name), row, 0)
            tx.addWidget(widget, row, 1)
        tx_box = QGroupBox(f"Transmit: relay status ({hex(file_rx.FILE_RELAY_STATUS_TLM_MID)})")
        tx_box.setLayout(tx)

        # --- Receive (0x08C3) ---
        self.spin_tag = QSpinBox()
        self.spin_tag.setRange(0, 255)
        self.spin_tag.setValue(dest_tag)
        self.spin_tag.setToolTip("Keep only segments for this DestTag")
        self.spin_tag.valueChanged.connect(self._set_tag)
        self.edit_out = QLineEdit(str(out_dir))
        self.btn_out = QPushButton("Browse...")
        self.btn_out.setAutoDefault(False)
        self.btn_out.clicked.connect(self.browse)
        self.rx_state = QLabel("-")
        self.rx_file = QLabel("-")
        self.rx_bar = QProgressBar()
        self.rx_bytes = QLabel("-")
        self.rx_crc = QLabel("-")
        self.rx_gaps = QLabel("-")
        self.rx_gap_list = QPlainTextEdit()
        self.rx_gap_list.setReadOnly(True)
        self.rx_gap_list.setMaximumHeight(90)
        self.rx_saved = QLabel("-")
        self.rx_segments = QLabel("-")
        out_row = QHBoxLayout()
        out_row.addWidget(self.edit_out, 1)
        out_row.addWidget(self.btn_out)
        rx = QGridLayout()
        rx.addWidget(QLabel("DestTag"), 0, 0)
        rx.addWidget(self.spin_tag, 0, 1)
        rx.addWidget(QLabel("Save to"), 1, 0)
        rx.addLayout(out_row, 1, 1)
        for row, (name, widget) in enumerate((("State", self.rx_state), ("File", self.rx_file),
                                              ("Progress", self.rx_bar), ("Bytes received", self.rx_bytes),
                                              ("CRC-32", self.rx_crc), ("Missing", self.rx_gaps),
                                              ("", self.rx_gap_list), ("Saved", self.rx_saved),
                                              ("Segments", self.rx_segments)), start=2):
            rx.addWidget(QLabel(name), row, 0)
            rx.addWidget(widget, row, 1)
        rx_box = QGroupBox(f"Receive: relay segments ({hex(file_rx.FILE_RELAY_SEG_TLM_MID)})")
        rx_box.setLayout(rx)

        self.btn_close = QPushButton("Close")
        self.btn_close.setAutoDefault(False)
        self.btn_close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.btn_close)

        layout = QVBoxLayout(self)
        layout.addWidget(tx_box)
        layout.addWidget(rx_box)
        layout.addLayout(bottom)

        self.receiver = file_rx.FileReceiver(out_dir, dest_tag=dest_tag, mids=(file_rx.FILE_RELAY_SEG_TLM_MID,))
        self._show_receive()
        self._write_state()

        self.thread = None
        if topics:
            self.thread = TlmReceiver(topics)
            self.thread.packet.connect(self.handle_packet)
            self.thread.start()

    # --- settings ---

    def _set_tag(self, value):
        self.receiver.dest_tag = value

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, "Save received files to", self.edit_out.text())
        if path:
            self.edit_out.setText(path)
            self.receiver.out_dir = Path(path)  # used from the next START

    # --- packets ---

    def handle_packet(self, packet):
        mid = file_rx.packet_mid(packet)
        if mid == file_rx.FILE_RELAY_STATUS_TLM_MID:
            status = file_rx.parse_status(packet)
            if status is None:
                return
            self.transmit = status
            self.status_count += 1
            self._show_transmit()
            self._write_state()
        elif mid == file_rx.FILE_RELAY_SEG_TLM_MID:
            if self.receiver.state != file_rx.RECEIVING:  # a new file goes to the folder shown now
                self.receiver.out_dir = Path(self.edit_out.text().strip() or DEFAULT_OUT)
            seg = self.receiver.handle_packet(packet)
            if seg is None:
                return
            self.segment_counts[seg.type] = self.segment_counts.get(seg.type, 0) + 1
            self._show_receive()
            self._write_state()

    # --- display ---

    def _show_transmit(self):
        s = self.transmit
        self.tx_state.setText(s["state"])
        self.tx_file.setText(s["name"] or "-")
        self.tx_tag.setText(str(s["dest_tag"]))
        self.tx_bar.setMaximum(max(s["size"], 1))
        self.tx_bar.setValue(min(s["received"], s["size"]))
        self.tx_bytes.setText(f"{s['received']} / {s['size']} B")
        if s["state"] in ("COMPLETE", "FAILED"):
            match = "match" if s["file_crc"] == s["expected_crc"] else "MISMATCH"
            self.tx_crc.setText(f"expected {s['expected_crc']:08x}, on board {s['file_crc']:08x} ({match})")
        else:
            self.tx_crc.setText(f"expected {s['expected_crc']:08x}")
        shown = len(s["gaps"])
        self.tx_gaps.setText(f"{s['total_gaps']} range(s)" + (f", first {shown} below" if shown < s["total_gaps"]
                                                               else ""))
        self.tx_gap_list.setPlainText(format_gaps(s["gaps"]))
        self.tx_updated.setText(f"{time.strftime('%H:%M:%S')} (status #{self.status_count})")

    def _show_receive(self):
        r = self.receiver
        c = self.segment_counts
        self.rx_segments.setText(f"START {c[file_rx.TYPE_START]}, DATA {c[file_rx.TYPE_DATA]}, "
                                 f"END {c[file_rx.TYPE_END]}")
        if r.state == file_rx.IDLE:
            self.rx_state.setText("waiting for START")
            return
        state = r.state
        if state == file_rx.COMPLETE and not r.verified:
            state += " (END not received: CRC not checked)"
        self.rx_state.setText(state)
        self.rx_file.setText(f"{r.name} ({r.size} B)")
        self.rx_bar.setMaximum(max(r.size, 1))
        self.rx_bar.setValue(r.received)
        self.rx_bytes.setText(f"{r.received} / {r.size} B")
        parts = []
        if r.end_crc is not None:
            parts.append(f"END {r.end_crc:08x}")
        if r.file_crc is not None:
            parts.append(f"file {r.file_crc:08x}")
        self.rx_crc.setText(", ".join(parts) or "-")
        gaps = r.missing()
        self.rx_gaps.setText(f"{len(gaps)} range(s)" if gaps else "none")
        self.rx_gap_list.setPlainText(format_gaps(gaps))
        if r.state == file_rx.COMPLETE:
            self.rx_saved.setText(str(r.path))
        elif r.state == file_rx.CRC_MISMATCH:
            self.rx_saved.setText(f"not saved, kept as {r.part_path}")
        else:
            self.rx_saved.setText("-")

    def _write_state(self):
        """After every relay packet (about 10 a second during a file): small JSON, atomic."""
        try:
            file_relay.write_state({"time": time.time(), "transmit": self.transmit,
                                    "receive": file_relay.receive_state(self.receiver)}, self.state_path)
        except OSError as exc:
            print(f"File Relay: cannot write {self.state_path or file_relay.state_path()}: {exc}")

    def closeEvent(self, event):  # noqa: N802 - Qt name
        if self.thread is not None:
            self.thread.runs = False
            self.thread.wait(2000)
        self.receiver.close()
        super().closeEvent(event)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="File Relay telemetry")
    parser.add_argument("--sub", default="GroundSystem")
    parser.add_argument("--out", default=os.environ.get("GS_FILE_RELAY_OUT", str(DEFAULT_OUT)))
    parser.add_argument("--tag", type=int, default=2)
    args, _ = parser.parse_known_args(argv)  # the Telemetry System also passes --title, --appid, ...
    return args


if __name__ == "__main__":
    args = parse_args()
    app = QApplication(sys.argv)
    page = FileRelayTelemetry(subscriptions(args.sub), Path(args.out), args.tag)
    page.show()
    page.raise_()
    sys.exit(app.exec_())
