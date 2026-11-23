#!/usr/bin/env python3
"""Draw real images over the cell previews of inline-images, in WezTerm.

The same daemon as `iterm_overlay.py`, with WezTerm in place of the iTerm2
Python API. The `Watcher` there decides what to draw and when; this file gives
it a WezTerm pane to read and write:

- It reads the screen with `wezterm cli get-text`. WezTerm has no screen
  stream, so it reads a few times a second, faster while boxes are on it.
- It cannot write the image to the pane's tty: those bytes mix with Claude
  Code's own output, and a large image then shows as base64 text. It drops the
  image, as one escape sequence, in a private folder (the inbox), and
  `mod/wezterm/inline_images.lua`, loaded by the WezTerm config, hands it to
  `pane:inject_output`, which WezTerm parses apart from Claude Code's output.
  Without that file in the config the overlay does not run. WezTerm draws the
  iTerm2 protocol, but only a single `File=` sequence: it ignores
  `MultipartFile`.
- `get-text` does not say which cells show an image: WezTerm draws an image
  over the text that is in its cells. So before the image goes down, the cells
  under it are erased. A blank cell where this daemon drew is an image cell;
  when Claude Code paints the cell again it shows its glyph, and the image in
  that cell is gone, as in iTerm2.

    wezterm_overlay.py --pane <WEZTERM_PANE>

It prints one JSON line when it is ready, or one with the reason it cannot run,
and then runs until its parent exits. With INLINE_IMAGES_DEBUG=<file> it writes
what it does to that file.
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

if HERE not in sys.path:
    sys.path.insert(0, HERE)

from iterm_overlay import BRAILLE, LOOK_SECONDS, Overlays, Watcher, debug, emit  # noqa: E402

FAST_SECONDS = 0.15  # between reads while boxes or images are on the screen
SLOW_SECONDS = 0.5  # between reads otherwise
HEARTBEAT_SECONDS = 1.0  # look anyway, though the text has not changed
GEOMETRY_SECONDS = 2.0  # read the pane's size again after this long
MAX_DRAWN = 256  # rectangles remembered as drawn
COMMAND_SECONDS = 5  # a wezterm command that takes longer is stuck
ALIVE_SECONDS = 10  # the WezTerm side wrote its heartbeat this recently: it is there
HELLO_SECONDS = 4  # how long to wait for that heartbeat at the start
INBOX = os.environ.get("INLINE_IMAGES_WEZTERM_INBOX") or os.path.join(
    os.path.expanduser("~"), ".local", "state", "inline-images", "wezterm"
)


def wezterm_binary():
    """The `wezterm` command: on the PATH, or inside the app on macOS."""
    found = shutil.which("wezterm")

    if found:
        return found

    for candidate in (
        "/Applications/WezTerm.app/Contents/MacOS/wezterm",
        os.path.expanduser("~/Applications/WezTerm.app/Contents/MacOS/wezterm"),
    ):
        if os.access(candidate, os.X_OK):
            return candidate

    return None


def make_inbox(path=INBOX):
    """The inbox, private to this user: anyone who can write to it can put text on a pane."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)

    if os.path.islink(path) or not os.path.isdir(path) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("the inbox %s must be a folder of yours that only you can use (0700)" % path)

    return path


def is_alive(path=INBOX, now=None):
    """Whether the WezTerm side (inline_images.lua) has written its heartbeat lately."""
    try:
        return (now or time.time()) - os.stat(os.path.join(path, "alive")).st_mtime < ALIVE_SECONDS
    except OSError:
        return False


def post(path, pane_id, number, data):
    """Put one sequence in the inbox for a pane, whole: the reader never sees half a file."""
    descriptor, temporary = tempfile.mkstemp(dir=path, prefix=".", suffix=".part")

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)

        os.replace(temporary, os.path.join(path, "%s-%016d.osc" % (pane_id, number)))
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def cell_width(char):
    """How many cells a character takes: 2 for wide ones, 0 for combining marks."""
    if unicodedata.combining(char) or char in "​‌‍︎️":
        return 0

    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def split_cells(text):
    """The text of each cell of a line: a wide character, then "" for the cell it also covers."""
    cells = []

    for char in text:
        width = cell_width(char)

        if width == 0:
            if cells:
                cells[-1] += char
            continue

        cells.append(char)

        if width == 2:
            cells.append("")

    return cells


class Style:
    """What `iterm_overlay.is_image` asks of a cell's style."""

    class _Image:
        name = "ITERM2"

    def __init__(self, image):
        self.image = Style._Image() if image else None


class Line:
    """One row of the screen, as the iTerm2 API's LineContents answers."""

    def __init__(self, text, row, drawn):
        self.string = text
        self.cells = split_cells(text)
        self.row = row
        self.drawn = drawn

    def string_at(self, column):
        if column < 0:
            raise IndexError(column)

        return self.cells[column] if column < len(self.cells) else ""

    def style_at(self, column):
        blank = self.string_at(column) in (" ", "")

        return Style(
            blank
            and any(
                top <= self.row < top + rows and left <= column < left + columns for top, left, rows, columns in self.drawn
            )
        )


class Contents:
    """One read of the screen, as the iTerm2 API's ScreenContents answers."""

    number_of_lines_above_screen = 0

    def __init__(self, text, rows, drawn):
        lines = text.split("\n")

        if lines and lines[-1] == "":
            lines.pop()

        self.number_of_lines = max(rows, len(lines))
        self.texts = lines + [""] * (self.number_of_lines - len(lines))
        self.drawn = list(drawn)

    def line(self, row):
        return Line(self.texts[row], row, self.drawn)


class GridSize:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class Pane:
    """A WezTerm pane, with the three things that `Watcher` asks of an iTerm2 session."""

    def __init__(self, wezterm, pane_id, inbox=INBOX):
        self.wezterm = wezterm
        self.pane_id = str(pane_id)
        self.inbox = inbox
        self.grid_size = GridSize(80, 24)
        self.drawn = []  # (top, left, rows, columns) of each image drawn, newest last
        self.geometry_at = 0.0
        self.last_text = None
        self.primed = None  # a read made by the poll, for the look that follows it
        self.posted = 0

    async def command(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            self.wezterm,
            "--skip-config",
            "cli",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            out, err = await asyncio.wait_for(process.communicate(), COMMAND_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError("wezterm cli %s took more than %d s" % (arguments[0], COMMAND_SECONDS))

        if process.returncode != 0:
            raise RuntimeError("wezterm cli %s: %s" % (arguments[0], err.decode(errors="replace").strip()))

        return out.decode("utf-8", errors="replace")

    async def geometry(self, force=False):
        """The pane's size, read again once in a while. False when the pane is gone."""
        now = time.monotonic()

        if not force and now - self.geometry_at < GEOMETRY_SECONDS:
            return True

        self.geometry_at = now

        for pane in json.loads(await self.command("list", "--format", "json")):
            if str(pane.get("pane_id")) == self.pane_id:
                size = pane.get("size", {})
                self.grid_size = GridSize(int(size.get("cols", 80)), int(size.get("rows", 24)))
                return True

        return False

    async def read_text(self):
        return await self.command("get-text", "--pane-id", self.pane_id)

    async def async_get_screen_contents(self):
        if self.primed is not None:
            text, self.primed = self.primed, None
        else:
            text = await self.read_text()

        return Contents(text, self.grid_size.height, self.drawn)

    async def async_inject(self, data):
        if not is_alive(self.inbox):
            raise RuntimeError("the WezTerm side (inline_images.lua) stopped answering")

        self.posted += 1
        number = int(time.time() * 1000) * 1000 + self.posted % 1000
        await asyncio.get_running_loop().run_in_executor(None, post, self.inbox, self.pane_id, number, data)


def sequence(row, column, columns, rows, data):
    """Save the cursor, erase the cells, draw the image over them, and restore the cursor.

    WezTerm draws an image over the text in its cells and `get-text` still
    reads that text: the erase is what lets a look tell an image cell (blank)
    from a cell that Claude Code painted again (its glyph). One sequence, which
    WezTerm parses whole, so no output of Claude Code lands between the erase and
    the image.
    """
    payload = base64.b64encode(data).decode("ascii")
    arguments = "inline=1;size=%d;width=%d;height=%d;preserveAspectRatio=0;doNotMoveCursor=1" % (len(data), columns, rows)
    erase = "".join("\x1b[%d;%dH\x1b[%dX" % (row + 1 + index, column + 1, columns) for index in range(rows))

    return (
        "\x1b7\x1b[0m%s\x1b[%d;%dH\x1b]1337;File=%s:%s\x07\x1b8" % (erase, row + 1, column + 1, arguments, payload)
    ).encode()


class WeztermWatcher(Watcher):
    def encode(self, row, column, columns, rows, data):
        drawn = self.session.drawn
        # A newer image on the same cells replaces an older one.
        drawn[:] = [
            rect
            for rect in drawn
            if not (rect[0] < row + rows and row < rect[0] + rect[2] and rect[1] < column + columns and column < rect[1] + rect[3])
        ]
        drawn.append((row, column, rows, columns))
        del drawn[:-MAX_DRAWN]

        return sequence(row, column, columns, rows, data)


async def watch(pane, parent):
    try:
        if not await pane.geometry(force=True):
            emit({"ok": False, "error": "WezTerm has no pane %s" % pane.pane_id})
            return
    except Exception as problem:
        emit({"ok": False, "error": "cannot reach WezTerm with `wezterm cli`: %s" % problem})
        return

    try:
        make_inbox(pane.inbox)
    except Exception as problem:
        emit({"ok": False, "error": str(problem)})
        return

    waited = time.monotonic()

    while not is_alive(pane.inbox):
        if time.monotonic() - waited > HELLO_SECONDS:
            emit(
                {
                    "ok": False,
                    "error": "add the WezTerm side to your config: copy mod/wezterm/inline_images.lua next to wezterm.lua "
                    "and call require('inline_images').apply(config)",
                }
            )
            return

        await asyncio.sleep(0.25)

    loop = asyncio.get_running_loop()
    changed = asyncio.Event()

    def wake(delay=0):
        if delay > 0:
            loop.call_later(delay, changed.set)
        else:
            changed.set()

    watcher = WeztermWatcher(pane, Overlays(), wake)
    emit({"ok": True, "ready": True})
    last_look = 0.0
    busy = True

    while os.getppid() == parent:
        await asyncio.sleep(FAST_SECONDS if busy else SLOW_SECONDS)

        try:
            if not await pane.geometry():
                debug("pane %s is gone" % pane.pane_id)
                return

            text = await pane.read_text()
        except Exception as problem:
            debug("read failed: %s" % problem)
            continue

        now = time.monotonic()

        if text != pane.last_text:
            pane.last_text = text
            watcher.screen_changed()
            changed.set()

        busy = bool(watcher.placements) or bool(BRAILLE.search(text))

        if not changed.is_set() and not (busy and now - last_look >= HEARTBEAT_SECONDS):
            continue

        changed.clear()
        pane.primed = text
        last_look = now

        try:
            await asyncio.wait_for(watcher.look(), LOOK_SECONDS)
        except asyncio.TimeoutError:
            debug("a look took more than %d s: given up" % LOOK_SECONDS)
        except Exception as problem:  # one bad frame must not end the overlay
            debug("error %s: %s" % (type(problem).__name__, problem))
        finally:
            pane.primed = None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pane", required=True, help="WEZTERM_PANE")
    arguments = parser.parse_args()
    parent = os.getppid()

    if parent == 1:
        emit({"ok": False, "error": "the process that started the overlay has exited"})
        sys.exit(1)

    wezterm = wezterm_binary()

    if wezterm is None:
        emit({"ok": False, "error": "the wezterm command is not on the PATH"})
        sys.exit(2)

    asyncio.run(watch(Pane(wezterm, arguments.pane), parent))


if __name__ == "__main__":
    main()
