"""Tests for bin/render.py. Run with: python3 -m unittest discover -s mod/tests -p 'test_*.py'"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

HELPER = os.path.join(os.path.dirname(__file__), "..", "bin", "render.py")

try:
    from PIL import Image
except ImportError:  # the tests that need Pillow are skipped
    Image = None


def run(*args, stdin=None, env=None):
    result = subprocess.run(
        [sys.executable, HELPER, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return result.returncode, json.loads(result.stdout.strip().splitlines()[-1])


def png_base64(width, height, colour):
    path = tempfile.mktemp(suffix=".png")
    Image.new("RGBA", (width, height), colour).save(path)

    with open(path, "rb") as handle:
        data = handle.read()

    os.remove(path)
    return base64.b64encode(data).decode("ascii")


@unittest.skipIf(Image is None, "Pillow is not installed")
class RenderTests(unittest.TestCase):
    def inspect(self, width=40, height=20, colour=(255, 0, 0, 255)):
        code, reply = run("inspect", stdin=png_base64(width, height, colour))
        self.assertEqual(code, 0, reply)
        return reply

    def test_inspect_reports_format_and_size(self):
        reply = self.inspect(40, 20)
        self.assertEqual((reply["format"], reply["width"], reply["height"], reply["frames"]), ("PNG", 40, 20, 1))
        self.assertGreater(reply["bytes"], 0)

    def test_inspect_rejects_data_that_is_not_an_image(self):
        code, reply = run("inspect", stdin=base64.b64encode(b"not an image at all").decode())
        self.assertNotEqual(code, 0)
        self.assertFalse(reply["ok"])

    def test_inspect_rejects_empty_data(self):
        code, reply = run("inspect", stdin="")
        self.assertNotEqual(code, 0)
        self.assertFalse(reply["ok"])

    def cells(self, reply, columns, rows, *extra):
        # Cells 8 wide and 16 tall have square sub-pixels, so a picture of the grid's shape fills it.
        code, out = run("cells", reply["path"], "--columns", str(columns), "--rows", str(rows), *extra)
        self.assertEqual(code, 0, out)
        raw = base64.b64decode(out["cells"])
        self.assertEqual(len(raw), columns * rows * 12)  # three u32 words per cell
        return [tuple(int.from_bytes(raw[i + j : i + j + 4], "little") for j in (0, 4, 8)) for i in range(0, len(raw), 12)]

    def test_a_flat_colour_is_a_solid_cell(self):
        for glyph, foreground, background in self.cells(self.inspect(40, 40, colour=(255, 128, 0, 255)), 4, 2):
            self.assertEqual(glyph, ord(" "))
            self.assertEqual(background, 0xFF8000)

    def test_a_split_picture_picks_a_half_block(self):
        path = tempfile.mktemp(suffix=".png")
        # 2 x 2 pixels is one cell, so nothing is resampled and the colours stay exact.
        top_red = Image.new("RGBA", (2, 2), (0, 0, 255, 255))
        top_red.paste((255, 0, 0, 255), (0, 0, 2, 1))
        top_red.save(path)

        with open(path, "rb") as handle:
            code, reply = run("inspect", stdin=base64.b64encode(handle.read()).decode())

        os.remove(path)
        glyph, foreground, background = self.cells(reply, 1, 1, "--cell-height", "8")[0]
        # Either half block draws it: the split is the same with the colours swapped.
        self.assertIn(chr(glyph), "▀▄")
        is_upper = chr(glyph) == "▀"
        top, bottom = (foreground, background) if is_upper else (background, foreground)
        self.assertEqual((top, bottom), (0xFF0000, 0x0000FF))

    def test_a_see_through_picture_keeps_the_terminal_colour(self):
        for glyph, foreground, background in self.cells(self.inspect(30, 60, colour=(0, 0, 0, 0)), 3, 3):
            self.assertEqual(background, 0x01000000)

    def test_a_palette_limits_the_colours(self):
        path = tempfile.mktemp(suffix=".png")
        gradient = Image.linear_gradient("L").resize((64, 64)).convert("RGBA")
        gradient.save(path)

        with open(path, "rb") as handle:
            code, reply = run("inspect", stdin=base64.b64encode(handle.read()).decode())

        os.remove(path)
        colours = {c for cell in self.cells(reply, 32, 16, "--palette", "4") for c in cell[1:]}
        self.assertLessEqual(len(colours), 4)

    def test_png_has_the_shape_of_its_box(self):
        reply = self.inspect(400, 200)
        # A box of 10 x 5 cells, each 8 x 16 pixels, is 80 x 80 pixels, drawn at twice that size
        # for a dense screen. The 2:1 picture sits in the middle with see-through margins.
        code, out = run("png", reply["path"], "--columns", "10", "--rows", "5", "--cell-width", "8", "--cell-height", "16")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["width"], out["height"]), (160, 160))

        with Image.open(out["path"]) as picture:
            self.assertEqual(picture.getpixel((80, 2))[3], 0)  # margin above
            self.assertEqual(picture.getpixel((80, 80))[3], 255)  # the picture

    def test_png_never_enlarges_a_picture(self):
        reply = self.inspect(40, 20)
        code, out = run("png", reply["path"], "--columns", "100", "--rows", "50", "--cell-width", "8", "--cell-height", "16")
        self.assertEqual(code, 0, out)

        with Image.open(out["path"]) as picture:
            box = picture.getbbox()
            self.assertEqual((box[2] - box[0], box[3] - box[1]), (40, 20))

    def test_stretch_fills_the_box(self):
        reply = self.inspect(40, 20, colour=(9, 9, 9, 255))
        cells = self.cells(reply, 4, 4, "--stretch")
        self.assertTrue(all(cell[2] == 0x090909 for cell in cells))

    def test_cells_letterbox_a_picture_that_does_not_fit_the_box(self):
        # A wide picture in a square grid leaves see-through rows at the top and bottom.
        cells = self.cells(self.inspect(80, 20, colour=(255, 0, 0, 255)), 8, 8)
        self.assertEqual(cells[0][2], 0x01000000)
        self.assertEqual(cells[-1][2], 0x01000000)
        middle = cells[4 * 8 + 4]
        self.assertEqual(middle[2], 0xFF0000)

    def test_cell_reports_a_size_or_nothing(self):
        code, reply = run("cell")
        self.assertEqual(code, 0)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["width"] is None, reply["height"] is None)

    def test_scan_finds_images_in_a_saved_output(self):
        data = png_base64(10, 10, (0, 255, 0, 255))
        half = len(data) // 2
        esc, bel = "\x1b", "\x07"
        text = (
            "some output\n"
            f"{esc}]1337;MultipartFile=inline=1;name=YS5wbmc={bel}"
            f"{esc}]1337;FilePart={data[:half]}{bel}{esc}]1337;FilePart={data[half:]}{bel}{esc}]1337;FileEnd{bel}\n"
            f"{esc}]1337;File=inline=1:{data}{esc}\\\n"
            f"{esc}]1337;File=inline=1:{data[:20]}"  # cut off: ignored
        )

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write(text)

        code, reply = run("scan", handle.name)
        os.remove(handle.name)
        self.assertEqual(code, 0, reply)
        self.assertEqual([image["width"] for image in reply["images"]], [10, 10])
        self.assertEqual(reply["images"][0]["args"], "inline=1;name=YS5wbmc=")

    def test_inspect_reads_a_file_and_names_a_link(self):
        path = tempfile.mktemp(suffix=".png")
        Image.new("RGB", (6, 4), (1, 2, 3)).save(path)
        code, reply = run("inspect", "--file", path, "--name", "some/dir/holiday photo")
        os.remove(path)
        self.assertEqual(code, 0, reply)
        self.assertTrue(reply["path"].endswith(".png"))  # an extension, so a viewer opens it
        self.assertEqual(os.path.basename(reply["named"]), "holiday photo.png")
        self.assertTrue(os.path.samefile(reply["path"], reply["named"]))

    def test_a_name_cannot_leave_the_cache(self):
        code, reply = run("inspect", "--name", "../../../etc/passwd", stdin=png_base64(2, 2, (0, 0, 0, 255)))
        self.assertEqual(code, 0, reply)
        self.assertEqual(os.path.basename(reply["named"]), "passwd.png")
        self.assertIn(os.sep + "named" + os.sep, reply["named"])

    def test_inspect_says_why_a_file_cannot_be_read(self):
        code, reply = run("inspect", "--file", "/nonexistent/picture.jpg")
        self.assertNotEqual(code, 0)
        self.assertIn("cannot read", reply["error"])

    def test_a_missing_file_is_an_error_not_a_traceback(self):
        code, reply = run("cells", "/nonexistent/file.img", "--columns", "2", "--rows", "2")
        self.assertNotEqual(code, 0)
        self.assertFalse(reply["ok"])


if __name__ == "__main__":
    unittest.main()
