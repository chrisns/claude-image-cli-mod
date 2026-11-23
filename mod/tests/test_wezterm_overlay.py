import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))

import iterm_overlay  # noqa: E402
import wezterm_overlay  # noqa: E402


class Cells(unittest.TestCase):
    def test_wide_characters_take_two_cells(self):
        self.assertEqual(wezterm_overlay.split_cells("a一b"), ["a", "一", "", "b"])

    def test_combining_marks_join_the_cell_before(self):
        self.assertEqual(wezterm_overlay.split_cells("éx"), ["é", "x"])


class Contents(unittest.TestCase):
    def test_blank_cells_where_an_image_was_drawn_are_image_cells(self):
        contents = wezterm_overlay.Contents("⣿⣿   \nabc\n", 3, [(0, 2, 2, 2)])
        line = contents.line(0)

        self.assertEqual(contents.number_of_lines, 3)
        self.assertFalse(iterm_overlay.is_image(line, 1))  # outside the rectangle
        self.assertTrue(iterm_overlay.is_image(line, 2))
        self.assertTrue(iterm_overlay.is_image(line, 3))
        # Painted again by the program: text, so no image.
        self.assertFalse(iterm_overlay.is_image(contents.line(1), 2))
        # Past the end of a trimmed line, in the rectangle: blank, so image.
        self.assertTrue(iterm_overlay.is_image(wezterm_overlay.Contents("", 3, [(2, 0, 1, 4)]).line(2), 3))


class Sequence(unittest.TestCase):
    def test_erases_then_draws_one_file_sequence_and_restores_the_cursor(self):
        data = wezterm_overlay.sequence(4, 2, 3, 2, b"PNG").decode()

        self.assertTrue(data.startswith("\x1b7\x1b[0m"))
        self.assertIn("\x1b[5;3H\x1b[3X\x1b[6;3H\x1b[3X", data)
        self.assertIn("\x1b]1337;File=inline=1;size=3;width=3;height=2;", data)
        self.assertNotIn("MultipartFile", data)
        self.assertTrue(data.endswith("\x07\x1b8"))


class Watcher(unittest.TestCase):
    def test_a_draw_remembers_its_rectangle_and_replaces_an_older_one(self):
        class Fake:
            drawn = [(0, 0, 2, 2), (10, 0, 1, 1)]

        watcher = wezterm_overlay.WeztermWatcher(Fake(), iterm_overlay.Overlays(), lambda delay=0: None)
        watcher.encode(1, 1, 3, 3, b"x")

        self.assertEqual(Fake.drawn, [(10, 0, 1, 1), (1, 1, 3, 3)])


class Inbox(unittest.TestCase):
    def test_made_private_and_refused_when_others_can_use_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "inbox")
            wezterm_overlay.make_inbox(path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
            os.chmod(path, 0o755)

            with self.assertRaises(RuntimeError):
                wezterm_overlay.make_inbox(path)

    def test_alive_only_with_a_fresh_heartbeat(self):
        with tempfile.TemporaryDirectory() as path:
            self.assertFalse(wezterm_overlay.is_alive(path))
            open(os.path.join(path, "alive"), "w").close()
            self.assertTrue(wezterm_overlay.is_alive(path))
            self.assertFalse(wezterm_overlay.is_alive(path, now=time.time() + 60))

    def test_a_post_is_one_whole_file_named_for_the_pane_in_order(self):
        with tempfile.TemporaryDirectory() as path:
            wezterm_overlay.post(path, "7", 2, b"two")
            wezterm_overlay.post(path, "7", 10, b"ten")
            names = sorted(name for name in os.listdir(path))

            self.assertEqual(names, ["7-0000000000000002.osc", "7-0000000000000010.osc"])
            self.assertEqual(open(os.path.join(path, names[1]), "rb").read(), b"ten")


if __name__ == "__main__":
    unittest.main()
