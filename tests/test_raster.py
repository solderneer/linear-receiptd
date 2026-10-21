from pathlib import Path
import tempfile
import unittest

from PIL import Image

from linear_receiptd.config import Settings, write_private
from linear_receiptd.setup import printer_config
from linear_receiptd.worker import Printer


class RasterTests(unittest.TestCase):
    def test_coreprint_plain_roll_payload_preserves_rows_and_wire_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new("1", (384, 16), 1)
            for x in range(384):
                image.putpixel((x, 0), 0)
            image.putpixel((0, 1), 0)
            image.save(root / "sample.png")
            write_private(root / "printer.json", printer_config("AA:BB:CC:DD:EE:FF"))
            printer = Printer(Settings(user_id="test", printer_config=root / "printer.json"))
            payload = printer.prepare(root / "sample.png")
        # Init + density 15, GS v 0 raster width 48 bytes and height 16 rows,
        # one full black row, one black pixel, remaining white rows, four LFs.
        expected = bytes.fromhex("1b401d49f00f1d76300030001000")
        expected += b"\xff" * 48 + b"\x80" + b"\x00" * (48 * 15 - 1) + b"\n" * 4
        self.assertEqual(payload, expected)
