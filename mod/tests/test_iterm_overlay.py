"""Tests for bin/iterm_overlay.py, against a fake screen. No iTerm2 is needed.

Run with: python3 -m unittest discover -s mod/tests -p 'test_*.py'
"""

import importlib.util
import os
import sys
import unittest

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
        if 0 <= row < len(self.rows_) and 0 <= column < len(self.rows_[row]):
            return self.rows_[row][column]
        return None


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


if __name__ == "__main__":
    unittest.main()
