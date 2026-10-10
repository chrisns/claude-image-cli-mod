#!/usr/bin/env python3
"""Turn a folder of window captures into a GIF for the README.

Every frame is cut where the last frame's input box starts (the cut that
crop-screenshot.py makes), less the line just above it, so the prompt
suggestion, the status lines and the notices Claude Code puts above the input
box (spinner, usage) never show. The start-up banner (the model and plan) is
taken out too. Runs of identical frames become one frame that is held as long.
ffmpeg builds the GIF with one palette for the whole clip.

    make-gif.py <frames folder> <out.gif> [--width 780] [--fps 4] [--line 20] [--still out.png]
"""

import argparse
import glob
import importlib.util
import os
import subprocess
import tempfile

from PIL import Image, ImageChops

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("crop", os.path.join(here, "crop-screenshot.py"))
crop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crop)


def cut_row(image):
    """The y where the input box starts, found as crop-screenshot.py finds it."""
    pixels = image.load()
    width, height = image.size
    groups = []

    for y in range(height - 1, height // 2, -1):
        if crop.is_rule(pixels, width, y):
            if groups and groups[-1][-1] - y <= 1:
                groups[-1].append(y)
            else:
                groups.append([y])

    bars = [group[-1] for group in groups if len(group) > 2]
    above = [group for group in groups if not bars or group[0] < min(bars)]
    thin = [group for group in above if len(group) <= 2]

    if len(thin) < 2:
        raise SystemExit("could not find the input box in the last frame")

    return thin[1][-1] - 4


TITLE = 24  # the height of the window's title bar, in points


def hide_banner(frame, scale):
    """Paint the start-up banner (model, plan, folder) black, where a frame still shows it.

    It sits between the title bar and the first prompt bar, the first row from
    the top that is one colour from edge to edge. The layout does not move.
    """
    pixels = frame.load()
    width, height = frame.size

    title = TITLE * scale

    for y in range(title + 4 * scale, height // 2):
        if crop.is_rule(pixels, width, y):
            if y - title > 30 * scale:
                frame.paste((0, 0, 0), (0, title + 2 * scale, width, y - 6 * scale))
            return frame

    return frame


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("frames")
    parser.add_argument("out")
    parser.add_argument("--width", type=int, default=780)
    parser.add_argument("--fps", type=int, default=4)
    parser.add_argument("--still", help="also save the last frame, cut the same way, as a PNG")
    parser.add_argument("--line", type=int, default=20, help="points above the input box to cut: the line of the spinner and any notice")
    arguments = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(arguments.frames, "*.png")))
    last = Image.open(paths[-1]).convert("RGB")
    # A Retina capture has two pixels for each point of the window.
    scale = max(1, round(last.width / arguments.width))
    cut = cut_row(last) - arguments.line * scale
    size = last.size

    with tempfile.TemporaryDirectory() as work:
        written = 0
        previous = None

        for path in paths:
            frame = Image.open(path).convert("RGB")

            if frame.size != size:
                continue

            frame = hide_banner(frame.crop((0, 0, size[0], cut)), scale)

            if frame.width > arguments.width:
                frame = frame.resize((arguments.width, round(frame.height * arguments.width / frame.width)), Image.LANCZOS)

            # A frame like the one before adds nothing but time: keep the time.
            if previous is not None and ImageChops.difference(frame, previous).getbbox() is None:
                frame = previous

            frame.save(os.path.join(work, "%05d.png" % written))
            previous = frame
            written += 1

        if arguments.still:
            previous.save(arguments.still, optimize=True)

        # Hold the last frame for two seconds.
        for extra in range(arguments.fps * 2):
            previous.save(os.path.join(work, "%05d.png" % (written + extra)))

        source = os.path.join(work, "%05d.png")
        palette = os.path.join(work, "palette.png")
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(arguments.fps), "-i", source,
             "-vf", "palettegen=stats_mode=diff", palette],
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(arguments.fps), "-i", source, "-i", palette,
             "-lavfi", "paletteuse=dither=sierra2_4a:diff_mode=rectangle", "-loop", "0", arguments.out],
            check=True,
        )

    print("wrote %s (%d frames, %.0f KB)" % (arguments.out, written, os.path.getsize(arguments.out) / 1024))


if __name__ == "__main__":
    main()
