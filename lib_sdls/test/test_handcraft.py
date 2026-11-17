"""Handcraft TC page (Subsystems/cmdGui/HandcraftCommand.py) and its packet logic
(spp_craft.py). The packet logic tests always run; the window tests need PyQt5 and run
without a display (Qt offscreen platform)."""

import contextlib
import os
import socket
import unittest
import unittest.mock

from sat_reference import sat_tc_extract_packet
from test_groundsystem import _pyqt5_available, offset_file  # also puts cmdGui on sys.path
from test_sdlp_link import private_link, read_log

import spp_craft  # noqa: E402 - Subsystems/cmdGui is on sys.path (test_groundsystem)

NOOP = bytes.fromhex("1882c0000001" "00a4")  # TO_LAB NOOP, the page's example


def texts(**overrides):
    base = {"packet_id": "1882", "seq_ctrl": "c000", "length": "0001",
            "func_code": "00", "checksum": "a4", "data": ""}
    base.update(overrides)
    return base


class ParseTest(unittest.TestCase):
    def test_fixed_fields(self):
        self.assertEqual(spp_craft.parse_fixed("1882", 2), 0x1882)
        self.assertEqual(spp_craft.parse_fixed("0x1882", 2), 0x1882)
        self.assertEqual(spp_craft.parse_fixed(" 18 82 ", 2), 0x1882)
        self.assertEqual(spp_craft.parse_fixed("1", 2), 0x0001)  # padded on the left
        self.assertEqual(spp_craft.parse_fixed("A4", 1), 0xA4)

    def test_bad_fixed_fields(self):
        for text, size in (("", 2), ("   ", 1), ("12345", 2), ("100", 1), ("zz", 1), ("0x", 1)):
            with self.subTest(text=text), self.assertRaises(ValueError):
                spp_craft.parse_fixed(text, size)

    def test_data(self):
        self.assertEqual(spp_craft.parse_data(""), b"")
        self.assertEqual(spp_craft.parse_data("01 02 0a0B"), bytes([1, 2, 10, 11]))
        self.assertEqual(spp_craft.parse_data("0x01 0x02"), bytes([1, 2]))
        for bad in ("123", "0g"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                spp_craft.parse_data(bad)

    def test_errors_are_reported_per_field(self):
        fields, errors = spp_craft.parse_fields(texts(seq_ctrl="xyz", data="1"))
        self.assertIsNone(fields)
        self.assertEqual(set(errors), {"seq_ctrl", "data"})


class BuildTest(unittest.TestCase):
    def test_example_is_the_noop_command(self):
        fields, errors = spp_craft.parse_fields(texts())
        self.assertEqual(errors, {})
        self.assertEqual(fields.to_bytes(), NOOP)
        self.assertEqual(spp_craft.preview(NOOP), "18 82 c0 00 00 01 00 a4")
        self.assertTrue(spp_craft.describe_length(fields)[1])
        self.assertTrue(spp_craft.describe_checksum(fields)[1])

    def test_fields_go_out_as_typed_even_when_wrong(self):
        fields, _ = spp_craft.parse_fields(texts(length="0010", checksum="00", data="aa bb"))
        self.assertEqual(fields.to_bytes(), bytes.fromhex("1882c000001000" "00aabb"))
        self.assertFalse(spp_craft.describe_length(fields)[1])
        self.assertFalse(spp_craft.describe_checksum(fields)[1])

    def test_calc_length_and_checksum(self):
        fields, _ = spp_craft.parse_fields(texts(func_code="02", checksum="00", data="0700"))
        fields.length = spp_craft.calc_length(fields.data)
        fields.checksum = spp_craft.calc_checksum(fields)
        packet = fields.to_bytes()
        self.assertEqual(fields.length, len(packet) - 7)
        self.assertEqual(spp_craft.xor_all(packet), 0xFF)

    def test_descriptions(self):
        self.assertEqual(spp_craft.describe_packet_id(0x1882),
                         "ver 0, cmd, sec hdr 1, APID 0x082")
        self.assertEqual(spp_craft.describe_packet_id(0x0801), "ver 0, tlm, sec hdr 1, APID 0x001")
        self.assertEqual(spp_craft.describe_seq_ctrl(0xC005), "flags 11 (unsegmented), count 5")

    def test_same_packet_as_minicmdutil(self):
        """Typed fields with Calc'd length and checksum == what a command page sends."""
        with offset_file():
            from MiniCmdUtil import MiniCmdUtil  # noqa: PLC0415

            with open("/tmp/OffsetData", "rb") as f:
                if any(f.read(4)):
                    self.skipTest("GroundSystem offsets in /tmp/OffsetData are not zero")
            mcu = MiniCmdUtil("127.0.0.1", 9, "LE", "0x1880", 2, "--uint16=7 --uint8=3")
            mcu.assemble_packet()
        expected = bytes(mcu.packet)
        fields, _ = spp_craft.parse_fields(
            texts(packet_id="1880", func_code="02", checksum="00", data="0700 03",
                  seq_ctrl=expected[2:4].hex()))
        fields.length = spp_craft.calc_length(fields.data)
        fields.checksum = spp_craft.calc_checksum(fields)
        self.assertEqual(fields.to_bytes(), expected)


@unittest.skipUnless(_pyqt5_available(), "PyQt5 not installed")
class HandcraftPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication  # noqa: PLC0415

        cls.app = QApplication.instance() or QApplication([])
        import HandcraftCommand  # noqa: PLC0415

        cls.module = HandcraftCommand

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.log = stack.enter_context(private_link())
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(0.5)
        self.addCleanup(self.rx.close)
        self.page = self.module.HandcraftCommand("127.0.0.1", self.rx.getsockname()[1])
        self.addCleanup(self.page.deleteLater)

    def test_example_shows_on_one_line(self):
        self.assertEqual(self.page.preview.text(), "18 82 c0 00 00 01 00 a4")
        self.assertEqual(self.page.lbl_size.text(), "8 B")
        self.assertTrue(self.page.btn_send.isEnabled())

    def test_bad_hex_disables_send(self):
        self.page.edits["func_code"].setText("1g")
        self.assertFalse(self.page.btn_send.isEnabled())
        self.assertEqual(self.page.preview.text(), "")
        self.assertIn("not hex", self.page.hints["func_code"].text())
        self.page.edits["func_code"].setText("00")
        self.assertTrue(self.page.btn_send.isEnabled())

    def test_calc_buttons(self):
        self.page.edits["data"].setText("07 00")
        self.page.edits["func_code"].setText("02")
        self.page.btn_calc_length.click()
        self.page.btn_calc_checksum.click()
        self.assertEqual(self.page.edits["length"].text(), "0003")
        self.assertEqual(spp_craft.xor_all(self.page.packet), 0xFF)
        self.assertIn("matches", self.page.hints["length"].text())

    def test_send_puts_the_packet_in_a_tc_frame(self):
        self.page.edits["data"].setText("aa")  # wrong length on purpose: sent anyway
        packet = self.page.packet
        self.page.btn_send.click()
        frame, _ = self.rx.recvfrom(2048)
        self.assertEqual(sat_tc_extract_packet(frame), packet)
        (entry,) = read_log(self.log)
        self.assertEqual((entry["dir"], entry["result"], entry["src"]), ("TC", "OK", "Handcraft"))
        self.assertEqual(entry["in"], packet.hex())
        self.assertIn("Sent 9 B", self.page.lbl_status.text())

    def test_send_fails_cleanly_without_the_library(self):
        os.environ["SDLS_LIB"] = "/nonexistent/libsdls.so"
        self.module.sdlp_link._lib = None
        self.assertFalse(self.page.send())
        with self.assertRaises(socket.timeout):
            self.rx.recvfrom(2048)
        self.assertIn("Not sent", self.page.lbl_status.text())


@unittest.skipUnless(_pyqt5_available(), "PyQt5 not installed")
class CommandSystemHandcraftButtonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication  # noqa: PLC0415

        cls.app = QApplication.instance() or QApplication([])
        import CommandSystem  # noqa: PLC0415

        cls.module = CommandSystem

    def test_button_opens_the_page_for_the_target_ip(self):
        window = self.module.CommandSystem()
        self.addCleanup(window.deleteLater)
        window.target_ip = "192.168.0.20"
        with unittest.mock.patch.object(self.module.subprocess, "Popen") as popen:
            window.btn_handcraft.click()
        (args,), _ = popen.call_args
        self.assertTrue(args[1].endswith("HandcraftCommand.py"))
        self.assertEqual(args[2:], ["--host=192.168.0.20", "--port=1234"])


if __name__ == "__main__":
    unittest.main()
