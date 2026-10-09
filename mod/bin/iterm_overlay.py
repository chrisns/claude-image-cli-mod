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
that are on the screen, so a draw never scrolls the screen.

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
import sys
import tempfile
import time
from collections import defaultdict

SIGNATURE = "⣿"
MARKER_CELLS = 5
BLANK = 0x2800
CHUNK = 65536  # base64 characters in one FilePart
SETTLE_SECONDS = 0.03  # let a frame that is being written finish first
STEADY_SECONDS = 0.06  # a box must stand still this long before it is drawn
HEARTBEAT_SECONDS = 0.5  # look anyway, in case a change was not reported


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
    return os.path.join(tempfile.gettempdir(), "inline-images-%d" % os.getuid(), "overlays")


class Overlays:
    """The images the mod registered, by marker, read when first seen."""

    def __init__(self):
        self.loaded = {}
        self.crops = {}
        self.glyphs = {}

    def glyph(self, marker, row, column):
        """The glyph that render.py drew at one cell of a box, or None."""
        columns = self.loaded[marker][0]
        glyphs = self.glyphs.get(marker, "")
        at = row * columns + column

        return glyphs[at] if 0 <= column < columns and 0 <= at < len(glyphs) else None

    def get(self, marker):
        if marker in self.loaded:
            return self.loaded[marker]

        found = None

        try:
            with open(os.path.join(overlay_dir(), marker + ".json")) as handle:
                record = json.load(handle)

            with open(record["png"], "rb") as handle:
                data = handle.read()

            found = (int(record["columns"]), int(record["rows"]), data)
            self.glyphs[marker] = record.get("glyphs", "")
        except (OSError, ValueError, KeyError):
            pass  # not registered (yet): look again next time

        if found is not None:
            self.loaded[marker] = found

        return found

    def rows(self, marker, first, last):
        """The PNG of rows `first` to `last` of the box, or None when it cannot be cut."""
        columns, rows, data = self.loaded[marker]

        if first == 0 and last == rows - 1:
            return data

        key = (marker, first, last)

        if key not in self.crops:
            if Image is None:
                return None

            with Image.open(io.BytesIO(data)) as picture:
                top = round(picture.height * first / rows)
                bottom = round(picture.height * (last + 1) / rows)
                out = io.BytesIO()
                picture.crop((0, top, picture.width, bottom)).save(out, "PNG")

            self.crops[key] = out.getvalue()

        return self.crops[key]


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


def read_marker(glyphs):
    """The (marker, row) that five glyphs carry, or None: render.py's marker_glyphs, undone."""
    if len(glyphs) != MARKER_CELLS or any(len(glyph) != 1 or not BLANK <= ord(glyph) <= BLANK + 0xFF for glyph in glyphs):
        return None

    check, *shuffled, row = (ord(glyph) - BLANK for glyph in glyphs)
    marker = bytes((value - row * 37 - index * 11) & 0xFF for index, value in enumerate(shuffled, start=1))

    if (marker[0] + marker[1] + marker[2] + row * 37 + 0x5A) & 0xFF != check:
        return None

    return marker.hex(), row


def markers_in(contents, width):
    """Each row marker on the screen: (screen row, column, marker, row of the box)."""
    for row in range(contents.number_of_lines):
        line = contents.line(row)
        text = line.string

        if not any(BLANK <= ord(character) <= BLANK + 0xFF for character in text):
            continue

        for column in range(max(0, width - MARKER_CELLS + 1)):
            found = read_marker([cell(line, column + i) for i in range(MARKER_CELLS)])

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


BLANKS = (" ", "\u2800", "", "\x00")


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
    images = matching = foreign = 0
    telling = False

    for index in range(placement["rows"]):
        row = placement["top"] + index

        if not 0 <= row < contents.number_of_lines:
            continue  # off the screen: what is left is all that can be checked

        line = contents.line(row)

        for offset in range(placement["columns"]):
            column = placement["column"] + offset

            if is_image(line, column):
                images += 1
                continue

            text = cell(line, column)
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


def clean_runs(marker, top, column, first, last, contents, overlays):
    """The runs of box rows that show only the box: its glyphs, or its image.

    A row with anything else on it (Claude Code draws hints and menus over the
    transcript) is left out, so the image never hides them.
    """
    runs = []
    start = None

    for index in range(first, last + 1):
        line = contents.line(top + index)
        clean = True

        for offset in range(overlays.loaded[marker][0]):
            if is_image(line, column + offset):
                continue

            text = cell(line, column + offset)
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


async def watch(connection, session_id):
    import iterm2

    app = await iterm2.async_get_app(connection)
    session = app.get_session_by_id(session_id)

    if session is None:
        emit({"ok": False, "error": "iTerm2 has no session %s" % session_id})
        return

    overlays = Overlays()
    placements = []  # what this daemon drew and has not seen broken
    seen = {}  # box -> when it was first seen where it is
    scrolled = None  # lines above the screen at the last look
    changed = asyncio.Event()
    parent = os.getppid()

    async def draw(marker, top, column, first, last, contents):
        for run in clean_runs(marker, top, column, first, last, contents, overlays):
            await draw_rows(marker, top, column, run[0], run[1])

    async def draw_rows(marker, top, column, first, last):
        columns, rows, _ = overlays.get(marker)
        data = overlays.rows(marker, first, last)

        if data is None:
            return

        # The new image replaces any older one of this box on these rows.
        placements[:] = [
            placement
            for placement in placements
            if not (
                placement["marker"] == marker
                and placement["top"] - placement["first"] == top
                and placement["first"] <= last
                and first <= placement["first"] + placement["rows"] - 1
            )
        ]
        placements.append(
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
        await session.async_inject(sequence(top + first, column, columns, last - first + 1, data))

    async def look():
        nonlocal scrolled

        contents = await session.async_get_screen_contents()
        above = contents.number_of_lines_above_screen
        width = session.grid_size.width
        lines = contents.number_of_lines

        # Lines that went into the scrollback took the images with them.
        if scrolled is not None and above != scrolled:
            for placement in placements:
                placement["top"] -= above - scrolled

        scrolled = above
        boxes = list(spans(markers_in(contents, width)))

        # 1. The images already drawn: still whole, painted over in place, covered, or gone.
        for placement in list(placements):
            top = placement["top"] - placement["first"]  # the screen row of the box's row 0
            moved = any(box[0] == placement["marker"] and box[2] != top for box in boxes)

            if placement["top"] + placement["rows"] <= 0 or placement["top"] >= lines or moved:
                placements.remove(placement)  # off the screen, or the box is somewhere else now
                continue

            state = damage(placement, contents, overlays)

            if state in ("intact", "covered"):
                continue

            placements.remove(placement)
            debug("%s %s at screen row %d" % (state, placement["marker"], placement["top"]))

            if state == "painted":
                first = max(placement["first"], -top)
                last = min(placement["first"] + placement["rows"] - 1, lines - 1 - top)
                await draw(placement["marker"], top, placement["column"], first, last, contents)

        # 2. Boxes on the screen that show their markers: draw the image.
        now = time.monotonic()

        for box in boxes:
            marker, column, top, first, last = box
            found = overlays.get(marker)

            if found is None:
                continue

            columns, rows, _ = found
            last = min(last, rows - 1)
            first_row, last_row = top + first, top + last

            # Only rows that are on the screen: an image below the last row would scroll it.
            if first > last or first_row < 0 or last_row >= lines or column + columns > width:
                continue

            since = seen.setdefault(box, now)

            if now - since < STEADY_SECONDS:
                asyncio.get_running_loop().call_later(STEADY_SECONDS, changed.set)
                continue

            # Read the screen again just before the draw: the box must still be there.
            fresh = await session.async_get_screen_contents()

            if fresh.number_of_lines_above_screen != above or box not in set(spans(markers_in(fresh, width))):
                changed.set()
                continue

            await draw(marker, top, column, first, last, fresh)

        for box in list(seen):
            if box not in boxes:
                del seen[box]

    async def stream():
        async with session.get_screen_streamer(want_contents=False) as streamer:
            while True:
                await streamer.async_get()
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

    while not ending.done() and not tasks[0].done():
        waiting = asyncio.ensure_future(changed.wait())
        await asyncio.wait([waiting, ending, tasks[0]], return_when=asyncio.FIRST_COMPLETED)

        if not changed.is_set():
            waiting.cancel()
            continue

        changed.clear()
        await asyncio.sleep(SETTLE_SECONDS)

        try:
            await look()
        except Exception as problem:  # one bad frame must not end the overlay
            debug("error %s: %s" % (type(problem).__name__, problem))

    for task in tasks:
        task.cancel()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, help="ITERM_SESSION_ID, or the id after its colon")
    arguments = parser.parse_args()
    session_id = arguments.session.split(":")[-1]

    try:
        import iterm2
    except ImportError:
        emit({"ok": False, "error": "the iterm2 Python module is not installed: run `python3 -m pip install iterm2`"})
        sys.exit(2)

    try:
        iterm2.run_until_complete(lambda connection: watch(connection, session_id), retry=False)
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
