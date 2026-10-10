#!/usr/bin/env python3
"""Draw real images over the cell previews of inline-images, in iTerm2.

Claude Code owns the screen, so a mod cannot print an image itself. In iTerm2
the mod draws its cell preview with a hidden marker at the start of every row
(see `render.py cells --marker`): the box's id and the row's number. This
daemon watches one iTerm2 session through the iTerm2 Python API and draws the
real image over each box it finds, with the iTerm2 inline images protocol, as
if the program had printed it there.

Claude Code repaints only the cells that it thinks have changed. Two rules
keep the screen right:

1. Every marker cell differs from the one in the same column on any other row
   of the box. When a box moves, all its marker cells change, Claude Code paints
   them, and the box is found and drawn in its new place. Box cells never equal
   text or blank cells, so no piece of an old image stays outside a box.
2. When Claude Code paints part of a drawn image again but the box has not
   moved (a menu was drawn over it and is gone, say), the painted cells are box
   cells and no marker of that box shows: the image is drawn again in place.

A box is drawn only where it stands still for one look, and only the rows of it
that are on the screen, so a draw never scrolls the screen. Before any draw the
screen is read again, and the box must still be where it was.

    iterm_overlay.py --session <ITERM_SESSION_ID>

It prints one JSON line when it is ready, or one with the reason it cannot run,
and then runs until its parent exits. It needs `python3 -m pip install iterm2`
and iTerm2 > Settings > General > Magic > Enable Python API. With
INLINE_IMAGES_DEBUG=<file> it writes what it does to that file.
"""

import argparse
import asyncio
import base64
import io
import json
import os
import re
import sys
import time
from collections import OrderedDict, defaultdict

# render.py sits beside this file; it knows where the cache is. It needs no
# Pillow to be imported.
HERE = os.path.dirname(os.path.abspath(__file__))

if HERE not in sys.path:
    sys.path.insert(0, HERE)

import render  # noqa: E402

SIGNATURE = "⣿"
MARKER_CELLS = 5
BLANK = 0x2800
CHUNK = 65536  # base64 characters in one FilePart
SETTLE_SECONDS = 0.03  # let a frame that is being written finish first
QUIET_SECONDS = 0.3  # draw when the screen has not changed for this long
BUSY_STEADY_SECONDS = 2.0  # or, while it keeps changing (a turn runs), when a box stood still this long
HEARTBEAT_SECONDS = 0.5  # look anyway, in case a change was not reported
LOOK_INTERVAL = 0.1  # at most ten looks a second: a burst of changes is one look
LOOK_SECONDS = 10  # a look that takes longer is stuck: give up on it
MISSING_SECONDS = 5  # a marker with no record is not looked for again until then
MAX_RECORDS = 64  # records kept in memory, the least recently used dropped first
MAX_PICTURES = 4  # decoded box PNGs kept to cut rows from
MAX_CROP_BYTES = 64 * 1024 * 1024  # PNGs of cut rows kept

BRAILLE = re.compile("[⠀-⣿]")


def emit(message):
    print(json.dumps(message), flush=True)


DEBUG_LOG = os.environ.get("INLINE_IMAGES_DEBUG")


def debug(text):
    """One line in the file that INLINE_IMAGES_DEBUG names, when it names one."""
    if DEBUG_LOG:
        with open(DEBUG_LOG, "a") as handle:
            handle.write("%.3f %s\n" % (time.time(), text))


try:
    from PIL import Image
except ImportError:  # without Pillow, only a box that is all on the screen is drawn
    Image = None


def overlay_dir():
    """Where render.py writes its records: the same function finds the cache."""
    return os.path.join(render.cache_dir(), "overlays")


class Overlays:
    """The images the mod registered, by marker, read when first seen.

    Everything here is bounded: records, decoded pictures and cut rows are each
    dropped least recently used first, so a long session does not grow.
    """

    def __init__(self):
        self.loaded = OrderedDict()  # marker -> (columns, rows, PNG bytes)
        self.glyphs = {}
        self.missing = {}  # marker -> when it had no record
        self.pictures = OrderedDict()  # marker -> the decoded PNG
        self.crops = OrderedDict()  # (marker, first, last) -> PNG bytes
        self.crop_bytes = 0

    def glyph(self, marker, row, column):
        """The glyph that render.py drew at one cell of a box, or None."""
        found = self.loaded.get(marker)

        if found is None:
            return None

        columns = found[0]
        glyphs = self.glyphs.get(marker, "")
        at = row * columns + column

        return glyphs[at] if 0 <= column < columns and 0 <= at < len(glyphs) else None

    def get(self, marker):
        found = self.loaded.get(marker)

        if found is not None:
            self.loaded.move_to_end(marker)
            return found

        now = time.monotonic()

        # Braille text can look like a marker by chance: do not read the disk at every look for it.
        if now - self.missing.get(marker, -MISSING_SECONDS) < MISSING_SECONDS:
            return None

        try:
            with open(os.path.join(overlay_dir(), marker + ".json")) as handle:
                record = json.load(handle)

            with open(record["png"], "rb") as handle:
                data = handle.read()

            found = (int(record["columns"]), int(record["rows"]), data)
            glyphs = str(record.get("glyphs", ""))
        except (OSError, ValueError, KeyError, TypeError, render.UnsafeCache) as problem:
            debug("no record for %s: %s" % (marker, problem))

            if len(self.missing) > 4096:
                self.missing.clear()

            self.missing[marker] = now
            return None  # not registered (yet): look again later

        self.missing.pop(marker, None)
        self.loaded[marker] = found
        self.glyphs[marker] = glyphs

        while len(self.loaded) > MAX_RECORDS:
            self.forget(next(iter(self.loaded)))

        return found

    def forget(self, marker):
        self.loaded.pop(marker, None)
        self.glyphs.pop(marker, None)
        self.pictures.pop(marker, None)

        for key in [key for key in self.crops if key[0] == marker]:
            self.crop_bytes -= len(self.crops.pop(key))

    def picture(self, marker):
        """The box PNG decoded, once for all the cuts of it."""
        found = self.pictures.get(marker)

        if found is not None:
            self.pictures.move_to_end(marker)
            return found

        found = Image.open(io.BytesIO(self.loaded[marker][2]))
        found.load()
        self.pictures[marker] = found

        while len(self.pictures) > MAX_PICTURES:
            self.pictures.popitem(last=False)

        return found

    def rows(self, marker, first, last):
        """The PNG of rows `first` to `last` of the box, or None when it cannot be cut."""
        columns, rows, data = self.loaded[marker]

        if first == 0 and last == rows - 1:
            return data

        key = (marker, first, last)
        found = self.crops.get(key)

        if found is not None:
            self.crops.move_to_end(key)
            return found

        if Image is None:
            return None

        picture = self.picture(marker)
        top = round(picture.height * first / rows)
        bottom = round(picture.height * (last + 1) / rows)
        out = io.BytesIO()
        # Level 1: the PNG goes straight to iTerm2 on this machine, so speed beats size.
        picture.crop((0, top, picture.width, bottom)).save(out, "PNG", compress_level=1)
        found = out.getvalue()
        self.crops[key] = found
        self.crop_bytes += len(found)

        while self.crop_bytes > MAX_CROP_BYTES and len(self.crops) > 1:
            _, old = self.crops.popitem(last=False)
            self.crop_bytes -= len(old)

        return found


# ---------------------------------------------------------------------------
# Reading the screen. `line` is the API's LineContents, or anything with
# `string`, `string_at(x)` and `style_at(x)`.


def cell(line, column):
    """The text of one cell; empty past the end of the line, which the API trims."""
    try:
        return line.string_at(column)
    except IndexError:
        return ""


def style(line, column):
    try:
        return line.style_at(column)
    except IndexError:
        return None


def is_image(line, column):
    found = style(line, column)

    return found is not None and found.image is not None and found.image.name == "ITERM2"


class Snapshot:
    """One read of the screen, each row's cells read at most once.

    The API builds a new LineContents, parsing all its style runs, at every
    call of `line`, and a look asks for the same rows many times: for markers,
    for damage, for clean runs. Here a row is read once, into a list of
    (text, is an image) for each cell.
    """

    def __init__(self, contents):
        self.contents = contents
        self.number_of_lines = contents.number_of_lines
        self.number_of_lines_above_screen = getattr(contents, "number_of_lines_above_screen", 0)
        self.lines = {}
        self.rows = {}

    def line(self, row):
        found = self.lines.get(row)

        if found is None:
            found = self.lines[row] = self.contents.line(row)

        return found

    def text(self, row):
        return self.line(row).string

    def cells(self, row, width):
        """(text, is an image) for the first `width` cells of a row, at least."""
        found = self.rows.get(row)

        if found is None or len(found) < width:
            line = self.line(row)
            found = found or []
            found.extend((cell(line, column), is_image(line, column)) for column in range(len(found), width))
            self.rows[row] = found

        return found


def snapshot(contents):
    return contents if isinstance(contents, Snapshot) else Snapshot(contents)


def read_marker(glyphs):
    """The (marker, row) that five glyphs carry, or None: render.py's marker_glyphs, undone."""
    if len(glyphs) != MARKER_CELLS or any(len(glyph) != 1 or not BLANK <= ord(glyph) <= BLANK + 0xFF for glyph in glyphs):
        return None

    check, *shuffled, row = (ord(glyph) - BLANK for glyph in glyphs)
    marker = bytes((value - row * 37 - index * 11) & 0xFF for index, value in enumerate(shuffled, start=1))

    if (marker[0] + marker[1] + marker[2] + row * 37 + 0x5A) & 0xFF != check:
        return None

    return marker.hex(), row


def is_braille(text):
    return len(text) == 1 and BLANK <= ord(text) <= BLANK + 0xFF


def markers_in(contents, width):
    """Each row marker on the screen: (screen row, column, marker, row of the box)."""
    screen = snapshot(contents)

    for row in range(screen.number_of_lines):
        # Most rows have no braille at all: one search of the row's text skips them.
        if not BRAILLE.search(screen.text(row)):
            continue

        texts = [text for text, _ in screen.cells(row, width)]

        for column in range(max(0, width - MARKER_CELLS + 1)):
            if not is_braille(texts[column]):
                continue

            found = read_marker(texts[column : column + MARKER_CELLS])

            if found is not None:
                yield (row, column) + found


def spans(found):
    """Group row markers into boxes: (marker, column, screen row of box row 0, first row, last row).

    The rows run from the first marker seen to the last: rows between them that
    show no marker still belong to the box (Claude Code may have left a marker
    cell under an image because it did not change).
    """
    boxes = defaultdict(list)

    for screen_row, column, marker, box_row in found:
        boxes[(marker, column, screen_row - box_row)].append(box_row)

    for (marker, column, top), box_rows in sorted(boxes.items()):
        yield marker, column, top, min(box_rows), max(box_rows)


# ---------------------------------------------------------------------------
# Writing to the screen.


def sequence(row, column, columns, rows, data):
    """Save the cursor, draw the image at a cell, and restore the cursor.

    The image goes in chunks (MultipartFile, as imgcat 3 sends it): iTerm2 prints
    one File= sequence of about a megabyte as text. The whole transfer is one
    inject, so no output of Claude Code can land between its chunks.
    """
    payload = base64.b64encode(data).decode("ascii")
    arguments = "inline=1;size=%d;width=%d;height=%d;preserveAspectRatio=0;doNotMoveCursor=1" % (
        len(data),
        columns,
        rows,
    )
    parts = "".join("\x1b]1337;FilePart=%s\x07" % payload[at : at + CHUNK] for at in range(0, len(payload), CHUNK))

    return (
        "\x1b7\x1b[%d;%dH\x1b]1337;MultipartFile=%s\x07%s\x1b]1337;FileEnd\x07\x1b8"
        % (row + 1, column + 1, arguments, parts)
    ).encode()


BLANKS = (" ", "⠀", "", "\x00")


def damage(placement, contents, overlays):
    """How a drawn image stands now.

    "intact":  every cell still shows the image.
    "painted": Claude Code painted cells of it again, each with the glyph that
               render.py drew there, one or more of them not blank: the box is
               still here, so the image can go back.
    "covered": something else sits on part of it (a hint, a menu): keep
               watching; when it goes, the box cells come back as "painted".
    "gone":    nothing of the image or the box is left.
    """
    screen = snapshot(contents)
    images = matching = foreign = 0
    telling = False
    end = placement["column"] + placement["columns"]

    for index in range(placement["rows"]):
        row = placement["top"] + index

        if not 0 <= row < screen.number_of_lines:
            continue  # off the screen: what is left is all that can be checked

        cells = screen.cells(row, end)

        for offset in range(placement["columns"]):
            text, image = cells[placement["column"] + offset]

            if image:
                images += 1
                continue

            expected = overlays.glyph(placement["marker"], placement["first"] + index, offset)

            if expected is not None and (text == expected or (text in BLANKS and expected in BLANKS)):
                matching += 1
                telling = telling or text not in BLANKS
            else:
                foreign += 1

    if foreign == 0 and matching == 0:
        return "intact"

    if foreign == 0:
        return "painted" if telling else "covered"

    # A blank cell matches a blank of the box by chance: only glyphs tell that the box is there.
    return "gone" if images == 0 and not telling else "covered"


def signature(placement, contents):
    """What `damage` reads for a placement: when it has not changed, neither has the answer."""
    screen = snapshot(contents)
    end = placement["column"] + placement["columns"]
    rows = range(max(0, placement["top"]), min(screen.number_of_lines, placement["top"] + placement["rows"]))

    return (placement["top"],) + tuple(tuple(screen.cells(row, end)[placement["column"] : end]) for row in rows)


def clean_runs(marker, top, column, first, last, contents, overlays):
    """The runs of box rows that show only the box: its glyphs, or its image.

    A row with anything else on it (Claude Code draws hints and menus over the
    transcript) is left out, so the image never hides them.
    """
    screen = snapshot(contents)
    columns = overlays.loaded[marker][0]
    runs = []
    start = None

    for index in range(first, last + 1):
        cells = screen.cells(top + index, column + columns)
        clean = True

        for offset in range(columns):
            text, image = cells[column + offset]

            if image:
                continue

            expected = overlays.glyph(marker, index, offset)

            if expected is None or not (text == expected or (text in BLANKS and expected in BLANKS)):
                clean = False
                break

        if clean and start is None:
            start = index
        elif not clean and start is not None:
            runs.append((start, index - 1))
            start = None

    if start is not None:
        runs.append((start, last))

    return runs


# ---------------------------------------------------------------------------


class Watcher:
    """What one look at the screen does. `session` is the API's Session, or a fake.

    `wake(delay)` asks for another look after `delay` seconds.
    """

    def __init__(self, session, overlays, wake):
        self.session = session
        self.overlays = overlays
        self.wake = wake
        self.placements = []  # what this daemon drew and has not seen broken
        self.seen = {}  # box -> when it was first seen where it is
        self.scrolled = None  # lines above the screen at the last look
        # When the screen last changed. Claude Code writes each frame as a
        # synchronized update, and what the API reports can lag a frame that is
        # arriving: a draw made then lands where the box was, not where it is.
        # So a box is drawn when the screen is quiet, or has long stood still.
        self.changed_at = 0.0

    def screen_changed(self):
        self.changed_at = time.monotonic()

    def wait_needed(self, now, since):
        """How long to wait before a draw is safe, or 0 when it is safe now."""
        quiet = QUIET_SECONDS - (now - self.changed_at)
        steady = BUSY_STEADY_SECONDS - (now - since)

        return 0 if quiet <= 0 or steady <= 0 else min(quiet, steady)

    async def read(self):
        return Snapshot(await self.session.async_get_screen_contents())

    def drop(self, placement):
        if placement in self.placements:
            self.placements.remove(placement)

    async def look(self):
        screen = await self.read()
        above = screen.number_of_lines_above_screen
        width = self.session.grid_size.width

        # Lines that went into the scrollback took the images with them.
        if self.scrolled is not None and above != self.scrolled:
            for placement in self.placements:
                placement["top"] -= above - self.scrolled

        self.scrolled = above
        boxes = list(spans(markers_in(screen, width)))

        # 1. The images already drawn: still whole, painted over in place, covered, or gone.
        # One box that fails (a record half gone, a PNG that will not cut) must not stop the others.
        for placement in list(self.placements):
            try:
                await self.check(placement, screen, boxes, above)
            except Exception as problem:
                self.drop(placement)
                debug("error checking %s: %s: %s" % (placement["marker"], type(problem).__name__, problem))

        # 2. Boxes on the screen that show their markers: draw the image.
        now = time.monotonic()

        for box in boxes:
            try:
                await self.show(box, screen, above, width, now)
            except Exception as problem:
                debug("error drawing %s: %s: %s" % (box[0], type(problem).__name__, problem))

        for box in list(self.seen):
            if box not in boxes:
                del self.seen[box]

    async def check(self, placement, screen, boxes, above):
        top = placement["top"] - placement["first"]  # the screen row of the box's row 0
        moved = any(box[0] == placement["marker"] and box[2] != top for box in boxes)

        if placement["top"] + placement["rows"] <= 0 or placement["top"] >= screen.number_of_lines or moved:
            self.drop(placement)  # off the screen, or the box is somewhere else now
            return

        if self.overlays.get(placement["marker"]) is None:
            self.drop(placement)  # its record is gone: nothing to compare the screen with
            return

        mark = signature(placement, screen)

        if mark == placement.get("signature"):
            return  # its cells are as they were at the last look, and so is its state

        state = damage(placement, screen, self.overlays)

        if state in ("intact", "covered"):
            placement["signature"] = mark
            return

        debug("%s %s at screen row %d" % (state, placement["marker"], placement["top"]))

        if state == "gone":
            self.drop(placement)
            return

        # Painted over in place. Wait for a quiet screen, as step 2 does, then read
        # it again just before the draw: Claude Code may have moved or covered the box.
        wait = self.wait_needed(time.monotonic(), time.monotonic())

        if wait > 0:
            self.wake(wait + 0.01)
            return

        fresh = await self.read()

        if fresh.number_of_lines_above_screen != above or damage(placement, fresh, self.overlays) != "painted":
            self.wake(0)  # it changed: the next look decides what it is now
            return

        self.drop(placement)
        first = max(placement["first"], -top)
        last = min(placement["first"] + placement["rows"] - 1, fresh.number_of_lines - 1 - top)
        await self.draw(placement["marker"], top, placement["column"], first, last, fresh)

    async def show(self, box, screen, above, width, now):
        marker, column, top, first, last = box
        found = self.overlays.get(marker)

        if found is None:
            return

        columns, rows, _ = found
        last = min(last, rows - 1)
        first_row, last_row = top + first, top + last

        # Only rows that are on the screen: an image below the last row would scroll it.
        if first > last or first_row < 0 or last_row >= screen.number_of_lines or column + columns > width:
            return

        wait = self.wait_needed(now, self.seen.setdefault(box, now))

        if wait > 0:
            self.wake(wait + 0.01)
            return

        # Read the screen again just before the draw: the box must still be there.
        fresh = await self.read()

        if fresh.number_of_lines_above_screen != above or box not in set(spans(markers_in(fresh, width))):
            self.wake(0)
            return

        await self.draw(marker, top, column, first, last, fresh)

    async def draw(self, marker, top, column, first, last, contents):
        for run in clean_runs(marker, top, column, first, last, contents, self.overlays):
            await self.draw_rows(marker, top, column, run[0], run[1])

    async def draw_rows(self, marker, top, column, first, last):
        columns, rows, _ = self.overlays.get(marker)
        data = self.overlays.rows(marker, first, last)

        if data is None:
            return

        # The new image replaces any older one of this box on these rows.
        self.placements[:] = [
            placement
            for placement in self.placements
            if not (
                placement["marker"] == marker
                and placement["top"] - placement["first"] == top
                and placement["first"] <= last
                and first <= placement["first"] + placement["rows"] - 1
            )
        ]
        self.placements.append(
            {
                "marker": marker,
                "first": first,
                "top": top + first,
                "column": column,
                "rows": last - first + 1,
                "columns": columns,
            }
        )
        debug("draw %s rows %d-%d of %d at screen row %d, %d bytes" % (marker, first, last, rows, top + first, len(data)))
        await self.session.async_inject(sequence(top + first, column, columns, last - first + 1, data))


async def watch(connection, session_id, parent):
    import iterm2

    app = await iterm2.async_get_app(connection)
    session = app.get_session_by_id(session_id)

    if session is None:
        emit({"ok": False, "error": "iTerm2 has no session %s" % session_id})
        return

    changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def wake(delay=0):
        if delay > 0:
            loop.call_later(delay, changed.set)
        else:
            changed.set()

    watcher = Watcher(session, Overlays(), wake)

    async def stream():
        async with session.get_screen_streamer(want_contents=False) as streamer:
            while True:
                await streamer.async_get()
                watcher.screen_changed()
                changed.set()

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            changed.set()

    async def orphaned():
        # The mod's session is gone: so is the reason to run.
        while os.getppid() == parent:
            await asyncio.sleep(2)

    emit({"ok": True, "ready": True})
    tasks = [asyncio.ensure_future(job()) for job in (stream, heartbeat)]
    ending = asyncio.ensure_future(orphaned())
    changed.set()
    last_look = 0.0

    while not ending.done() and not tasks[0].done():
        waiting = asyncio.ensure_future(changed.wait())
        await asyncio.wait([waiting, ending, tasks[0]], return_when=asyncio.FIRST_COMPLETED)

        if not changed.is_set():
            waiting.cancel()
            continue

        # At most ten looks a second: the changes that come meanwhile are one look.
        pause = last_look + LOOK_INTERVAL - time.monotonic()

        if pause > 0:
            await asyncio.sleep(pause)

        changed.clear()
        await asyncio.sleep(SETTLE_SECONDS)
        last_look = time.monotonic()

        try:
            await asyncio.wait_for(watcher.look(), LOOK_SECONDS)
        except asyncio.TimeoutError:
            debug("a look took more than %d s: given up" % LOOK_SECONDS)
        except Exception as problem:  # one bad frame must not end the overlay
            debug("error %s: %s" % (type(problem).__name__, problem))

    for task in tasks:
        task.cancel()

    # The screen stream failed (iTerm2 quit, the API was turned off): say why,
    # for the mod's message, rather than end with no reason.
    if tasks[0].done() and not tasks[0].cancelled() and tasks[0].exception() is not None:
        problem = tasks[0].exception()
        emit({"ok": False, "error": "the iTerm2 screen stream stopped: %s: %s" % (type(problem).__name__, problem)})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, help="ITERM_SESSION_ID, or the id after its colon")
    arguments = parser.parse_args()
    session_id = arguments.session.split(":")[-1]

    # The parent, read before the connection: if it is gone already (the
    # process is init's), nothing would ever end this daemon.
    parent = os.getppid()

    if parent == 1:
        emit({"ok": False, "error": "the process that started the overlay has exited"})
        sys.exit(1)

    try:
        import iterm2
    except ImportError:
        emit({"ok": False, "error": "the iterm2 Python module is not installed: run `python3 -m pip install iterm2`"})
        sys.exit(2)

    try:
        iterm2.run_until_complete(lambda connection: watch(connection, session_id, parent), retry=False)
    except Exception as problem:
        emit(
            {
                "ok": False,
                "error": "cannot reach the iTerm2 Python API (Settings > General > Magic > Enable Python API): %s"
                % problem,
            }
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
