#!/usr/bin/env python3
"""Draw real images over the cell previews of inline-images, in WezTerm.

The same daemon as `iterm_overlay.py`, with WezTerm in place of the iTerm2
Python API. The `Watcher` there decides what to draw and when; this file gives
it a WezTerm pane to read and write:

- It reads the screen from a file that `mod/wezterm/inline_images.lua` writes
  from inside WezTerm (`pane:get_lines_as_text`) while this daemon asks for it.
  Not `wezterm cli get-text`: that sends every image in the pane with the text,
  and polling it stalled WezTerm.
- It cannot write the image to the pane's tty: those bytes mix with Claude
  Code's own output, and a large image then shows as base64 text. It drops the
  image, as one escape sequence, in a private folder (the inbox), and
  `mod/wezterm/inline_images.lua`, loaded by the WezTerm config, hands it to
  `pane:inject_output`, which WezTerm parses apart from Claude Code's output.
  Without that file in the config the overlay does not run. WezTerm draws the
  iTerm2 protocol, but only a single `File=` sequence: it ignores
  `MultipartFile`.
- The screen text does not say which cells show an image: WezTerm draws an image
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
import sys
import tempfile
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

if HERE not in sys.path:
    sys.path.insert(0, HERE)

from iterm_overlay import BRAILLE, LOOK_SECONDS, Overlays, Watcher, debug, emit  # noqa: E402

HEARTBEAT_SECONDS = 1.0  # look anyway, though the screen has not changed, while boxes show
MAX_DRAWN = 256  # rectangles remembered as drawn
ALIVE_SECONDS = 10  # the WezTerm side wrote its heartbeat this recently: it is there
HELLO_SECONDS = 6  # how long to wait for that heartbeat at the start
PENDING_SECONDS = 5  # an image not picked up by then is given up on
WATCH_REFRESH_SECONDS = 3  # how often to tell the WezTerm side the pane is still watched
POLL_SECONDS = 0.1  # between checks of the screen file
INBOX = os.environ.get("INLINE_IMAGES_WEZTERM_INBOX") or os.path.join(
    os.path.expanduser("~"), ".local", "state", "inline-images", "wezterm"
)


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

        final = os.path.join(path, "%s-%016d.osc" % (pane_id, number))
        os.replace(temporary, final)
        return final
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def post_text(path, text):
    """Write a small file whole."""
    temporary = path + ".part"

    with open(temporary, "w") as handle:
        handle.write(text)

    os.replace(temporary, path)


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
    """A WezTerm pane, with the three things that `Watcher` asks of an iTerm2 session.

    The screen comes from `inline_images.lua`, which writes it to <inbox>/<pane>.screen
    while this daemon keeps <inbox>/<pane>.watch fresh.
    """

    def __init__(self, pane_id, inbox=INBOX):
        self.pane_id = str(pane_id)
        self.inbox = inbox
        self.grid_size = GridSize(80, 24)
        self.drawn = []  # (top, left, rows, columns) of each image drawn, newest last
        self.posted = 0
        self.pending = []  # (path, when) of images posted and not yet picked up by WezTerm
        self.text = None
        self.sequence = None
        self.stamp = None  # (mtime_ns, size) of the screen file last read
        self.watched_at = 0.0

    @property
    def screen_path(self):
        return os.path.join(self.inbox, "%s.screen" % self.pane_id)

    def watch(self, now=None):
        """Ask the WezTerm side for this pane's screen, again every few seconds."""
        now = now or time.time()

        if now - self.watched_at >= WATCH_REFRESH_SECONDS:
            post_text(os.path.join(self.inbox, "%s.watch" % self.pane_id), "%d\n" % int(now))
            self.watched_at = now

    def read_screen(self):
        """Read the screen file when it changed. True when there is a screen to look at."""
        try:
            info = os.stat(self.screen_path)
        except OSError:
            return False

        stamp = (info.st_mtime_ns, info.st_size)

        if stamp != self.stamp:
            with open(self.screen_path, encoding="utf-8", errors="replace") as handle:
                data = handle.read()

            head, _, text = data.partition("\n")
            rows, columns, sequence = (int(value) for value in head.split())
            self.grid_size = GridSize(columns, rows)
            self.text, self.sequence, self.stamp = text, sequence, stamp

        return self.text is not None

    async def async_get_screen_contents(self):
        self.read_screen()

        return Contents(self.text or "", self.grid_size.height, self.drawn)

    async def async_inject(self, data):
        self.posted += 1
        number = int(time.time() * 1000) * 1000 + self.posted % 1000
        path = await asyncio.get_running_loop().run_in_executor(None, post, self.inbox, self.pane_id, number, data)
        self.pending.append((path, time.monotonic()))

    def waiting(self):
        """Whether an image is still in the inbox: until WezTerm draws it, the screen shows the
        box glyphs where it goes, and a look would take them for a repaint and draw again."""
        now = time.monotonic()
        self.pending = [(path, at) for path, at in self.pending if now - at < PENDING_SECONDS and os.path.exists(path)]

        return bool(self.pending)


def sequence(row, column, columns, rows, data):
    """Save the cursor, erase the cells, draw the image over them, and restore the cursor.

    WezTerm draws an image over the text in its cells and the screen text still
    holds that text: the erase is what lets a look tell an image cell (blank)
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
    # An image posted in this look is not on the screen yet: the rest of the look would
    # read the box glyphs it replaces and draw it again.
    async def check(self, placement, screen, boxes, above):
        if not self.session.waiting():
            await Watcher.check(self, placement, screen, boxes, above)

    async def show(self, box, screen, above, width, now):
        if not self.session.waiting():
            await Watcher.show(self, box, screen, above, width, now)

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
        make_inbox(pane.inbox)
        pane.watch()
    except Exception as problem:
        emit({"ok": False, "error": str(problem)})
        return

    waited = time.monotonic()

    # The WezTerm side answers on its next tick. A heartbeat file that exists, however
    # old, means it is installed and WezTerm is only holding back (a hidden window):
    # start, and draw once it answers.
    while not (is_alive(pane.inbox) and pane.read_screen()):
        if time.monotonic() - waited > HELLO_SECONDS:
            if os.path.exists(os.path.join(pane.inbox, "alive")):
                break

            emit(
                {
                    "ok": False,
                    "error": "add the WezTerm side to your config, or update it: copy mod/wezterm/inline_images.lua "
                    "next to wezterm.lua and call require('inline_images').apply(config)",
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
    last_sequence = None
    stale = False

    while os.getppid() == parent:
        await asyncio.sleep(POLL_SECONDS)

        try:
            pane.watch()
            pane.read_screen()
        except Exception as problem:
            debug("read failed: %s" % problem)
            continue

        # The WezTerm side is quiet: the config is being loaded again, or macOS holds back
        # a WezTerm that nobody can see. Draw nothing until it answers: an image posted now
        # would wait, and the box glyphs would be read as repaints.
        if not is_alive(pane.inbox):
            if not stale:
                debug("the WezTerm side is quiet: waiting")
            stale = True
            continue

        stale = False

        if pane.waiting():
            continue

        now = time.monotonic()

        if pane.sequence != last_sequence:
            last_sequence = pane.sequence
            watcher.screen_changed()
            changed.set()

        busy = bool(watcher.placements) or bool(BRAILLE.search(pane.text or ""))

        if not changed.is_set() and not (busy and now - last_look >= HEARTBEAT_SECONDS):
            continue

        changed.clear()
        last_look = now

        try:
            await asyncio.wait_for(watcher.look(), LOOK_SECONDS)
        except asyncio.TimeoutError:
            debug("a look took more than %d s: given up" % LOOK_SECONDS)
        except Exception as problem:  # one bad frame must not end the overlay
            debug("error %s: %s" % (type(problem).__name__, problem))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pane", required=True, help="WEZTERM_PANE")
    arguments = parser.parse_args()
    parent = os.getppid()

    if parent == 1:
        emit({"ok": False, "error": "the process that started the overlay has exited"})
        sys.exit(1)

    asyncio.run(watch(Pane(arguments.pane), parent))


if __name__ == "__main__":
    main()
