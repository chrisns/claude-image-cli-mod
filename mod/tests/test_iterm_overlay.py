"""Tests for bin/iterm_overlay.py, against a fake screen. No iTerm2 is needed.

Run with: python3 -m unittest discover -s mod/tests -p 'test_*.py'
"""

import asyncio
import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BIN = os.path.join(os.path.dirname(__file__), "..", "bin")
sys.path.insert(0, BIN)

import render  # noqa: E402

spec = importlib.util.spec_from_file_location("iterm_overlay", os.path.join(BIN, "iterm_overlay.py"))
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


class Kind:
    def __init__(self, name):
        self.name = name


class Style:
    def __init__(self, image):
        self.image = Kind("ITERM2" if image else "NONE")


class Line:
    """One screen row: a list of cells, each a glyph, or None for an image cell."""

    def __init__(self, cells):
        self.cells = cells
        self.string = "".join(cell or "" for cell in cells)

    def string_at(self, x):
        if x >= len(self.cells):
            raise IndexError(x)
        return self.cells[x] or ""

    def style_at(self, x):
        if x >= len(self.cells):
            raise IndexError(x)
        return Style(self.cells[x] is None)


class Screen:
    def __init__(self, rows, width=40):
        self.rows = [Line(list(row.ljust(width))) if isinstance(row, str) else Line(row) for row in rows]
        self.number_of_lines = len(self.rows)

    def line(self, row):
        return self.rows[row]


MARKER = bytes([0x15, 0x82, 0xEE])


def box_row(row, width=8, left=2, fill="█"):
    """The cells of one row of a box: margin, marker, then picture glyphs."""
    marker = [chr(code) for code in render.marker_glyphs(MARKER, row)]
    return [" "] * left + marker + [fill] * (width - len(marker)) + [" "] * 10


class Overlays:
    """What Overlays knows about the one box in these tests: 8 columns, 4 rows."""

    def __init__(self, rows=4, width=8, fill="█"):
        self.loaded = {MARKER.hex(): (width, rows, b"png")}
        self.rows_ = [box_row(row, width, 0, fill)[:width] for row in range(rows)]

    def glyph(self, marker, row, column):
        if marker in self.loaded and 0 <= row < len(self.rows_) and 0 <= column < len(self.rows_[row]):
            return self.rows_[row][column]
        return None

    def get(self, marker):
        return self.loaded.get(marker)

    def rows(self, marker, first, last):
        return b"png %s %d-%d" % (marker.encode(), first, last)


class MarkerTests(unittest.TestCase):
    def test_each_row_reads_back(self):
        for row in range(256):
            glyphs = [chr(code) for code in render.marker_glyphs(MARKER, row)]
            self.assertEqual(overlay.read_marker(glyphs), (MARKER.hex(), row))

    def test_no_marker_cell_repeats_on_another_row(self):
        # When a box moves, every marker cell must change, or the terminal keeps an old image over it.
        for row in range(64):
            for other in range(row + 1, 64):
                pairs = zip(render.marker_glyphs(MARKER, row), render.marker_glyphs(MARKER, other))
                self.assertTrue(all(a != b for a, b in pairs), (row, other))

    def test_braille_text_is_not_a_marker(self):
        self.assertIsNone(overlay.read_marker(list("⠁⠂⠃⠄⠅")))
        self.assertIsNone(overlay.read_marker(list("hello")))
        self.assertIsNone(overlay.read_marker(list("⠁⠂")))

    def test_markers_and_spans_find_a_box_cut_by_the_top_of_the_screen(self):
        # Rows 2 and 3 of the box are on screen rows 0 and 1: row 0 of the box is two rows above the screen.
        screen = Screen([box_row(2), box_row(3), list("prompt".ljust(30))])
        found = list(overlay.markers_in(screen, 30))
        self.assertEqual(found, [(0, 2, MARKER.hex(), 2), (1, 2, MARKER.hex(), 3)])
        self.assertEqual(list(overlay.spans(found)), [(MARKER.hex(), 2, -2, 2, 3)])

    def test_a_trimmed_line_is_not_an_error(self):
        screen = Screen([["a"], box_row(0)])
        self.assertEqual(len(list(overlay.markers_in(screen, 30))), 1)


class DamageTests(unittest.TestCase):
    def placement(self):
        return {"marker": MARKER.hex(), "first": 0, "top": 0, "column": 2, "rows": 2, "columns": 8}

    def image_row(self):
        return [" ", " "] + [None] * 8 + [" "] * 10

    def test_intact(self):
        screen = Screen([self.image_row(), self.image_row()])
        self.assertEqual(overlay.damage(self.placement(), screen, Overlays()), "intact")

    def test_painted_again_in_place(self):
        screen = Screen([box_row(0), self.image_row()])
        self.assertEqual(overlay.damage(self.placement(), screen, Overlays()), "painted")

    def test_covered_by_a_hint(self):
        row = self.image_row()
        row[6:9] = list("fn+")
        screen = Screen([self.image_row(), row])
        self.assertEqual(overlay.damage(self.placement(), screen, Overlays()), "covered")

    def test_gone_when_nothing_of_the_box_is_left(self):
        screen = Screen(["  some other text here", "  and more"])
        self.assertEqual(overlay.damage(self.placement(), screen, Overlays()), "gone")

    def test_blank_matches_alone_prove_nothing(self):
        overlays = Overlays(fill=" ")
        row = self.image_row()
        row[8:10] = [" ", " "]
        screen = Screen([self.image_row(), row])
        self.assertEqual(overlay.damage(self.placement(), screen, overlays), "covered")


class CleanRunsTests(unittest.TestCase):
    def test_rows_with_other_content_are_left_out(self):
        hinted = box_row(2)
        hinted[7:10] = list("fn+")
        screen = Screen([box_row(0), box_row(1), hinted, box_row(3)])
        runs = overlay.clean_runs(MARKER.hex(), 0, 2, 0, 3, screen, Overlays())
        self.assertEqual(runs, [(0, 1), (3, 3)])

    def test_image_cells_count_as_clean(self):
        imaged = [" ", " "] + [None] * 8 + [" "] * 10
        screen = Screen([box_row(0), imaged])
        self.assertEqual(overlay.clean_runs(MARKER.hex(), 0, 2, 0, 1, screen, Overlays()), [(0, 1)])


class SequenceTests(unittest.TestCase):
    def test_a_large_image_goes_in_chunks_in_one_piece(self):
        data = bytes(range(256)) * 1000
        out = overlay.sequence(4, 2, 10, 5, data).decode()
        self.assertTrue(out.startswith("\x1b7\x1b[5;3H\x1b]1337;MultipartFile=inline=1;size=256000;width=10;height=5;"))
        self.assertIn("preserveAspectRatio=0;doNotMoveCursor=1\x07", out)
        self.assertEqual(out.count("FilePart="), 6)
        self.assertTrue(out.endswith("\x1b]1337;FileEnd\x07\x1b8"))


OTHER = bytes([0x40, 0x07, 0x99])


def other_row(row, width=8, left=20, fill="▓"):
    """A row of a second box, further right, with a marker of its own."""
    marker = [chr(code) for code in overlay.render.marker_glyphs(OTHER, row)]
    return [" "] * left + marker + [fill] * (width - len(marker))


class TwoBoxes(Overlays):
    """Two boxes; the first one's PNG cannot be cut."""

    def __init__(self):
        super().__init__(rows=2)
        self.loaded[OTHER.hex()] = (8, 2, b"other")
        self.other = [other_row(row, left=0)[:8] for row in range(2)]

    def glyph(self, marker, row, column):
        if marker == OTHER.hex():
            return self.other[row][column] if 0 <= row < 2 and 0 <= column < 8 else None
        return super().glyph(marker, row, column)

    def rows(self, marker, first, last):
        if marker == MARKER.hex():
            raise OSError("the PNG is damaged")
        return super().rows(marker, first, last)


class Session:
    """The API's Session: hands out the screens in turn (the last one again and again)."""

    def __init__(self, screens, width=40):
        self.screens = list(screens)
        self.reads = 0
        self.injected = []
        self.grid_size = SimpleNamespace(width=width)

    async def async_get_screen_contents(self):
        screen = self.screens[min(self.reads, len(self.screens) - 1)]
        self.reads += 1
        return screen

    async def async_inject(self, data):
        self.injected.append(data)


def joined(*parts):
    """One screen row made of several lists of cells, padded to 40."""
    row = []

    for part in parts:
        row = row + part

    return row + [" "] * (40 - len(row))


class TimingTests(unittest.TestCase):
    def watcher(self):
        return overlay.Watcher(None, None, lambda delay=0: None)

    def test_a_quiet_screen_draws_at_once(self):
        watcher = self.watcher()
        watcher.changed_at = 100.0
        self.assertEqual(watcher.wait_needed(now=100.0 + overlay.QUIET_SECONDS + 0.001, since=100.0), 0)

    def test_a_busy_screen_waits_for_the_box_to_stand_still(self):
        watcher = self.watcher()
        watcher.changed_at = 100.0
        self.assertGreater(watcher.wait_needed(now=100.1, since=100.0), 0)
        self.assertEqual(watcher.wait_needed(now=100.1, since=100.1 - overlay.BUSY_STEADY_SECONDS), 0)

    def test_a_busy_screen_draws_nothing_on_the_first_look(self):
        rows = [box_row(row) for row in range(2)]
        session = Session([Screen(rows)])
        woken = []
        watcher = overlay.Watcher(session, TwoBoxes(), lambda delay=0: woken.append(delay))
        watcher.screen_changed()
        asyncio.run(watcher.look())
        self.assertEqual(session.injected, [])
        self.assertTrue(woken and woken[0] > 0)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        # The screen counts as quiet at once, so a box is drawn on the first look.
        patcher = mock.patch.object(overlay, "QUIET_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def look(self, watcher):
        asyncio.run(watcher.look())

    def test_one_failing_box_does_not_stop_the_others(self):
        rows = [joined(box_row(row), other_row(row, left=10)) for row in range(2)]
        session = Session([Screen(rows)])
        watcher = overlay.Watcher(session, TwoBoxes(), lambda delay=0: None)
        self.look(watcher)
        self.assertEqual(len(session.injected), 1)
        self.assertIn(OTHER.hex().encode(), base64.b64decode(session.injected[0].split(b"FilePart=")[1].split(b"\x07")[0]))

    def test_a_failing_check_does_not_stop_the_others(self):
        class Broken(TwoBoxes):
            def glyph(self, marker, row, column):
                if marker == MARKER.hex():
                    raise ValueError("a damaged record")
                return super().glyph(marker, row, column)

            def rows(self, marker, first, last):
                return Overlays.rows(self, marker, first, last)

        image = [" ", " "] + [None] * 8
        rows = [joined(image, other_row(row, left=10)) for row in range(2)]
        session = Session([Screen(rows)])
        watcher = overlay.Watcher(session, Broken(), lambda delay=0: None)
        watcher.placements.append({"marker": MARKER.hex(), "first": 0, "top": 0, "column": 2, "rows": 2, "columns": 8})
        # The image of the first box is painted over, so its check reads the glyphs, and fails.
        session.screens = [Screen([joined(box_row(0), other_row(0, left=10)), joined(box_row(1), other_row(1, left=10))])]
        session.screens[0].rows[0].cells[12] = "x"  # not a marker any more: no new draw of the first box
        self.look(watcher)
        self.assertEqual([data.count(b"FileEnd") for data in session.injected], [1])
        self.assertEqual(watcher.placements[-1]["marker"], OTHER.hex())

    def painted(self):
        """A box drawn on rows 0-1, then painted over in place with its glyphs."""
        image = [" ", " "] + [None] * 8
        drawn = Screen([image, image])
        painted = [box_row(0), box_row(1)]
        # A cell in the marker is overwritten, so step 2 does not find the box: only step 1 can draw it.
        painted[0][2] = "x"
        painted[1][2] = "x"
        overlays = Overlays(rows=2)
        overlays.rows_[0][0] = "x"
        overlays.rows_[1][0] = "x"
        placement = {"marker": MARKER.hex(), "first": 0, "top": 0, "column": 2, "rows": 2, "columns": 8}
        return drawn, Screen(painted), overlays, placement

    def test_painted_again_is_read_again_before_the_draw(self):
        drawn, painted, overlays, placement = self.painted()
        session = Session([painted, painted])
        woken = []
        watcher = overlay.Watcher(session, overlays, woken.append)
        watcher.placements.append(dict(placement))
        self.look(watcher)
        self.assertEqual(session.reads, 2)  # the look, and the read just before the draw
        self.assertEqual(len(session.injected), 1)

    def test_painted_but_moved_meanwhile_is_not_drawn(self):
        drawn, painted, overlays, placement = self.painted()
        elsewhere = Screen(["  some other text", "  and a prompt"])
        session = Session([painted, elsewhere])
        woken = []
        watcher = overlay.Watcher(session, overlays, woken.append)
        watcher.placements.append(dict(placement))
        self.look(watcher)
        self.assertEqual(session.reads, 2)
        self.assertEqual(session.injected, [])
        self.assertEqual(woken, [0])  # another look decides what it is now

    def test_an_unchanged_placement_is_not_checked_again(self):
        image = [" ", " "] + [None] * 8
        screen = Screen([image, image])
        overlays = Overlays(rows=2)
        watcher = overlay.Watcher(Session([screen]), overlays, lambda delay=0: None)
        watcher.placements.append({"marker": MARKER.hex(), "first": 0, "top": 0, "column": 2, "rows": 2, "columns": 8})
        self.look(watcher)

        with mock.patch.object(overlay, "damage", side_effect=AssertionError("checked again")):
            self.look(watcher)

        self.assertEqual(len(watcher.placements), 1)


class SnapshotTests(unittest.TestCase):
    def test_each_line_is_read_once(self):
        screen = Screen([box_row(0), list("text".ljust(30))])
        calls = []
        original = screen.line
        screen.line = lambda row: calls.append(row) or original(row)
        snapshot = overlay.Snapshot(screen)
        list(overlay.markers_in(snapshot, 30))
        overlay.damage({"marker": MARKER.hex(), "first": 0, "top": 0, "column": 2, "rows": 2, "columns": 8}, snapshot, Overlays())
        self.assertEqual(sorted(calls), [0, 1])


class RecordTests(unittest.TestCase):
    """The real Overlays, against records render.py writes."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patcher = mock.patch.dict(os.environ, {"INLINE_IMAGES_CACHE": self.folder.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, *args, stdin=None):
        result = subprocess.run(
            [sys.executable, os.path.join(BIN, "render.py"), *args], input=stdin, capture_output=True, text=True, env=os.environ
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_daemon_finds_what_render_registers(self):
        data = base64.b64encode(render.png_bytes(4, 4, bytes((9, 9, 9, 255)) * 16)).decode()
        stored = self.render("inspect", stdin=data)
        out = self.render("cells", stored["path"], "--columns", "8", "--rows", "4", "--marker")
        self.assertTrue(out["ok"], out)
        self.assertEqual(overlay.overlay_dir(), os.path.join(render.cache_dir(), "overlays"))
        found = overlay.Overlays().get(out["marker"])
        self.assertIsNotNone(found)
        self.assertEqual(found[:2], (8, 4))

    def test_a_missing_record_is_not_looked_for_again_at_once(self):
        overlays = overlay.Overlays()

        with mock.patch.object(overlay, "overlay_dir", wraps=overlay.overlay_dir) as looked:
            self.assertIsNone(overlays.get("abcdef"))
            self.assertIsNone(overlays.get("abcdef"))
            self.assertEqual(looked.call_count, 1)

    @unittest.skipIf(overlay.Image is None, "Pillow is not installed")
    def test_records_and_cut_rows_are_bounded(self):
        overlays = overlay.Overlays()
        picture = io.BytesIO()
        overlay.Image.new("RGBA", (8, 40), (1, 2, 3, 255)).save(picture, "PNG")

        for index in range(overlay.MAX_RECORDS + 5):
            overlays.loaded[str(index)] = (8, 4, picture.getvalue())
            overlays.glyphs[str(index)] = ""

            while len(overlays.loaded) > overlay.MAX_RECORDS:
                overlays.forget(next(iter(overlays.loaded)))

        self.assertEqual(len(overlays.loaded), overlay.MAX_RECORDS)

        with mock.patch.object(overlay, "MAX_CROP_BYTES", 1):
            first = overlays.rows("10", 0, 1)
            overlays.rows("10", 1, 2)
            overlays.rows("10", 2, 3)

        self.assertEqual(len(overlays.crops), 1)
        self.assertLessEqual(len(overlays.pictures), overlay.MAX_PICTURES)

        with overlay.Image.open(io.BytesIO(first)) as cut:
            self.assertEqual(cut.size, (8, 20))


if __name__ == "__main__":
    unittest.main()
