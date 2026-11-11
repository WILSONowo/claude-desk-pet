import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pet'))

from screen import choose_z, is_fullscreen, place_bubble, visible_span  # noqa: E402

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


class PlaceBubbleTest(unittest.TestCase):
    def test_centered_when_there_is_room(self):
        self.assertEqual(place_bubble(100, 200, 0, 400, 20), 150)

    def test_moves_in_from_the_right_edge(self):
        self.assertEqual(place_bubble(100, 200, 0, 230, 20), 130)

    def test_moves_in_from_the_left_edge(self):
        self.assertEqual(place_bubble(100, 200, 170, 400, 20), 170)

    def test_the_tail_stays_under_the_bubble(self):
        # 角色贴着屏幕右边：气泡往里挪也得盖住尾巴
        self.assertEqual(place_bubble(100, 200, 0, 190, 20), 120)

    def test_wider_than_the_screen_keeps_the_left_side(self):
        self.assertEqual(place_bubble(300, 200, 100, 300, 20), 100)


class VisibleSpanTest(unittest.TestCase):
    def test_fully_on_screen(self):
        self.assertEqual(visible_span(100, 400, (0, 0, 1920, 1080)), (0, 400))

    def test_hanging_off_the_right(self):
        self.assertEqual(visible_span(1700, 400, (0, 0, 1920, 1080)), (0, 216))

    def test_hanging_off_the_left_rounds_inward(self):
        self.assertEqual(visible_span(-30, 400, (0, 0, 1920, 1080)), (32, 400))
