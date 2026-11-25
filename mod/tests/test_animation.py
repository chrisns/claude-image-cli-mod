"""Animated pictures: render.py's frames, the animated box GIF, and the overlay's cut of it."""

import base64
import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "bin")
sys.path.insert(0, BIN)

try:
    from PIL import Image, ImageDraw

    HAVE_PILLOW = True
except ImportError:  # the ImageMagick job has no Pillow
    HAVE_PILLOW = False


def gif_bytes(count=6, size=(120, 80), duration=70):
    frames = []

    for index in range(count):
        frame = Image.new("RGB", size, (10, 10, 30))
        ImageDraw.Draw(frame).rectangle((index * 10, 20, index * 10 + 30, 60), fill=(255, 140, 0))
        frames.append(frame)

    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=duration, loop=0)
    return out.getvalue()


@unittest.skipUnless(HAVE_PILLOW, "animations need Pillow")
class Animation(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, INLINE_IMAGES_CACHE=os.path.join(self.folder.name, "cache"))

    def tearDown(self):
        self.folder.cleanup()

    def helper(self, *arguments, stdin=None):
        ran = subprocess.run(
            [sys.executable, os.path.join(BIN, "render.py"), *arguments], input=stdin, capture_output=True, text=True, env=self.env
        )
        return json.loads(ran.stdout)

    def store(self, data):
        return self.helper("inspect", "--name", "spin.gif", stdin=base64.b64encode(data).decode())

    def test_frames_as_png_files(self):
        stored = self.store(gif_bytes())
        self.assertEqual(stored["frames"], 6)
        reply = self.helper("frames", stored["path"], "--as", "png", "--columns", "15", "--rows", "5")

        self.assertEqual(len(reply["frames"]), 6)
        self.assertEqual({frame["duration"] for frame in reply["frames"]}, {70})

        with Image.open(reply["frames"][3]["path"]) as frame:
            self.assertEqual(frame.format, "PNG")

    def test_frames_as_cells(self):
        stored = self.store(gif_bytes())
        reply = self.helper("frames", stored["path"], "--as", "cells", "--columns", "15", "--rows", "5")

        self.assertEqual(len(reply["frames"]), 6)
        # 15 by 5 cells, three 32-bit words each.
        self.assertEqual(len(base64.b64decode(reply["frames"][0]["cells"])), 15 * 5 * 12)
        self.assertNotEqual(reply["frames"][0]["cells"], reply["frames"][3]["cells"])

    def test_a_too_short_frame_shows_for_100_ms(self):
        stored = self.store(gif_bytes(duration=0))
        reply = self.helper("frames", stored["path"], "--as", "png", "--columns", "15", "--rows", "5")

        self.assertEqual({frame["duration"] for frame in reply["frames"]}, {100})

    def test_a_still_picture_has_no_frames(self):
        out = io.BytesIO()
        Image.new("RGB", (20, 20), (1, 2, 3)).save(out, "PNG")
        stored = self.store(out.getvalue())

        self.assertEqual(self.helper("frames", stored["path"], "--as", "cells", "--columns", "5", "--rows", "2")["frames"], [])

    def test_the_overlay_box_of_a_gif_is_an_animated_gif_and_cuts_into_one(self):
        stored = self.store(gif_bytes())
        reply = self.helper(
            "cells", stored["path"], "--columns", "15", "--rows", "5", "--cell-width", "8", "--cell-height", "16", "--marker"
        )
        record = json.load(open(glob.glob(os.path.join(self.folder.name, "cache", "overlays", "*.json"))[0]))

        self.assertTrue(reply["ok"])
        self.assertTrue(record["png"].endswith(".gif"))

        with Image.open(record["png"]) as box:
            self.assertEqual(box.n_frames, 6)

        # The overlay cuts the rows a scrolled box still shows: every frame of them.
        os.environ["INLINE_IMAGES_CACHE"] = self.env["INLINE_IMAGES_CACHE"]
        import iterm_overlay

        overlays = iterm_overlay.Overlays()

        with open(record["png"], "rb") as handle:
            overlays.loaded[b"m"] = (record["columns"], record["rows"], handle.read())

        cut = overlays.rows(b"m", 1, 3)

        with Image.open(io.BytesIO(cut)) as rows:
            self.assertEqual(rows.format, "GIF")
            self.assertEqual(rows.n_frames, 6)


if __name__ == "__main__":
    unittest.main()
