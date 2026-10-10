#!/usr/bin/env python3
"""Decode an image and turn it into something a terminal can draw.

The mod calls this helper because the mod itself runs in a sandbox with no
image decoder. Each command prints one line of JSON on stdout:

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

Only PNG, JPEG, GIF, WebP, BMP, TIFF and ICO are decoded. The type is read
from the first bytes of the file before any decoder sees it, so a PostScript,
PDF or SVG file never reaches Ghostscript or a delegate of ImageMagick.

The cache is a folder only this user can use: ~/Library/Caches/inline-images
on macOS, $XDG_CACHE_HOME/inline-images or ~/.cache/inline-images elsewhere.
The environment variable INLINE_IMAGES_CACHE names another folder (the tests
use it). bin/iterm_overlay.py reads the overlay records from the same folder.
"""

import argparse
import base64
import binascii
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata
import warnings
from array import array

# A picture larger than this is refused before its pixels are decoded: a small
# file can claim a huge size, and decoding it would take all the memory.
MAX_PIXELS = 40_000_000

try:
    from PIL import Image, ImageOps

    # Pillow only warns between its limit and twice that; here a warning is an error.
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    HAVE_PILLOW = True
except ImportError:  # pragma: no cover - depends on the machine
    HAVE_PILLOW = False

MAX_BYTES = 64 * 1024 * 1024
MAX_AGE_SECONDS = 24 * 60 * 60
CACHE_VARIABLE = "INLINE_IMAGES_CACHE"

# Quadrant block glyphs. The index is a bitmask of the sub-pixels that take the
# foreground colour: bit 0 top left, 1 top right, 2 bottom left, 3 bottom right.
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"

DEFAULT_COLOUR = 0x01000000  # bit 24 alone: the terminal's own colour
SEE_THROUGH_COST = 3 * 255 * 255
SEE_THROUGH_ALPHA = 24  # a sub-pixel less opaque than this is not painted at all

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


# ---------------------------------------------------------------------------
# The cache


class UnsafeCache(Exception):
    """The cache folder exists but another user could read or change it."""


def cache_location():
    """The cache folder's path. iterm_overlay.py calls this too, so both agree.

    A folder in the user's home is preferred to one in the shared temporary
    folder, where another user could make the folder first.
    """
    override = os.environ.get(CACHE_VARIABLE)

    if override:
        return os.path.abspath(override)

    home = os.path.expanduser("~")

    if os.path.isabs(home) and os.path.isdir(home) and os.access(home, os.W_OK | os.X_OK):
        if sys.platform == "darwin":
            return os.path.join(home, "Library", "Caches", "inline-images")

        base = os.environ.get("XDG_CACHE_HOME", "")

        if not os.path.isabs(base):  # the specification says to ignore a relative path
            base = os.path.join(home, ".cache")

        return os.path.join(base, "inline-images")

    return os.path.join(tempfile.gettempdir(), "inline-images-%d" % os.getuid())


def secure_folder(path):
    """Make `path` a folder that only this user can use, or raise UnsafeCache.

    A link could send the files somewhere else, and a folder that another user
    owns or can write could hold files planted to be drawn or opened.
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)

    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeCache("the cache folder %s is a link or not a folder" % path)

    if info.st_uid != os.getuid():
        raise UnsafeCache("the cache folder %s belongs to another user" % path)

    if info.st_mode & 0o077:
        os.chmod(path, 0o700)  # ours, but made with a loose umask: tighten it
        info = os.lstat(path)

        if info.st_mode & 0o077:
            raise UnsafeCache("other users can use the cache folder %s" % path)

    return path


def cache_dir():
    return secure_folder(cache_location())


def write_atomic(path, data):
    """Write a file in the cache so that no reader ever sees half of it.

    The bytes go to a new file in the same folder, which then takes the name in
    one step: two processes that store the same picture never trip each other.
    """
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".part")

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)

        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        raise


def prune(directory):
    """Remove cached files nobody has touched for a day, and folders left empty.

    lstat, so a link is judged by its own age and what it points to is never touched.
    """
    cutoff = time.time() - MAX_AGE_SECONDS

    for folder, _, names in os.walk(directory, topdown=False, followlinks=False):
        for name in names:
            path = os.path.join(folder, name)

            try:
                if os.lstat(path).st_mtime < cutoff:
                    os.remove(path)
            except OSError:
                pass  # gone already: another process pruned it

        if folder != directory:
            # Old folders only: another process may have just made this one, to
            # put a link in it. rmdir only succeeds when the folder is empty.
            try:
                if os.lstat(folder).st_mtime < cutoff:
                    os.rmdir(folder)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Loading


# The formats this helper decodes: the name Pillow reports, the Pillow plugins
# that may open it, ImageMagick's coder for it, and its file extension.
FORMATS = {
    "PNG": (["PNG"], "png", ".png"),
    "JPEG": (["JPEG", "MPO"], "jpeg", ".jpg"),
    "GIF": (["GIF"], "gif", ".gif"),
    "WEBP": (["WEBP"], "webp", ".webp"),
    "BMP": (["BMP"], "bmp", ".bmp"),
    "TIFF": (["TIFF"], "tiff", ".tiff"),
    "ICO": (["ICO"], "ico", ".ico"),
}

EXTENSIONS = {name: extension for name, (_, _, extension) in FORMATS.items()}
EXTENSIONS["MPO"] = ".jpg"  # a camera's JPEG with a second picture in it

UNSUPPORTED = "not a supported image format (PNG, JPEG, GIF, WebP, BMP, TIFF or ICO)"


def sniff(head):
    """The format of an image from its first bytes, or None when it is not one we decode."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"

    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"

    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"

    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"

    if head[:2] == b"BM":
        return "BMP"

    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):  # TIFF and BigTIFF
        return "TIFF"

    if head[:4] == b"\x00\x00\x01\x00":
        return "ICO"

    return None


def sniff_file(path):
    with open(path, "rb") as handle:
        return sniff(handle.read(16))


def check_size(width, height):
    if width <= 0 or height <= 0:
        fail("this image has no pixels")

    if width * height > MAX_PIXELS:
        fail("the image is too large: %d x %d pixels (at most %d million)" % (width, height, MAX_PIXELS // 1_000_000))


def magick():
    return shutil.which("magick") or shutil.which("convert")


# ImageMagick's own limits, so that a file it does read cannot take the machine.
MAGICK_LIMITS = [
    "-limit", "memory", "256MiB",
    "-limit", "map", "512MiB",
    "-limit", "area", "64MP",
    "-limit", "width", "16KP",
    "-limit", "height", "16KP",
]

# EXIF orientations that turn the picture a quarter: its width and height swap.
QUARTER_TURNS = {"LeftTop", "RightTop", "RightBottom", "LeftBottom"}


def magick_source(path, label):
    """The input argument for ImageMagick: the coder named, so it never guesses one.

    Left to guess, ImageMagick hands PostScript to Ghostscript and reads MSL,
    SVG and URLs; with `png:` in front, the file is read as a PNG or not at all.
    """
    return "%s:%s[0]" % (FORMATS[label][1], path)


def open_image(path):
    """Read an image's header: (kind, handle, width, height, format, frames).

    `kind` is "pillow" or "magick". No pixels are decoded: `decode` does that,
    once the size is known to be safe.
    """
    try:
        label = sniff_file(path)
    except OSError as problem:
        fail("cannot read the image: %s" % problem.strerror)

    if label is None:
        fail(UNSUPPORTED)

    if HAVE_PILLOW:
        image = None

        try:
            image = Image.open(path, formats=FORMATS[label][0])
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            fail("the image is too large (more than %d million pixels)" % (MAX_PIXELS // 1_000_000))
        except Exception:
            pass  # a plugin Pillow was built without (WebP, say): try ImageMagick

        if image is not None:
            # Before exif_transpose, which returns a copy with only one frame.
            try:
                frames = getattr(image, "n_frames", 1)
            except Exception:
                frames = 1  # a damaged animation: its first frame may still show

            width, height = image.size

            try:
                if image.getexif().get(0x0112) in (5, 6, 7, 8):
                    width, height = height, width
            except Exception:
                pass  # unreadable EXIF: draw it as it is stored

            check_size(width, height)
            return "pillow", image, width, height, image.format, frames

    tool = magick()

    if tool is None:
        if HAVE_PILLOW:
            fail("this image cannot be decoded (is it damaged?)")

        fail("no image decoder: run `python3 -m pip install Pillow` or install ImageMagick")

    command = [tool, "identify"] if os.path.basename(tool) == "magick" else ["identify"]
    # -ping reads the header only. -auto-orient does nothing without pixels, so a
    # quarter turn is read from the orientation and the size swapped here, to
    # match what `convert -auto-orient` produces.
    source = magick_source(path, label)[: -len("[0]")]  # every frame, to count them

    try:
        result = subprocess.run(
            command + MAGICK_LIMITS + ["-ping", "-format", "%w %h %[orientation]\n", source],
            capture_output=True,
            text=True,
            timeout=30,
        )
        lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
        width, height = int(lines[0][0]), int(lines[0][1])
    except (ValueError, IndexError, OSError, subprocess.TimeoutExpired):
        fail("this image cannot be decoded (is it damaged?)")

    if len(lines[0]) > 2 and lines[0][2] in QUARTER_TURNS:
        width, height = height, width

    check_size(width, height)
    return "magick", path, width, height, label, len(lines)


def to_eight_bit(image):
    """Grey of 16 or 32 bits, or of floats, as 8-bit grey.

    convert("L") clips such values at 255, so mid grey would come out white.
    """
    if image.mode.startswith("I;16"):
        image, top = image.convert("I"), 65535
    else:
        _, high = image.getextrema()

        if image.mode == "F" and high <= 1:
            top = 1  # floats from 0 to 1
        elif high <= 255:
            top = 255
        elif high <= 65535:
            top = 65535  # 16-bit samples in a 32-bit image
        else:
            top = high

    return image.point(lambda value: value * (255 / top)).convert("L")


def decode(kind, handle, width, height):
    """The first frame, upright and in RGBA, decoded once for all the sizes a command needs.

    `width` by `height` is the largest size it will be scaled to. A JPEG much
    larger than that is decoded at a half, a quarter or an eighth of its size,
    which is far quicker and needs far less memory.
    """
    if kind != "pillow":
        return handle  # ImageMagick reads the file for each size

    image = handle

    if getattr(image, "n_frames", 1) > 1:
        image.seek(0)

    if image.format in ("JPEG", "MPO"):
        side = max(1, round(max(width, height)))  # a square: the EXIF turn is not applied yet
        image.draft(image.mode, (side, side))

    image.load()
    image = ImageOps.exif_transpose(image)

    if image.mode in ("I", "F") or image.mode.startswith("I;16"):
        image = to_eight_bit(image)

    return image.convert("RGBA")


def resized_rgba(kind, handle, width, height):
    """Return the image scaled to exactly width by height, as RGBA bytes.

    For Pillow, `handle` is what `decode` returned.
    """
    if kind == "pillow":
        return handle.resize((width, height), Image.Resampling.LANCZOS).tobytes()

    path, label = handle, sniff_file(handle)

    if label is None:
        fail(UNSUPPORTED)

    tool = magick()
    command = [tool] if os.path.basename(tool) == "magick" else ["convert"]
    result = subprocess.run(
        command
        + MAGICK_LIMITS
        + [magick_source(path, label), "-auto-orient", "-resize", "%dx%d!" % (width, height), "-depth", "8", "rgba:-"],
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


def blend(red, green, blue, alpha, background):
    """A part-transparent colour over the background, as the terminal would show it."""
    mix = alpha / 255

    return (
        round(red * mix + background[0] * (1 - mix)),
        round(green * mix + background[1] * (1 - mix)),
        round(blue * mix + background[2] * (1 - mix)),
    )


def quantise(pixels, width, height, size, background=(32, 32, 32)):
    """Return `size` representative colours for the image (median cut).

    Only the colours that are painted count: see-through pixels keep the
    terminal's own background, and would only waste entries of the palette.
    """
    if not HAVE_PILLOW or size <= 0:
        return None

    painted = bytearray()

    for at in range(0, width * height * 4, 4):
        red, green, blue, alpha = pixels[at : at + 4]

        if alpha >= SEE_THROUGH_ALPHA:
            painted.extend((red, green, blue) if alpha == 255 else blend(red, green, blue, alpha, background))

    if not painted:
        return None

    flat = Image.frombytes("RGB", (len(painted) // 3, 1), bytes(painted))
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
    palette = quantise(pixels, width, rows * 2, palette_size, background)
    snapped = {}
    glyphs = {}  # most pictures repeat blocks: each is worked out once

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

    def colour_at(at):
        red, green, blue, alpha = pixels[at : at + 4]

        if alpha < SEE_THROUGH_ALPHA:
            return None  # see-through: the terminal's own background

        if alpha < 255:
            return blend(red, green, blue, alpha, background)

        return (red, green, blue)

    words = array("I")
    stride = width * 4

    for row in range(rows):
        top = row * 2 * stride

        for column in range(columns):
            at = top + column * 8
            block = (colour_at(at), colour_at(at + 4), colour_at(at + stride), colour_at(at + stride + 4))

            if block[0] == block[1] == block[2] == block[3]:
                # One colour, or none: what best_glyph picks for it, without the search.
                glyph, fg, bg = " ", block[0], block[0]
            else:
                found = glyphs.get(block)

                if found is None:
                    found = glyphs[block] = best_glyph(block)

                glyph, fg, bg = found

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

    try:
        os.utime(path)  # in use: a prune must keep it
    except OSError:
        pass  # not ours to touch: it is still read

    return open_image(path)


def extension_for(format_name, label):
    """The file extension for a format, so that a click opens the system's viewer."""
    found = EXTENSIONS.get(str(format_name).upper())

    if found is None and HAVE_PILLOW:
        found = next(
            (extension for extension, name in Image.registered_extensions().items() if name == str(format_name).upper()),
            None,
        )

    return found or EXTENSIONS.get(label) or ".img"


def store_image(data):
    """Keep the bytes of one image in the cache and describe it.

    The file gets the extension of its format, so that a click can open it in
    the system's own viewer.
    """
    if not data:
        fail("the image data is empty")

    if len(data) > MAX_BYTES:
        fail("the image is larger than %d MB" % (MAX_BYTES // 1024 // 1024))

    label = sniff(data[:16])

    if label is None:
        fail(UNSUPPORTED)  # before anything is written or decoded

    directory = cache_dir()
    name = hashlib.sha256(data).hexdigest()[:24]

    # The cache is keyed by content: an image stored before is used as it is.
    for extension in sorted(set(EXTENSIONS.values()) | {".img"}):
        known = os.path.join(directory, name + extension)

        try:
            if os.path.isfile(known) and os.path.getsize(known) == len(data):
                os.utime(known)  # touched, so a prune keeps it
                return describe_stored(known)
        except OSError:
            pass  # pruned by another process just now: store it again

    # A file of its own, under a name no other process uses, until it is known
    # to be an image: then it takes its final name in one step.
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix="." + name + ".", suffix=".part")

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)

        described = describe_stored(temporary)
        path = os.path.join(directory, name + extension_for(described["format"], label))
        os.replace(temporary, path)
        described["path"] = path
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)  # still there only when it was not an image

    return described


def describe_stored(path):
    """What `inspect` reports. Only the header is read: no pixels are decoded."""
    kind, image, width, height, format_name, frames = open_image(path)

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

    if arguments.file:
        # A file on disk, delivered as it is: read it, no base64 on the way.
        try:
            if os.path.getsize(arguments.file) > MAX_BYTES:
                fail("the image is larger than %d MB" % (MAX_BYTES // 1024 // 1024))

            with open(arguments.file, "rb") as handle:
                data = handle.read()
        except OSError as problem:
            fail("cannot read %s: %s" % (arguments.file, problem.strerror))
    else:
        try:
            data = base64.b64decode(sys.stdin.read().encode("ascii"))
        except (binascii.Error, UnicodeEncodeError):
            fail("the image data is not base64")

    described = store_image(data)

    if arguments.name:
        named = named_copy(described["path"], arguments.name)

        if named is not None:
            described["named"] = named

    print(json.dumps(described))


# Characters that must not reach a file name a viewer shows in its title bar:
# controls, and format characters, which include the bidi overrides that make
# "photo‮gnp.exe" read as "photoexe.png".
HIDDEN_CATEGORIES = ("Cc", "Cf")
MAX_NAME_BYTES = 200  # file systems allow 255 bytes, and the extension may follow


def clean_name(name):
    """A file name from the name its sender gave, safe to show and to create."""
    base = os.path.basename(name.replace("\\", "/"))
    base = "".join(character for character in base if unicodedata.category(character) not in HIDDEN_CATEGORIES)
    base = "".join(character if character.isprintable() and character not in "/:" else "_" for character in base)
    base = base.strip()
    # Bytes, not characters: 200 characters of most scripts are far more than 255 bytes.
    base = base.encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore").strip()

    return base if base not in ("", ".", "..") else "image"


def named_copy(path, name):
    """A link to a stored image under the name its sender gave, for the viewer's title bar.

    None when it cannot be made: the picture still shows, only its title is lost.
    """
    base = clean_name(name)
    extension = os.path.splitext(path)[1]

    if extension and not base.lower().endswith(extension) and not (extension == ".jpg" and base.lower().endswith(".jpeg")):
        base += extension

    try:
        named = os.path.join(cache_dir(), "named")
        os.makedirs(named, mode=0o700, exist_ok=True)
        folder = os.path.join(named, os.path.splitext(os.path.basename(path))[0])
        os.makedirs(folder, mode=0o700, exist_ok=True)
        target = os.path.join(folder, base)

        if os.path.exists(target) and os.path.samefile(target, path):
            return target

        # A link under a name of its own, which then takes the name in one step:
        # another process may be making the same link at the same time.
        temporary = os.path.join(folder, ".%s.part" % secrets.token_hex(8))

        try:
            os.link(path, temporary)
            os.replace(temporary, target)
        except OSError:
            with open(path, "rb") as handle:
                write_atomic(target, handle.read())
        finally:
            # A rename onto another link of the same file does nothing and
            # leaves the temporary name behind: it goes here.
            with contextlib.suppress(OSError):
                os.remove(temporary)

        return target
    except (OSError, UnsafeCache):
        return None


ESC_BYTE = b"\x1b"
TMUX_START = b"\x1bPtmux;"
# A tmux DCS passthrough: ESC P tmux ; <body with every ESC doubled> ESC \.
# Unrolled ("normal* (special normal*)*"), so a long body with no end costs
# linear time; the form (?:[^ESC]|ESC ESC)* backtracks one byte at a time.
TMUX = re.compile(rb"\x1bPtmux;([^\x1b]*(?:\x1b\x1b[^\x1b]*)*)\x1b\\")
# The TypeScript parser (hooks/osc1337.ts) also takes the 8-bit ST, U+009C. It
# reads decoded text; this reads bytes, and in UTF-8 the byte 0x9c is part of
# many ordinary characters (a continuation byte), so it cannot end a sequence here.
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

    if TMUX_START in text:
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
                # Quietly: a picture that will not decode prints no reply of its own.
                with contextlib.redirect_stdout(io.StringIO()):
                    stored = store_image(data)
            except SystemExit:
                return  # one picture that will not decode must not hide the others

            stored["args"] = args
            name = dict(pair.split("=", 1) for pair in args.split(";") if "=" in pair).get("name")

            if name:
                try:
                    named = named_copy(stored["path"], base64.b64decode(name).decode("utf-8", "replace"))
                except (binascii.Error, ValueError):
                    named = None  # the picture still shows; only its title is lost

                if named is not None:
                    stored["named"] = named

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


MAX_ANCESTORS = 12
CELL_SECONDS = 5  # for the whole walk: a slow `ps` must not hold up a preview


def tty_of_ancestors():
    """The first terminal device that this process or one of its parents has."""
    pid = os.getpid()
    deadline = time.monotonic() + CELL_SECONDS

    for _ in range(MAX_ANCESTORS):
        left = deadline - time.monotonic()

        if left <= 0:
            return None

        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,tty=", "-p", str(pid)], capture_output=True, text=True, timeout=min(5, left)
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        fields = result.stdout.split()

        if len(fields) < 2:
            return None

        parent, tty = fields[0], fields[1]

        if tty not in ("??", "?", "-"):
            return tty if tty.startswith("/dev/") else "/dev/" + tty

        try:
            pid = int(parent)
        except ValueError:
            return None

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

    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (32, 32, 32)


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


def box_pixels(arguments):
    """The box in device pixels, at twice the cell size for a sharp picture on a dense screen."""
    return arguments.columns * arguments.cell_width * 2, arguments.rows * arguments.cell_height * 2


def check_box(arguments):
    if arguments.columns < 1 or arguments.rows < 1 or arguments.cell_width <= 0 or arguments.cell_height <= 0:
        fail("the box must be at least one cell, and a cell more than zero pixels")


def command_cells(arguments):
    check_box(arguments)
    kind, image, width, height, _, _ = read_stored(arguments.path)
    grid_width, grid_height = arguments.columns * 2, arguments.rows * 2
    with_marker = arguments.marker and arguments.columns >= MARKER_CELLS
    largest = box_pixels(arguments) if with_marker else (grid_width, grid_height)
    image = decode(kind, image, *largest)
    pixels = place(
        kind, image, width, height, grid_width, grid_height,
        arguments.cell_width / 2, arguments.cell_height / 2, arguments.stretch,
    )
    marker = None
    png = None

    if with_marker:
        png, _, _ = write_box_png(kind, image, width, height, arguments)
        marker = overlay_marker(png, arguments)

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


def overlay_marker(png, arguments):
    """The three bytes that name one box: its picture, its size and how it was drawn.

    The cell size, stretch, palette and background change the PNG or the glyphs,
    so a box drawn after a change of font size never reuses an old record.
    """
    key = "%s:%d:%d:%r:%r:%d:%d:%s" % (
        png,
        arguments.columns,
        arguments.rows,
        float(arguments.cell_width),
        float(arguments.cell_height),
        bool(arguments.stretch),
        getattr(arguments, "palette", 0),
        getattr(arguments, "background", ""),
    )

    return hashlib.sha256(key.encode()).digest()[:3]


def register_overlay(marker, png, columns, rows, cells):
    """Record what iterm_overlay.py draws for a marker, and the glyphs it covers."""
    words = array("I", base64.b64decode(cells))

    if sys.byteorder == "big":
        words.byteswap()

    glyphs = "".join(chr(words[index]) for index in range(0, len(words), 3))
    record = {"png": png, "columns": columns, "rows": rows, "glyphs": glyphs}
    write_atomic(os.path.join(overlay_dir(), marker.hex() + ".json"), json.dumps(record).encode())


def nearest(pixels, width, height, out_width, out_height):
    """RGBA bytes scaled by repeating pixels: no new colours, no blur."""
    if HAVE_PILLOW:
        picture = Image.frombytes("RGBA", (width, height), pixels)
        return picture.resize((out_width, out_height), Image.Resampling.NEAREST).tobytes()

    # One 32-bit word per pixel, so a row is copied a word at a time.
    words = array("I")
    words.frombytes(pixels)
    # The source pixel under the centre of each new one, as Pillow picks it.
    columns = [(2 * x + 1) * width // (2 * out_width) for x in range(out_width)]
    out = array("I")

    for y in range(out_height):
        source = (2 * y + 1) * height // (2 * out_height) * width
        out.extend(array("I", [words[source + x] for x in columns]))

    return out.tobytes()


def write_box_png(kind, image, width, height, arguments):
    """A PNG with the exact shape of the box, the picture centred in it.

    For Pillow, `image` is what `decode` returned.
    """
    box_width, box_height = box_pixels(arguments)
    fit = min(box_width / width, box_height / height)
    # Never enlarge: a small picture keeps its pixels and the margin grows instead.
    out_width = max(1, round(box_width * min(1, fit) / fit))
    out_height = max(1, round(box_height * min(1, fit) / fit))

    if arguments.stretch:
        out_width, out_height = max(1, min(width, round(box_width))), max(1, min(height, round(box_height)))
        pixels = resized_rgba(kind, image, out_width, out_height)
    else:
        pixels = place(kind, image, width, height, out_width, out_height, 1, 1, False)

    # iterm_overlay.py cuts the PNG into rows of cells. A height that is a
    # multiple of the rows gives every row the same whole number of pixels: no
    # empty row for a tiny picture, and no seam of one pixel between rows.
    if out_height % arguments.rows:
        taller = arguments.rows * -(-out_height // arguments.rows)
        wider = max(1, round(out_width * taller / out_height))
        pixels = nearest(pixels, out_width, out_height, wider, taller)
        out_width, out_height = wider, taller

    # The name carries how the box was drawn: a new font size is a new file.
    out_path = "%s.%dx%d.%gx%g%s.png" % (
        os.path.splitext(arguments.path)[0],
        arguments.columns,
        arguments.rows,
        arguments.cell_width,
        arguments.cell_height,
        ".stretch" if arguments.stretch else "",
    )

    if HAVE_PILLOW:
        out = io.BytesIO()
        Image.frombytes("RGBA", (out_width, out_height), pixels).save(out, "PNG")
        write_atomic(out_path, out.getvalue())
    else:
        write_atomic(out_path, png_bytes(out_width, out_height, pixels))

    return out_path, out_width, out_height


def command_png(arguments):
    check_box(arguments)
    kind, image, width, height, _, _ = read_stored(arguments.path)
    image = decode(kind, image, *box_pixels(arguments))
    out_path, out_width, out_height = write_box_png(kind, image, width, height, arguments)

    print(json.dumps({"ok": True, "path": out_path, "width": out_width, "height": out_height}))


def png_bytes(width, height, rgba):
    """A minimal PNG encoder, for a machine with ImageMagick and no Pillow."""
    import zlib

    def chunk(tag, body):
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + rgba[y * width * 4 : (y + 1) * width * 4] for y in range(height))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# Mermaid
#
# A ```mermaid block in a reply is drawn as a picture. mmdc (mermaid-cli)
# renders it in a headless browser; the PNG then goes through the same cache,
# previews and click-to-open as any other image.

MERMAID_MAX_SOURCE = 64 * 1024
MERMAID_THEMES = ("default", "neutral", "dark", "forest")
MERMAID_SCALE = 2  # twice the pixels, for a sharp picture on a dense screen
MERMAID_ERROR_SIZE = (512, 109)  # the picture Mermaid draws for a syntax error, at scale 1
BROWSER_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
BROWSER_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser")


def find_browser():
    """A Chrome-family browser for mmdc, when Puppeteer has none of its own."""
    chosen = os.environ.get("PUPPETEER_EXECUTABLE_PATH")

    if chosen and os.access(chosen, os.X_OK):
        return chosen

    for path in BROWSER_APPS:
        if os.access(path, os.X_OK):
            return path

    for command in BROWSER_COMMANDS:
        found = shutil.which(command)

        if found:
            return found

    return None  # Puppeteer's own download, if there is one


def mermaid_kind(source):
    """The diagram's type: the first word after any front matter and comments."""
    lines = iter(source.splitlines())

    for line in lines:
        text = line.strip()

        if text == "---":  # YAML front matter, up to the next ---
            for inner in lines:
                if inner.strip() == "---":
                    break
            continue

        if text and not text.startswith("%%"):
            return re.sub(r"[^A-Za-z0-9-]", "", text.split()[0]) or "diagram"

    return "diagram"


def mermaid_error(stderr):
    """The useful lines of what mmdc printed about a failure."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", stderr)
    lines = [line.rstrip() for line in plain.splitlines()]

    def readable(text):
        # Stop at a stack frame or a URL: noise, and it can carry a local path.
        return re.split(r"(?:^|\s)(?:at |https?://)", text.strip())[0].strip()

    for index, line in enumerate(lines):
        if line.startswith("Error:"):
            useful = [readable(line[len("Error:"):])]

            for text in lines[index + 1 : index + 4]:
                if not text.strip() or re.match(r"\s*(?:at |https?://)", text):
                    break

                useful.append(readable(text))

            return " ".join(part for part in useful if part)[:300]

    return readable((plain.strip().splitlines() or ["mmdc failed"])[-1])[:300] or "mmdc failed"


def run_mmdc(tool, browser, source_path, out_path, arguments):
    command = [
        tool, "--quiet", "-i", source_path, "-o", out_path,
        "-t", arguments.theme, "-b", arguments.background, "-s", str(MERMAID_SCALE),
    ]

    if browser is not None:
        config = os.path.join(cache_dir(), "puppeteer.json")
        write_atomic(config, json.dumps({"executablePath": browser, "headless": "shell"}).encode())
        command += ["-p", config]

    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        fail("Mermaid took more than 90 seconds to draw this diagram")


def looks_like_error(path):
    """Whether a PNG has the size of Mermaid's 'Syntax error in text' picture."""
    try:
        if HAVE_PILLOW:
            with Image.open(path) as picture:
                width, height = picture.size
        else:
            with open(path, "rb") as handle:
                width, height = struct.unpack(">II", handle.read(24)[16:24])
    except (OSError, struct.error, ValueError):
        return False

    expected = [size * MERMAID_SCALE for size in MERMAID_ERROR_SIZE]

    return abs(width - expected[0]) <= 4 * MERMAID_SCALE and abs(height - expected[1]) <= 4 * MERMAID_SCALE


def command_mermaid(arguments):
    source = sys.stdin.read()

    if not source.strip():
        fail("the diagram is empty")

    if len(source.encode()) > MERMAID_MAX_SOURCE:
        fail("the diagram is longer than %d KB" % (MERMAID_MAX_SOURCE // 1024))

    if arguments.theme not in MERMAID_THEMES:
        fail("unknown Mermaid theme %s" % arguments.theme)

    tool = shutil.which("mmdc")

    if tool is None:
        fail("Mermaid needs mmdc: run `npm install -g @mermaid-js/mermaid-cli`")

    prune(cache_dir())
    folder = secure_folder(os.path.join(cache_dir(), "mermaid"))
    key = hashlib.sha256("\0".join([source, arguments.theme, arguments.background, str(MERMAID_SCALE)]).encode()).hexdigest()[:24]
    rendered = os.path.join(folder, key + ".png")
    kind = mermaid_kind(source)

    if not os.path.isfile(rendered):
        browser = find_browser()

        with tempfile.TemporaryDirectory(dir=folder) as work:
            source_path = os.path.join(work, "diagram.mmd")
            out_path = os.path.join(work, "diagram.png")

            with open(source_path, "w") as handle:
                handle.write(source)

            ran = run_mmdc(tool, browser, source_path, out_path, arguments)

            if ran.returncode != 0 or not os.path.isfile(out_path):
                fail("Mermaid: %s" % mermaid_error(ran.stderr or ran.stdout))

            # Some diagram types draw a 'Syntax error' picture and exit 0. One with
            # that picture's size is checked as SVG, which says so in words.
            if looks_like_error(out_path):
                svg = os.path.join(work, "diagram.svg")
                run_mmdc(tool, browser, source_path, svg, arguments)

                with open(svg, errors="replace") as handle:
                    if "Syntax error in text" in handle.read():
                        fail("Mermaid: syntax error in this %s diagram" % kind)

            os.replace(out_path, rendered)

    os.utime(rendered)

    with open(rendered, "rb") as handle:
        described = store_image(handle.read())

    named = named_copy(described["path"], "%s.png" % kind)

    if named is not None:
        described["named"] = named

    described["kind"] = kind
    print(json.dumps(described))


def command_mermaid_check(arguments):
    """What drawing a diagram would use: mmdc and a browser, or what is missing."""
    print(json.dumps({"ok": True, "mmdc": shutil.which("mmdc"), "browser": find_browser()}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--file", help="read the image from this file, not base64 from stdin")
    inspect.add_argument("--name", help="also link the image under this file name, for a viewer to show")
    inspect.set_defaults(run=command_inspect)

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

    mermaid = commands.add_parser("mermaid", help="draw a Mermaid diagram, its source on stdin")
    mermaid.add_argument("--theme", default="default", help="default, neutral, dark or forest")
    mermaid.add_argument("--background", default="white", help="a CSS colour, or transparent")
    mermaid.set_defaults(run=command_mermaid)

    commands.add_parser("mermaid-check").set_defaults(run=command_mermaid_check)

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
    except UnsafeCache as problem:
        fail(str(problem))
    except Exception as problem:  # one clear line beats a traceback in a terminal row
        fail("%s: %s" % (type(problem).__name__, problem))


if __name__ == "__main__":
    main()
