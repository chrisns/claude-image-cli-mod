#!/usr/bin/env python3
"""Decode an image and turn it into something a terminal can draw.

The mod calls this helper because the mod itself runs in a sandbox with no
image decoder. Three commands, each prints one line of JSON on stdout:

  inspect   Read base64 from stdin, store the image file in the cache and
            report its format and size in pixels.
  scan      Find the iTerm2 inline images in a text file (the saved output of
            a command that was too large to show), store each one and report
            it like `inspect` does, with the sequence's arguments.
  cells     Draw a stored image as a grid of coloured block characters, packed
            as Claude Code's `Raster` element wants it.
  png       Write a stored image as a PNG file sized for a box of cells, for a
            terminal that draws real pixels (kitty, Ghostty).
  cell      Measure one terminal cell in pixels, from the window size that the
            terminal reports on the tty of this process or of a parent.

`cells --marker` is for iTerm2. It hides an id in the first cells of the grid
and keeps a PNG of the box. bin/iterm_overlay.py finds the id on the screen
and draws that PNG over the cells with the iTerm2 inline images protocol.

Pillow is the decoder. ImageMagick (`magick`) is the fallback when Pillow is
not installed. Only the Python standard library is needed besides those.
"""

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
from array import array

try:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 178_956_970  # Pillow's own bomb limit is a warning only
    HAVE_PILLOW = True
except ImportError:  # pragma: no cover - depends on the machine
    HAVE_PILLOW = False

MAX_BYTES = 64 * 1024 * 1024
MAX_AGE_SECONDS = 24 * 60 * 60

# Quadrant block glyphs. The index is a bitmask of the sub-pixels that take the
# foreground colour: bit 0 top left, 1 top right, 2 bottom left, 3 bottom right.
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"

DEFAULT_COLOUR = 0x01000000  # bit 24 alone: the terminal's own colour
SEE_THROUGH_COST = 3 * 255 * 255

# The marker that iterm_overlay.py looks for, in the first cells of every row of
# the grid: a check glyph, three glyphs for the id and one for the row. Braille,
# because no prompt, reply or diff draws it, and each glyph is one cell wide.
#
# Each glyph mixes in the row number, so no marker cell is the same as the one
# on the row above or below. When a box moves, every marker cell changes, so
# Claude Code paints every one of them again: a terminal that repaints only the
# cells that changed cannot leave a marker hidden under an old image.
MARKER_CELLS = 5
BLANK = 0x2800  # a braille cell with no dots: draws nothing, but is not a space


def marker_glyphs(marker, row):
    """The five glyphs of the marker of one row; iterm_overlay.py reads them back."""
    row &= 0xFF
    shuffled = [(byte + row * 37 + index * 11) & 0xFF for index, byte in enumerate(marker, start=1)]
    check = (marker[0] + marker[1] + marker[2] + row * 37 + 0x5A) & 0xFF

    return [BLANK + value for value in (check, *shuffled, row)]


def fail(message, code=1):
    print(json.dumps({"ok": False, "error": message}))
    sys.exit(code)


def cache_dir():
    path = os.path.join(tempfile.gettempdir(), "inline-images-%d" % os.getuid())
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def prune(directory):
    """Remove cached files nobody has touched for a day."""
    cutoff = time.time() - MAX_AGE_SECONDS

    for name in os.listdir(directory):
        path = os.path.join(directory, name)

        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def magick():
    return shutil.which("magick") or shutil.which("convert")


# ---------------------------------------------------------------------------
# Loading


def open_image(path):
    """Return (kind, handle, width, height, format). `kind` is "pillow" or "magick"."""
    if HAVE_PILLOW:
        try:
            image = Image.open(path)
            image.load()
            format_name = image.format
            image = ImageOps.exif_transpose(image)
            return "pillow", image, image.width, image.height, format_name
        except Exception:
            pass  # a format Pillow lacks: try ImageMagick

    tool = magick()

    if tool is None:
        if HAVE_PILLOW:
            fail("this image format is not supported (is it an image?)")

        fail("no image decoder: run `python3 -m pip install Pillow` or install ImageMagick")

    command = [tool, "identify"] if os.path.basename(tool) == "magick" else ["identify"]
    result = subprocess.run(
        command + ["-format", "%w %h %m", path + "[0]"], capture_output=True, text=True, timeout=30
    )

    try:
        fields = result.stdout.split()
        width, height = int(fields[0]), int(fields[1])
        format_name = fields[2]
    except (ValueError, IndexError):
        fail("this image format is not supported (is it an image?)")

    return "magick", path, width, height, format_name


def resized_rgba(kind, handle, width, height):
    """Return the image scaled to exactly width by height, as RGBA bytes."""
    if kind == "pillow":
        image = handle

        if getattr(image, "n_frames", 1) > 1:
            image.seek(0)

        if image.mode == "P" or image.mode == "LA" or image.mode == "PA":
            image = image.convert("RGBA")

        image = image.convert("RGBA")
        resample = Image.Resampling.LANCZOS
        scaled = image.resize((width, height), resample)
        return scaled.tobytes()

    tool = magick()
    command = [tool] if os.path.basename(tool) == "magick" else ["convert"]
    result = subprocess.run(
        command
        + [handle + "[0]", "-auto-orient", "-resize", "%dx%d!" % (width, height), "-depth", "8", "rgba:-"],
        capture_output=True,
        timeout=60,
    )

    if result.returncode != 0 or len(result.stdout) != width * height * 4:
        fail("ImageMagick could not read this image")

    return result.stdout


# ---------------------------------------------------------------------------
# Cells


def pack(red, green, blue):
    return (red << 16) | (green << 8) | blue


def quantise(pixels, width, height, size):
    """Return `size` representative colours for the image (median cut)."""
    if not HAVE_PILLOW or size <= 0:
        return None

    flat = Image.frombytes("RGBA", (width, height), pixels).convert("RGB")
    reduced = flat.quantize(colors=size, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    table = reduced.getpalette()[: size * 3]

    return [tuple(table[i : i + 3]) for i in range(0, len(table) - 2, 3)]


def build_cells(pixels, columns, rows, background, palette_size, marker=None):
    """Pack the grid. `pixels` is RGBA, 2 * columns wide and 2 * rows tall.

    With a `marker` (three bytes), the first cells of each row carry it, drawn
    in the colour of the cell so that it does not show, and see-through cells
    are a blank braille glyph and not a space: the terminal must be told to
    clear them.
    """
    width = columns * 2
    palette = quantise(pixels, width, rows * 2, palette_size)
    snapped = {}

    def snap(colour):
        if palette is None:
            return colour

        found = snapped.get(colour)

        if found is None:
            red, green, blue = colour
            found = min(
                palette,
                key=lambda entry: (entry[0] - red) ** 2 + (entry[1] - green) ** 2 + (entry[2] - blue) ** 2,
            )
            snapped[colour] = found

        return found

    words = array("I")

    for row in range(rows):
        for column in range(columns):
            block = []

            for dy in (0, 1):
                for dx in (0, 1):
                    at = ((row * 2 + dy) * width + column * 2 + dx) * 4
                    red, green, blue, alpha = pixels[at : at + 4]

                    if alpha < 24:
                        block.append(None)  # see-through: the terminal's own background
                    else:
                        if alpha < 255:
                            mix = alpha / 255
                            red = round(red * mix + background[0] * (1 - mix))
                            green = round(green * mix + background[1] * (1 - mix))
                            blue = round(blue * mix + background[2] * (1 - mix))

                        block.append((red, green, blue))

            glyph, fg, bg = best_glyph(block)
            fg = DEFAULT_COLOUR if fg is None else pack(*snap(fg))
            bg = DEFAULT_COLOUR if bg is None else pack(*snap(bg))
            code = ord(glyph)

            if marker is not None:
                if column < MARKER_CELLS:
                    code = marker_glyphs(marker, row)[column]
                    colour = bg if bg != DEFAULT_COLOUR else (fg if fg != DEFAULT_COLOUR else 0)
                    fg = bg = colour
                elif code == 0x20 and fg == DEFAULT_COLOUR and bg == DEFAULT_COLOUR:
                    code = BLANK

            words.extend((code, fg, bg))

    if sys.byteorder == "big":
        words.byteswap()

    return base64.b64encode(words.tobytes()).decode("ascii")


def mean(colours):
    count = len(colours)

    return (
        round(sum(c[0] for c in colours) / count),
        round(sum(c[1] for c in colours) / count),
        round(sum(c[2] for c in colours) / count),
    )


def error(colours, centre):
    return sum((c[0] - centre[0]) ** 2 + (c[1] - centre[1]) ** 2 + (c[2] - centre[2]) ** 2 for c in colours)


def best_glyph(block):
    """Pick the glyph and colours that draw four sub-pixels with least error.

    A sub-pixel that is None is see-through. A group of sub-pixels that are all
    see-through keeps the terminal's own colour, which costs nothing; a see-through
    sub-pixel in a group that has a colour would be painted, which costs a lot.
    """
    if all(pixel is None for pixel in block):
        return " ", None, None

    best = None

    # Bit 0 stays clear so that each split is tried once: the glyph for the
    # complement is the same split with the two colours swapped.
    for mask in (0, 2, 4, 6, 8, 10, 12, 14):
        front = [block[i] for i in range(4) if mask >> i & 1]
        back = [block[i] for i in range(4) if not mask >> i & 1]
        cost = 0
        colours = []

        for group in (front, back):
            seen = [c for c in group if c is not None]
            colour = mean(seen) if seen else None
            colours.append(colour)

            if colour is not None:
                cost += error(seen, colour) + SEE_THROUGH_COST * (len(group) - len(seen))

        if best is None or cost < best[0]:
            best = (cost, mask, colours[0], colours[1])

    _, mask, fg, bg = best

    if mask == 0:
        return " ", bg, bg

    return QUADRANTS[mask], fg, bg


# ---------------------------------------------------------------------------
# Commands


def read_stored(path):
    if not os.path.isfile(path):
        fail("the stored image is gone")

    return open_image(path)


def store_image(data):
    """Keep the bytes of one image in the cache and describe it."""
    if not data:
        fail("the image data is empty")

    if len(data) > MAX_BYTES:
        fail("the image is larger than %d MB" % (MAX_BYTES // 1024 // 1024))

    directory = cache_dir()
    name = hashlib.sha256(data).hexdigest()[:24]
    path = os.path.join(directory, name + ".img")

    with open(path, "wb") as handle:
        handle.write(data)

    return describe_stored(path)


def describe_stored(path):
    kind, image, width, height, format_name = open_image(path)
    frames = getattr(image, "n_frames", 1) if kind == "pillow" else 1

    return {
        "ok": True,
        "path": path,
        "format": format_name,
        "width": width,
        "height": height,
        "frames": frames,
        "bytes": os.path.getsize(path),
    }


def command_inspect(arguments):
    prune(cache_dir())
    prune(overlay_dir())

    try:
        data = base64.b64decode(sys.stdin.read().encode("ascii"))
    except (binascii.Error, UnicodeEncodeError):
        fail("the image data is not base64")

    print(json.dumps(store_image(data)))


ESC_BYTE = b"\x1b"
TMUX = re.compile(rb"\x1bPtmux;((?:[^\x1b]|\x1b\x1b)*)\x1b\\")
SEQUENCE = re.compile(rb"\x1b\]1337;(File|MultipartFile|FilePart|FileEnd)(?:=([^\x07\x1b]*))?(?:\x07|\x1b\\)")
MAX_SCANNED = 512 * 1024 * 1024
MAX_IMAGES = 16


def command_scan(arguments):
    """The images of a saved output. The mod's own parser reads the arguments."""
    prune(cache_dir())

    if not os.path.isfile(arguments.path) or os.path.getsize(arguments.path) > MAX_SCANNED:
        fail("the saved output is missing or too large")

    with open(arguments.path, "rb") as handle:
        text = handle.read()

    text = TMUX.sub(lambda match: match.group(1).replace(b"\x1b\x1b", ESC_BYTE), text)
    found = []
    pending = None

    def finish(args, payload):
        payload = b"".join(payload.split())

        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return

        if len(found) < MAX_IMAGES and data:
            try:
                stored = store_image(data)
            except SystemExit:
                return  # one picture that will not decode must not hide the others

            stored["args"] = args
            found.append(stored)

    for match in SEQUENCE.finditer(text):
        verb, body = match.group(1), (match.group(2) or b"")

        if verb == b"File":
            pending = None
            colon = body.find(b":")

            if colon >= 0:
                finish(body[:colon].decode("ascii", "replace"), body[colon + 1 :])
        elif verb == b"MultipartFile":
            pending = (body.decode("ascii", "replace"), [])
        elif verb == b"FilePart" and pending is not None:
            pending[1].append(body)
        elif verb == b"FileEnd" and pending is not None:
            finish(pending[0], b"".join(pending[1]))
            pending = None

    print(json.dumps({"ok": True, "images": found}))


def tty_of_ancestors():
    """The first terminal device that this process or one of its parents has."""
    pid = os.getpid()

    for _ in range(12):
        result = subprocess.run(
            ["ps", "-o", "ppid=,tty=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
        fields = result.stdout.split()

        if len(fields) < 2:
            return None

        parent, tty = fields[0], fields[1]

        if tty not in ("??", "?", "-"):
            return tty if tty.startswith("/dev/") else "/dev/" + tty

        pid = int(parent)

        if pid <= 1:
            return None

    return None


def command_cell(arguments):
    """The size of one cell, where the terminal reports its size in pixels."""
    reply = {"ok": True, "width": None, "height": None}
    tty = tty_of_ancestors()

    if tty is not None:
        try:
            descriptor = os.open(tty, os.O_RDONLY | os.O_NOCTTY)

            try:
                rows, columns, x_pixels, y_pixels = struct.unpack(
                    "HHHH", fcntl.ioctl(descriptor, termios.TIOCGWINSZ, b"\0" * 8)
                )
            finally:
                os.close(descriptor)

            if rows and columns and x_pixels and y_pixels:
                reply["width"] = x_pixels / columns
                reply["height"] = y_pixels / rows
        except OSError:
            pass  # no such terminal, or not ours to read

    print(json.dumps(reply))


def parse_background(text):
    text = text.lstrip("#")

    if len(text) != 6:
        return (32, 32, 32)

    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def place(kind, image, width, height, grid_width, grid_height, sub_width, sub_height, stretch):
    """Scale the image to sit inside a grid of sub-pixels and return RGBA bytes.

    A sub-pixel is `sub_width` by `sub_height` device pixels, so the picture is
    scaled by its true shape and centred, with see-through margins: no terminal
    stretches it, whatever the font. `stretch` fills the grid instead.
    """
    if stretch:
        return resized_rgba(kind, image, grid_width, grid_height)

    box_width, box_height = grid_width * sub_width, grid_height * sub_height
    scale = min(box_width / width, box_height / height)
    fitted_width = max(1, min(grid_width, round(width * scale / sub_width)))
    fitted_height = max(1, min(grid_height, round(height * scale / sub_height)))
    pixels = resized_rgba(kind, image, fitted_width, fitted_height)

    if (fitted_width, fitted_height) == (grid_width, grid_height):
        return pixels

    canvas = bytearray(grid_width * grid_height * 4)
    left = (grid_width - fitted_width) // 2
    top = (grid_height - fitted_height) // 2

    for row in range(fitted_height):
        start = ((top + row) * grid_width + left) * 4
        canvas[start : start + fitted_width * 4] = pixels[row * fitted_width * 4 : (row + 1) * fitted_width * 4]

    return bytes(canvas)


def command_cells(arguments):
    kind, image, width, height, _ = read_stored(arguments.path)
    grid_width, grid_height = arguments.columns * 2, arguments.rows * 2
    pixels = place(
        kind, image, width, height, grid_width, grid_height,
        arguments.cell_width / 2, arguments.cell_height / 2, arguments.stretch,
    )
    marker = None
    png = None

    if arguments.marker and arguments.columns >= MARKER_CELLS:
        png, _, _ = write_box_png(kind, image, width, height, arguments)
        marker = overlay_marker(png, arguments.columns, arguments.rows)

    background = parse_background(arguments.background)
    cells = build_cells(pixels, arguments.columns, arguments.rows, background, arguments.palette, marker)
    reply = {"ok": True, "columns": arguments.columns, "rows": arguments.rows, "cells": cells}

    if marker is not None:
        register_overlay(marker, png, arguments.columns, arguments.rows, cells)
        reply["marker"] = marker.hex()

    print(json.dumps(reply))


def overlay_dir():
    path = os.path.join(cache_dir(), "overlays")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def overlay_marker(png, columns, rows):
    """The three bytes that name one box: its picture and its size."""
    return hashlib.sha256(("%s:%d:%d" % (png, columns, rows)).encode()).digest()[:3]


def register_overlay(marker, png, columns, rows, cells):
    """Record what iterm_overlay.py draws for a marker, and the glyphs it covers."""
    words = array("I", base64.b64decode(cells))

    if sys.byteorder == "big":
        words.byteswap()

    glyphs = "".join(chr(words[index]) for index in range(0, len(words), 3))
    record = {"png": png, "columns": columns, "rows": rows, "glyphs": glyphs}

    with open(os.path.join(overlay_dir(), marker.hex() + ".json"), "w") as handle:
        json.dump(record, handle)


def write_box_png(kind, image, width, height, arguments):
    """A PNG with the exact shape of the box, the picture centred in it."""
    # The box in device pixels, at twice the cell size for a sharp picture on a dense screen.
    box_width = arguments.columns * arguments.cell_width * 2
    box_height = arguments.rows * arguments.cell_height * 2
    fit = min(box_width / width, box_height / height)
    # Never enlarge: a small picture keeps its pixels and the margin grows instead.
    out_width = max(1, round(box_width * min(1, fit) / fit))
    out_height = max(1, round(box_height * min(1, fit) / fit))

    if arguments.stretch:
        out_width, out_height = min(width, round(box_width)), min(height, round(box_height))
        pixels = resized_rgba(kind, image, out_width, out_height)
    else:
        pixels = place(kind, image, width, height, out_width, out_height, 1, 1, False)

    out_path = "%s.%dx%d.png" % (os.path.splitext(arguments.path)[0], arguments.columns, arguments.rows)

    if HAVE_PILLOW:
        Image.frombytes("RGBA", (out_width, out_height), pixels).save(out_path, "PNG")
    else:
        write_png(out_path, out_width, out_height, pixels)

    return out_path, out_width, out_height


def command_png(arguments):
    kind, image, width, height, _ = read_stored(arguments.path)
    out_path, out_width, out_height = write_box_png(kind, image, width, height, arguments)

    print(json.dumps({"ok": True, "path": out_path, "width": out_width, "height": out_height}))


def write_png(path, width, height, rgba):
    """A minimal PNG writer, for a machine with ImageMagick and no Pillow."""
    import zlib

    def chunk(tag, body):
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + rgba[y * width * 4 : (y + 1) * width * 4] for y in range(height))

    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)))
        handle.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        handle.write(chunk(b"IEND", b""))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("inspect").set_defaults(run=command_inspect)

    commands.add_parser("cell").set_defaults(run=command_cell)

    scan = commands.add_parser("scan")
    scan.add_argument("path")
    scan.set_defaults(run=command_scan)

    cells = commands.add_parser("cells")
    cells.add_argument("path")
    cells.add_argument("--columns", type=int, required=True)
    cells.add_argument("--rows", type=int, required=True)
    cells.add_argument("--cell-width", type=float, default=8, help="a cell's width in pixels")
    cells.add_argument("--cell-height", type=float, default=16, help="a cell's height in pixels")
    cells.add_argument("--stretch", action="store_true", help="fill the box instead of keeping the shape")
    cells.add_argument("--background", default="202020")
    cells.add_argument("--palette", type=int, default=0, help="reduce to this many colours (0 keeps them all)")
    cells.add_argument("--marker", action="store_true", help="hide an id for iterm_overlay.py in the first cells")
    cells.set_defaults(run=command_cells)

    png = commands.add_parser("png")
    png.add_argument("path")
    png.add_argument("--columns", type=int, required=True)
    png.add_argument("--rows", type=int, required=True)
    png.add_argument("--cell-width", type=float, default=8)
    png.add_argument("--cell-height", type=float, default=16)
    png.add_argument("--stretch", action="store_true")
    png.set_defaults(run=command_png)

    arguments = parser.parse_args()

    try:
        arguments.run(arguments)
    except SystemExit:
        raise
    except Exception as problem:  # one clear line beats a traceback in a terminal row
        fail("%s: %s" % (type(problem).__name__, problem))


if __name__ == "__main__":
    main()
