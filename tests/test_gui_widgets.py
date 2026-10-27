"""
Tests for the pure helper functions of the GUI (no window is opened).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat import config
from netchat.gui_widgets import AVATAR_COLORS, DARK, LIGHT, avatar_color, validate_name


class NameValidationTests(unittest.TestCase):
    def test_good_names(self):
        self.assertEqual(validate_name("Rahul", config.MAX_NAME_LEN), "")
        self.assertEqual(validate_name("  Asha  ", config.MAX_NAME_LEN), "")      # spaces are trimmed
        self.assertEqual(validate_name("x" * config.MAX_NAME_LEN, config.MAX_NAME_LEN), "")

    def test_empty_name(self):
        self.assertNotEqual(validate_name("", config.MAX_NAME_LEN), "")
        self.assertNotEqual(validate_name("   ", config.MAX_NAME_LEN), "")

    def test_pipe_is_rejected(self):
        self.assertNotEqual(validate_name("a|b", config.MAX_NAME_LEN), "")

    def test_too_long(self):
        self.assertNotEqual(validate_name("x" * (config.MAX_NAME_LEN + 1), config.MAX_NAME_LEN), "")


class AvatarColorTests(unittest.TestCase):
    def test_same_name_same_color(self):
        self.assertEqual(avatar_color("Rahul"), avatar_color("Rahul"))
        self.assertIn(avatar_color("Rahul"), AVATAR_COLORS)

    def test_names_get_a_spread_of_colors(self):
        colors = {avatar_color(n) for n in ["Asha", "Rahul", "Meera", "Karan", "Priya", "Dev"]}
        self.assertGreater(len(colors), 2)


class ThemeTests(unittest.TestCase):
    def test_both_themes_define_the_same_colours(self):
        # Switching theme must never hit a missing key.
        self.assertEqual(set(LIGHT), set(DARK))

    def test_palette_from_the_design(self):
        self.assertEqual(LIGHT["header_bg"], "#14213D")
        self.assertEqual(LIGHT["accent"], "#1AA7A1")
        self.assertEqual(LIGHT["chat_bg"], "#F4F6F8")


if __name__ == "__main__":
    unittest.main()
