import unittest

from ui.library_view import calculate_grid_layout


class LibraryViewLayoutTests(unittest.TestCase):
    def test_grid_adapts_card_width_to_fill_available_canvas(self):
        columns, card_width = calculate_grid_layout(1000)

        self.assertEqual(columns, 6)
        used_width = 2 * 20 + columns * card_width + (columns - 1) * 16
        self.assertLessEqual(used_width, 1000)
        self.assertLessEqual(1000 - used_width, columns)

    def test_grid_uses_fewer_wider_cards_on_a_narrower_canvas(self):
        columns, card_width = calculate_grid_layout(900)

        self.assertEqual(columns, 5)
        self.assertGreater(card_width, 146)


if __name__ == "__main__":
    unittest.main()
