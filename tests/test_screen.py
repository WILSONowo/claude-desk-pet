import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pet'))

from screen import choose_z, is_fullscreen  # noqa: E402

MONITOR = (0, 0, 1920, 1080)


class FullscreenTest(unittest.TestCase):
    def test_window_covering_the_monitor_is_fullscreen(self):
        self.assertTrue(is_fullscreen((0, 0, 1920, 1080), 'Chrome_WidgetWin_1', MONITOR))

    def test_window_bigger_than_the_monitor_is_fullscreen(self):
        self.assertTrue(is_fullscreen((-8, -8, 1928, 1088), 'UnityWndClass', MONITOR))

    def test_window_leaving_the_taskbar_is_not(self):
        self.assertFalse(is_fullscreen((0, 0, 1920, 1040), 'Notepad', MONITOR))

    def test_maximized_window_is_not(self):
        self.assertFalse(is_fullscreen((0, 0, 1920, 1080), 'Notepad', MONITOR, zoomed=True))

    def test_desktop_is_not(self):
        for cls in ('Progman', 'WorkerW', 'Shell_TrayWnd'):
            self.assertFalse(is_fullscreen((0, 0, 1920, 1080), cls, MONITOR), cls)

    def test_fullscreen_on_another_monitor_does_not_count(self):
        self.assertFalse(is_fullscreen((1920, 0, 3840, 1080), 'Chrome_WidgetWin_1', MONITOR))


class ChooseZTest(unittest.TestCase):
    def test_sitting_on_a_window_goes_just_above_it(self):
        self.assertEqual(choose_z('perch', 1234, 0, False), 1234)

    def test_on_the_ground_stays_on_top(self):
        self.assertEqual(choose_z('perch', ('ground', 0, 0, None), 0, False), 'top')

    def test_in_the_air_or_not_perching_stays_on_top(self):
        for mode in ('hop', 'fall', 'held', None):
            self.assertEqual(choose_z(mode, 1234, 0, False), 'top', mode)

    def test_reminding_or_speaking_comes_to_the_front(self):
        self.assertEqual(choose_z('perch', 1234, 2, False), 'top')
        self.assertEqual(choose_z('perch', 1234, 0, True), 'top')


if __name__ == '__main__':
    unittest.main()
