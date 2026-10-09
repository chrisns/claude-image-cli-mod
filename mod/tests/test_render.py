"""Tests for bin/render.py. Run with: python3 -m unittest discover -s mod/tests -p 'test_*.py'

Every run of the helper uses a cache folder of its own (INLINE_IMAGES_CACHE),
never the real one. The fixtures are PNGs written by render.png_bytes, so most
tests run without Pillow too, against ImageMagick.
"""

import base64
import contextlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from types import SimpleNamespace
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
HELPER = os.path.join(BIN, "render.py")
sys.path.insert(0, BIN)

import render  # noqa: E402

try:
    from PIL import Image
except ImportError:  # the tests that need Pillow are skipped
    Image = None

HAVE_MAGICK = render.magick() is not None
WORK = None  # a TemporaryDirectory for the whole module
HIDE_PILLOW = None  # a folder with a `PIL` package that will not import
SAVED_CACHE = None


def setUpModule():
    global WORK, HIDE_PILLOW, SAVED_CACHE

    WORK = tempfile.TemporaryDirectory()
    SAVED_CACHE = os.environ.get(render.CACHE_VARIABLE)
    os.environ[render.CACHE_VARIABLE] = os.path.join(WORK.name, "cache")
    HIDE_PILLOW = os.path.join(WORK.name, "hide-pillow")
    os.makedirs(os.path.join(HIDE_PILLOW, "PIL"))

    with open(os.path.join(HIDE_PILLOW, "PIL", "__init__.py"), "w") as handle:
        handle.write("raise ImportError('Pillow is hidden for this test')\n")


def tearDownModule():
    if SAVED_CACHE is None:
        os.environ.pop(render.CACHE_VARIABLE, None)
    else:
        os.environ[render.CACHE_VARIABLE] = SAVED_CACHE

    WORK.cleanup()


def run(*args, stdin=None, env=None, hide_pillow=False):
    extra = dict(env or {})

    if hide_pillow:
        extra["PYTHONPATH"] = HIDE_PILLOW

    result = subprocess.run(
        [sys.executable, HELPER, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **extra},
        timeout=60,
    )
    lines = result.stdout.strip().splitlines()

    if not lines:
        raise AssertionError("no reply; stderr: %s" % result.stderr)

    return result.returncode, json.loads(lines[-1])


def png(width, height, colour):
    """A PNG of one colour, written without Pillow."""
    return render.png_bytes(width, height, bytes(colour) * width * height)


def png_base64(width, height, colour):
    return base64.b64encode(png(width, height, colour)).decode("ascii")


def png_header_only(width, height):
    """A PNG that claims a size but has almost no pixels: a decompression bomb."""

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0" * 64))
        + chunk(b"IEND", b"")
    )


def grey16_png(width, height, value):
    """A 16-bit grey PNG, written by hand so that no decoder is needed to make it."""

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    raw = b"".join(b"\0" + struct.pack(">H", value) * width for _ in range(height))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def png_size(path):
    with open(path, "rb") as handle:
        head = handle.read(24)

    return struct.unpack(">II", head[16:24])


def decode_cells(reply, columns, rows):
    raw = base64.b64decode(reply["cells"])
    assert len(raw) == columns * rows * 12  # three u32 words per cell
    return [tuple(int.from_bytes(raw[i + j : i + j + 4], "little") for j in (0, 4, 8)) for i in range(0, len(raw), 12)]


def own_cache(name):
    """A cache folder of its own, for a test that must start from an empty one."""
    return {render.CACHE_VARIABLE: os.path.join(WORK.name, name)}


class Cases:
    """The tests that run the same with Pillow and with ImageMagick alone."""

    hide_pillow = False

    def helper(self, *args, **options):
        return run(*args, hide_pillow=self.hide_pillow, **options)

    def inspect(self, width=40, height=20, colour=(255, 0, 0, 255)):
        code, reply = self.helper("inspect", stdin=png_base64(width, height, colour))
        self.assertEqual(code, 0, reply)
        return reply

    def cells(self, reply, columns, rows, *extra):
        # Cells 8 wide and 16 tall have square sub-pixels, so a picture of the grid's shape fills it.
        code, out = self.helper("cells", reply["path"], "--columns", str(columns), "--rows", str(rows), *extra)
        self.assertEqual(code, 0, out)
        return decode_cells(out, columns, rows)

    def test_inspect_reports_format_and_size(self):
        reply = self.inspect(40, 20)
        self.assertEqual((reply["format"], reply["width"], reply["height"], reply["frames"]), ("PNG", 40, 20, 1))
        self.assertGreater(reply["bytes"], 0)
        self.assertTrue(reply["path"].startswith(os.environ[render.CACHE_VARIABLE]))

    def test_inspect_rejects_data_that_is_not_an_image(self):
        code, reply = self.helper("inspect", stdin=base64.b64encode(b"not an image at all").decode())
        self.assertNotEqual(code, 0)
        self.assertFalse(reply["ok"])

    def test_inspect_rejects_empty_data(self):
        code, reply = self.helper("inspect", stdin="")
        self.assertNotEqual(code, 0)
        self.assertFalse(reply["ok"])

    def test_only_raster_formats_reach_a_decoder(self):
        # Each would start Ghostscript, a PDF or SVG renderer, or MSL, if a decoder guessed its type.
        payloads = {
            "PostScript": b"%!PS-Adobe-3.0\n0 0 moveto showpage\n",
            "EPS": b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\n",
            "PDF": b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n",
            "SVG": b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
            "XML": b'<?xml version="1.0"?><image><read filename="x.png"/></image>',
            "URL": b"https://example.com/picture.png",
        }

        for name, payload in payloads.items():
            started = time.monotonic()
            code, reply = self.helper("inspect", stdin=base64.b64encode(payload).decode())
            self.assertNotEqual(code, 0, name)
            # This message comes from the check of the first bytes, before any decoder runs.
            self.assertIn("not a supported image format", reply["error"], name)
            self.assertLess(time.monotonic() - started, 5, name)

    def test_a_stored_file_is_checked_again_before_it_is_drawn(self):
        reply = self.inspect(4, 4)
        planted = os.path.join(os.path.dirname(reply["path"]), "planted.png")

        with open(planted, "wb") as handle:
            handle.write(b"%!PS-Adobe-3.0\nshowpage\n")

        code, out = self.helper("cells", planted, "--columns", "2", "--rows", "2")
        self.assertNotEqual(code, 0)
        self.assertIn("not a supported image format", out["error"])

    def test_a_decompression_bomb_is_refused_quickly(self):
        for width, height in ((7000, 7000), (60000, 60000)):
            started = time.monotonic()
            code, reply = self.helper("inspect", stdin=base64.b64encode(png_header_only(width, height)).decode())
            self.assertNotEqual(code, 0, (width, reply))
            self.assertFalse(reply["ok"])
            self.assertLess(time.monotonic() - started, 5)

    def test_sixteen_bit_grey_stays_grey(self):
        code, reply = self.helper("inspect", stdin=base64.b64encode(grey16_png(8, 8, 0x8080)).decode())
        self.assertEqual(code, 0, reply)

        for glyph, foreground, background in self.cells(reply, 2, 1):
            red, green, blue = background >> 16, background >> 8 & 0xFF, background & 0xFF
            self.assertEqual(red, green)
            self.assertEqual(green, blue)
            self.assertTrue(100 < red < 160, hex(background))  # grey, not white

    def test_a_flat_colour_is_a_solid_cell(self):
        for glyph, foreground, background in self.cells(self.inspect(40, 40, colour=(255, 128, 0, 255)), 4, 2):
            self.assertEqual(glyph, ord(" "))
            self.assertEqual(background, 0xFF8000)

    def test_a_split_picture_picks_a_half_block(self):
        # 2 x 2 pixels is one cell, so nothing is resampled and the colours stay exact.
        pixels = bytes((255, 0, 0, 255)) * 2 + bytes((0, 0, 255, 255)) * 2
        data = base64.b64encode(render.png_bytes(2, 2, pixels)).decode()
        code, reply = self.helper("inspect", stdin=data)
        self.assertEqual(code, 0, reply)
        glyph, foreground, background = self.cells(reply, 1, 1, "--cell-height", "8")[0]
        # Either half block draws it: the split is the same with the colours swapped.
        self.assertIn(chr(glyph), "▀▄")
        is_upper = chr(glyph) == "▀"
        top, bottom = (foreground, background) if is_upper else (background, foreground)
        self.assertEqual((top, bottom), (0xFF0000, 0x0000FF))

    def test_a_see_through_picture_keeps_the_terminal_colour(self):
        for glyph, foreground, background in self.cells(self.inspect(30, 60, colour=(0, 0, 0, 0)), 3, 3):
            self.assertEqual(background, 0x01000000)

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

    def test_the_png_of_a_tiny_picture_has_whole_pixel_rows(self):
        reply = self.inspect(1, 1, colour=(0, 0, 255, 255))

        for rows in (1, 3, 5, 7):
            code, out = self.helper("png", reply["path"], "--columns", "10", "--rows", str(rows))
            self.assertEqual(code, 0, out)
            self.assertEqual(png_size(out["path"]), (out["width"], out["height"]))
            # iterm_overlay.py cuts the PNG into rows: each must get the same whole number of pixels.
            self.assertEqual(out["height"] % rows, 0, out)

    def test_a_new_cell_size_or_stretch_is_a_new_marker(self):
        reply = self.inspect(40, 20)
        markers = set()
        paths = set()

        for extra in ((), ("--cell-height", "18"), ("--cell-width", "9"), ("--stretch",), ("--palette", "8")):
            code, out = self.helper("cells", reply["path"], "--columns", "10", "--rows", "4", "--marker", *extra)
            self.assertEqual(code, 0, out)
            markers.add(out["marker"])

            with open(os.path.join(os.environ[render.CACHE_VARIABLE], "overlays", out["marker"] + ".json")) as handle:
                paths.add(json.load(handle)["png"])

        self.assertEqual(len(markers), 5)
        self.assertEqual(len(paths), 4)  # the palette changes the glyphs, not the PNG

    def test_scan_finds_images_in_a_saved_output(self):
        data = png_base64(10, 10, (0, 255, 0, 255))
        half = len(data) // 2
        esc, bel = "\x1b", "\x07"
        wrapped = f"{esc}]1337;File=inline=1:{data}{bel}".replace(esc, esc + esc)
        text = (
            "some output\n"
            f"{esc}]1337;MultipartFile=inline=1;name=YS5wbmc={bel}"
            f"{esc}]1337;FilePart={data[:half]}{bel}{esc}]1337;FilePart={data[half:]}{bel}{esc}]1337;FileEnd{bel}\n"
            f"{esc}]1337;File=inline=1:{data}{esc}\\\n"
            f"{esc}Ptmux;{wrapped}{esc}\\\n"  # inside tmux's passthrough
            f"{esc}]1337;File=inline=1:{data[:20]}"  # cut off: ignored
        )

        with tempfile.NamedTemporaryFile("w", suffix=".txt", dir=WORK.name, delete=False) as handle:
            handle.write(text)

        code, reply = self.helper("scan", handle.name)
        os.remove(handle.name)
        self.assertEqual(code, 0, reply)
        self.assertEqual([image["width"] for image in reply["images"]], [10, 10, 10])
        self.assertEqual(reply["images"][0]["args"], "inline=1;name=YS5wbmc=")
        self.assertEqual(os.path.basename(reply["images"][0]["named"]), "a.png")

    def test_the_tmux_pattern_is_quick_on_a_hostile_output(self):
        size = 5 * 1024 * 1024
        hostile = (
            b"\x1bPtmux;" + b"\x1b\x1bA" * (size // 6)  # doubled escapes with no end
            + (b"\x1bPtmux;" + b"B" * 40) * (size // 94)  # many starts with no end
        )

        with tempfile.NamedTemporaryFile("wb", suffix=".txt", dir=WORK.name, delete=False) as handle:
            handle.write(hostile)

        started = time.monotonic()
        code, reply = self.helper("scan", handle.name)
        elapsed = time.monotonic() - started
        os.remove(handle.name)
        self.assertEqual(code, 0, reply)
        self.assertEqual(reply["images"], [])
        self.assertLess(elapsed, 2)

    def test_inspect_reads_a_file_and_names_a_link(self):
        path = os.path.join(WORK.name, "picture.png")

        with open(path, "wb") as handle:
            handle.write(png(6, 4, (1, 2, 3, 255)))

        code, reply = self.helper("inspect", "--file", path, "--name", "some/dir/holiday photo")
        self.assertEqual(code, 0, reply)
        self.assertTrue(reply["path"].endswith(".png"))  # an extension, so a viewer opens it
        self.assertEqual(os.path.basename(reply["named"]), "holiday photo.png")
        self.assertTrue(os.path.samefile(reply["path"], reply["named"]))

    def test_a_name_cannot_leave_the_cache(self):
        code, reply = self.helper("inspect", "--name", "../../../etc/passwd", stdin=png_base64(2, 2, (0, 0, 0, 255)))
        self.assertEqual(code, 0, reply)
        self.assertEqual(os.path.basename(reply["named"]), "passwd.png")
        self.assertIn(os.sep + "named" + os.sep, reply["named"])

    def test_inspect_says_why_a_file_cannot_be_read(self):
        code, reply = self.helper("inspect", "--file", "/nonexistent/picture.jpg")
        self.assertNotEqual(code, 0)
        self.assertIn("cannot read", reply["error"])

    def test_a_missing_file_is_an_error_not_a_traceback(self):
        code, reply = self.helper("cells", "/nonexistent/file.img", "--columns", "2", "--rows", "2")
        self.assertNotEqual(code, 0)
        self.assertFalse(reply["ok"])

    def test_the_same_picture_inspected_at_once_by_six_processes(self):
        data = png_base64(300, 200, (10, 200, 30, 255))
        environment = {**os.environ, **own_cache("concurrent-%s" % self.hide_pillow)}

        if self.hide_pillow:
            environment["PYTHONPATH"] = HIDE_PILLOW

        processes = [
            subprocess.Popen(
                [sys.executable, HELPER, "inspect", "--name", "same.png"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            for _ in range(6)
        ]

        for process in processes:
            process.stdin.write(data)
            process.stdin.close()

        replies = []

        for process in processes:
            out = process.stdout.read()
            process.wait(timeout=60)
            process.stdout.close()
            process.stderr.close()
            replies.append(json.loads(out.strip().splitlines()[-1]))

        self.assertTrue(all(reply["ok"] for reply in replies), replies)
        self.assertEqual(len({reply["path"] for reply in replies}), 1)
        self.assertTrue(all(reply.get("named", "").endswith("same.png") for reply in replies), replies)
        cache = os.path.dirname(replies[0]["path"])
        leftovers = [name for _, _, names in os.walk(cache) for name in names if name.endswith(".part")]
        self.assertEqual(leftovers, [])


class PillowRenderTests(Cases, unittest.TestCase):
    """With Pillow, when it is installed."""

    def setUp(self):
        if Image is None:
            self.skipTest("Pillow is not installed")

    def save(self, picture, **options):
        out = io.BytesIO()
        picture.save(out, **options)
        return base64.b64encode(out.getvalue()).decode("ascii")

    def test_a_palette_limits_the_colours(self):
        gradient = Image.linear_gradient("L").resize((64, 64)).convert("RGBA")
        code, reply = run("inspect", stdin=self.save(gradient, format="PNG"))
        self.assertEqual(code, 0, reply)
        colours = {c for cell in self.cells(reply, 32, 16, "--palette", "4") for c in cell[1:]}
        self.assertLessEqual(len(colours), 4)

    def test_a_palette_ignores_see_through_pixels(self):
        # Half see-through, half two colours: the palette of two must be those two colours.
        picture = Image.new("RGBA", (8, 4), (0, 0, 0, 0))
        picture.paste((255, 0, 0, 255), (0, 0, 4, 2))
        picture.paste((0, 0, 255, 255), (0, 2, 4, 4))
        code, reply = run("inspect", stdin=self.save(picture, format="PNG"))
        # Cells 8 x 8: square sub-pixels, so the 8 x 4 picture fills the 4 x 2 grid and is not resampled.
        cells = self.cells(reply, 4, 2, "--palette", "2", "--cell-height", "8")
        colours = {c for cell in cells for c in cell[1:]} - {0x01000000}
        self.assertEqual(colours, {0xFF0000, 0x0000FF})

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

    def test_png_never_smooths_a_small_picture_up(self):
        reply = self.inspect(40, 20)
        code, out = run("png", reply["path"], "--columns", "100", "--rows", "50", "--cell-width", "8", "--cell-height", "16")
        self.assertEqual(code, 0, out)

        with Image.open(out["path"]) as picture:
            # The 40 x 40 box PNG grows only to the next multiple of the 50 rows, by repeating pixels.
            self.assertEqual(picture.size, (50, 50))
            box = picture.getbbox()
            self.assertEqual((box[2] - box[0], box[3] - box[1]), (50, 25))
            self.assertEqual({picture.getpixel((x, y)) for x in range(50) for y in range(50)} - {(0, 0, 0, 0)}, {(255, 0, 0, 255)})

    def test_frames_of_an_animation_are_counted(self):
        frames = [Image.new("RGB", (16, 16), colour) for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
        out = io.BytesIO()
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
        code, reply = run("inspect", stdin=base64.b64encode(out.getvalue()).decode())
        self.assertEqual(code, 0, reply)
        self.assertEqual((reply["format"], reply["frames"]), ("GIF", 3))
        self.assertTrue(reply["path"].endswith(".gif"))

    def test_an_mpo_is_stored_as_a_jpeg(self):
        first, second = Image.new("RGB", (16, 16), (10, 20, 30)), Image.new("RGB", (16, 16), (200, 20, 30))
        out = io.BytesIO()
        first.save(out, "MPO", save_all=True, append_images=[second])
        code, reply = run("inspect", stdin=base64.b64encode(out.getvalue()).decode())
        self.assertEqual(code, 0, reply)
        self.assertEqual(reply["format"], "MPO")
        self.assertTrue(reply["path"].endswith(".jpg"))

    def test_a_turned_jpeg_reports_its_upright_size(self):
        picture = Image.new("RGB", (200, 100), (255, 0, 0))
        exif = picture.getexif()
        exif[0x0112] = 6  # turned a quarter
        code, reply = run("inspect", stdin=self.save(picture, format="JPEG", exif=exif))
        self.assertEqual((reply["width"], reply["height"]), (100, 200))
        code, out = run("png", reply["path"], "--columns", "10", "--rows", "10", "--cell-width", "10", "--cell-height", "10")
        self.assertEqual(code, 0, out)

        with Image.open(out["path"]) as box:
            visible = box.getbbox()
            self.assertGreater(visible[3] - visible[1], visible[2] - visible[0])  # taller than wide


@unittest.skipUnless(HAVE_MAGICK, "ImageMagick is not installed")
class MagickRenderTests(Cases, unittest.TestCase):
    """With Pillow hidden: ImageMagick decodes, with an explicit coder and limits."""

    hide_pillow = True

    def test_pillow_is_really_hidden(self):
        result = subprocess.run(
            [sys.executable, "-c", "import PIL"], env={**os.environ, "PYTHONPATH": HIDE_PILLOW}, capture_output=True
        )
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipIf(Image is None, "Pillow is needed to make the fixture")
    def test_a_turned_jpeg_reports_its_upright_size(self):
        picture = Image.new("RGB", (200, 100), (255, 0, 0))
        exif = picture.getexif()
        exif[0x0112] = 6
        out = io.BytesIO()
        picture.save(out, "JPEG", exif=exif)
        code, reply = self.helper("inspect", stdin=base64.b64encode(out.getvalue()).decode())
        self.assertEqual(code, 0, reply)
        self.assertEqual((reply["width"], reply["height"]), (100, 200))
        # The pixels that convert -auto-orient makes must match that size exactly.
        code, cells = self.helper("cells", reply["path"], "--columns", "5", "--rows", "5", "--stretch")
        self.assertEqual(code, 0, cells)


class InProcessTests(unittest.TestCase):
    """Parts of render.py called directly."""

    def test_prune_keeps_new_files_and_follows_no_links(self):
        folder = tempfile.mkdtemp(dir=WORK.name)
        outside = tempfile.mkdtemp(dir=WORK.name)
        old, new, target = os.path.join(folder, "old.png"), os.path.join(folder, "new.png"), os.path.join(outside, "keep")

        for path in (old, new, target):
            with open(path, "wb") as handle:
                handle.write(b"x")

        long_ago = time.time() - render.MAX_AGE_SECONDS - 60
        os.utime(old, (long_ago, long_ago))
        os.utime(target, (long_ago, long_ago))
        os.symlink(outside, os.path.join(folder, "linked-folder"))
        os.symlink(target, os.path.join(folder, "linked-file"))
        render.prune(folder)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(target))  # an old file, but only reached through links

    def test_reading_a_stored_image_keeps_it_from_a_prune(self):
        folder = render.cache_dir()
        path = os.path.join(folder, "touched.png")

        with open(path, "wb") as handle:
            handle.write(png(2, 2, (1, 2, 3, 255)))

        long_ago = time.time() - render.MAX_AGE_SECONDS - 60
        os.utime(path, (long_ago, long_ago))
        render.read_stored(path)
        render.prune(folder)
        self.assertTrue(os.path.exists(path))

    def test_a_name_is_cut_to_bytes_and_cleaned(self):
        stored = os.path.join(render.cache_dir(), "abc123.png")

        with open(stored, "wb") as handle:
            handle.write(b"x")

        named = render.named_copy(stored, "é" * 300)
        self.assertLessEqual(len(os.path.basename(named).encode()), render.MAX_NAME_BYTES + len(".png"))
        self.assertTrue(os.path.basename(named).endswith("é.png"))  # no half character left

        named = render.named_copy(stored, "a\x07b\x1b[31mc‮gnp.exe‏")
        self.assertEqual(os.path.basename(named), "ab[31mcgnp.exe.png")

    def test_a_name_that_cannot_be_made_is_left_out(self):
        stored = os.path.join(render.cache_dir(), "abc124.png")

        with open(stored, "wb") as handle:
            handle.write(b"x")

        with mock.patch.object(render.os, "makedirs", side_effect=OSError("read-only")):
            self.assertIsNone(render.named_copy(stored, "photo.png"))

    def test_an_unsafe_cache_folder_is_refused(self):
        real = tempfile.mkdtemp(dir=WORK.name)
        link = os.path.join(WORK.name, "linked-cache")
        os.symlink(real, link)
        code, reply = run("inspect", stdin=png_base64(2, 2, (0, 0, 0, 255)), env={render.CACHE_VARIABLE: link})
        self.assertNotEqual(code, 0)
        self.assertIn("link", reply["error"])

        with mock.patch.dict(os.environ, {render.CACHE_VARIABLE: link}):
            with self.assertRaises(render.UnsafeCache):
                render.cache_dir()

    def test_a_loose_cache_folder_of_ours_is_tightened(self):
        loose = os.path.join(WORK.name, "loose-cache")
        os.makedirs(loose)
        os.chmod(loose, 0o777)
        code, reply = run("inspect", stdin=png_base64(2, 2, (0, 0, 0, 255)), env={render.CACHE_VARIABLE: loose})
        self.assertEqual(code, 0, reply)
        self.assertEqual(stat.S_IMODE(os.lstat(loose).st_mode), 0o700)

    def test_another_users_cache_folder_is_refused(self):
        folder = tempfile.mkdtemp(dir=WORK.name)
        real = os.lstat(folder)
        stranger = os.stat_result((real.st_mode, *real[1:4], real.st_uid + 1, *real[5:]))

        with mock.patch.dict(os.environ, {render.CACHE_VARIABLE: folder}):
            with mock.patch.object(render.os, "lstat", return_value=stranger):
                with self.assertRaises(render.UnsafeCache):
                    render.cache_dir()

    def test_the_cache_is_in_the_users_home_by_default(self):
        environment = {key: value for key, value in os.environ.items() if key != render.CACHE_VARIABLE}

        with mock.patch.dict(os.environ, environment, clear=True):
            location = render.cache_location()

        home = os.path.expanduser("~")

        if sys.platform == "darwin":
            self.assertEqual(location, os.path.join(home, "Library", "Caches", "inline-images"))
        else:
            self.assertTrue(location.endswith(os.sep + "inline-images"))

    def test_cell_needs_no_terminal(self):
        # Whatever runs the tests, a missing or unreadable terminal gives no size and no error.
        for tty in (None, os.devnull):
            out = io.StringIO()

            with mock.patch.object(render, "tty_of_ancestors", return_value=tty), contextlib.redirect_stdout(out):
                render.command_cell(SimpleNamespace())

            self.assertEqual(json.loads(out.getvalue()), {"ok": True, "width": None, "height": None})

    def test_quadrant_fast_path_matches_the_search(self):
        colours = [None, (0, 0, 0), (255, 255, 255), (10, 20, 30)]

        for colour in colours:
            glyph, fg, bg = render.best_glyph((colour,) * 4)
            self.assertEqual((glyph, fg, bg), (" ", colour, colour))

    def test_nearest_without_pillow_only_repeats_pixels(self):
        pixels = bytes(range(256))[: 4 * 4 * 4]

        with mock.patch.object(render, "HAVE_PILLOW", False):
            plain = render.nearest(pixels, 4, 4, 7, 9)

        self.assertEqual(len(plain), 7 * 9 * 4)
        source = {pixels[at : at + 4] for at in range(0, len(pixels), 4)}
        self.assertTrue(all(plain[at : at + 4] in source for at in range(0, len(plain), 4)))  # no new colours


if __name__ == "__main__":
    unittest.main()
