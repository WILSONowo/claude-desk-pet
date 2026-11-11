import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pet'))

from sessions import ACTIVE_S, BUSY_S, NOTIFY_S, Session, Summary, new_sound, summarize  # noqa: E402

NOW = 10_000.0


def s(age, state='tool', project='a', text='', since=None, turn=None, key=None, sound=''):
    """age：多久前写的文件；since：状态开始于多久前（默认很久以前，不算插播）；turn：这一轮开始于多久前。"""
    return Session(mtime=NOW - age, state=state, text=text, project=project, sound=sound, key=key or project,
                   since=NOW - (since if since is not None else 999), turn=NOW - (turn if turn is not None else 999))


class SummarizeTest(unittest.TestCase):
    def test_nothing(self):
        self.assertEqual(summarize([], NOW), Summary(None, None))

    def test_one_session_has_no_second_line(self):
        a = s(5)
        self.assertEqual(summarize([a], NOW), Summary(a, None))

    def test_others_are_summarised_after_a_separator(self):
        a, b = s(5, project='a', turn=10), s(60, project='b', turn=100)
        self.assertEqual(summarize([b, a], NOW), Summary(a, 'a｜b 在忙'))

    def test_finished_sessions_count_as_waiting_not_busy(self):
        a = s(5, project='a', turn=10)
        self.assertEqual(summarize([a, s(60, state='celebrate', project='b')], NOW).sub, 'a｜b 在等你')
        self.assertEqual(summarize([a, s(60, state='aborted', project='b')], NOW).sub, 'a｜b 在等你')

    def test_groups_are_listed_by_importance(self):
        a = s(5, project='a', turn=10)
        b, c, d = s(60, project='b'), s(90, state='waiting', project='c'), s(95, state='waiting', project='d')
        self.assertEqual(summarize([a, b, c, d], NOW).sub, 'a｜2 个在等你、b 在忙')

    def test_at_most_two_groups(self):
        a = s(5, project='a', turn=10)
        items = [a, s(60, state='error', project='e'), s(60, state='waiting', project='w'), s(60, project='b')]
        self.assertEqual(summarize(items, NOW).sub, 'a｜e 出错了、w 在等你')

    def test_a_single_failed_tool_call_is_not_an_error(self):
        a, b = s(5, project='a', turn=10), s(14, state='oops', project='PicGO')
        self.assertEqual(summarize([a, b], NOW).sub, 'a｜PicGO 在忙')

    def test_a_new_session_saying_hello_is_not_waiting(self):
        a, b = s(5, project='a', turn=10), s(60, state='hello', project='new')
        self.assertEqual(summarize([a, b], NOW).sub, None)

    def test_stale_sessions_are_ignored(self):
        a, old = s(5), s(ACTIVE_S + 1, state='waiting', project='old')
        self.assertEqual(summarize([a, old], NOW).sub, None)

    def test_quiet_busy_sessions_are_not_called_busy(self):
        a, stuck = s(5, turn=10), s(BUSY_S + 1, state='tool', project='stuck')
        self.assertEqual(summarize([a, stuck], NOW).sub, None)

    def test_unnamed_project(self):
        a, b = s(5, project='', turn=10, key='x'), s(60, state='waiting', project='', key='y')
        self.assertEqual(summarize([a, b], NOW).sub, '当前会话｜另一个会话 在等你')


class MainSessionTest(unittest.TestCase):
    def test_follows_the_session_you_last_talked_to_not_the_last_write(self):
        # b 刚写了文件，但你最近是在 a 里发的消息：几个会话都在忙时桌宠不会跟着来回跳
        a, b = s(20, project='a', turn=30), s(1, project='b', turn=300)
        self.assertIs(summarize([a, b], NOW).main, a)

    def test_a_new_message_moves_the_focus(self):
        a, b = s(20, project='a', turn=300), s(1, project='b', turn=2)
        self.assertIs(summarize([a, b], NOW).main, b)

    def test_a_finished_session_cuts_in_for_a_moment(self):
        busy, done = s(5, project='busy', turn=30), s(3, state='waiting', project='done', since=3, turn=200)
        self.assertEqual(summarize([busy, done], NOW), Summary(done, 'done｜busy 在忙'))

    def test_then_goes_back_to_the_busy_one(self):
        busy = s(5, project='busy', turn=300)
        done = s(NOTIFY_S + 1, state='celebrate', project='done', since=NOTIFY_S + 1, turn=200)
        self.assertEqual(summarize([busy, done], NOW), Summary(busy, 'busy｜done 在等你'))

    def test_rewriting_a_finished_session_does_not_cut_in_again(self):
        # 后台小助手收工时会重写文件（文字变了，状态没变）：since 还是早先的，不再插播
        busy = s(5, project='busy', turn=300)
        done = s(1, state='waiting', project='done', since=120, turn=200)
        self.assertIs(summarize([busy, done], NOW).main, busy)

    def test_your_session_finished_and_another_is_busy(self):
        done = s(NOTIFY_S + 1, state='waiting', project='done', since=NOTIFY_S + 1, turn=60)
        busy = s(5, project='busy', turn=300)
        self.assertIs(summarize([busy, done], NOW).main, busy)

    def test_without_a_busy_session_your_turn_stays(self):
        done = s(NOTIFY_S + 1, state='waiting', project='done', since=NOTIFY_S + 1, turn=60)
        other = s(60, state='waiting', project='b', since=200, turn=300)
        self.assertIs(summarize([done, other], NOW).main, done)

    def test_a_failed_turn_keeps_the_main_slot(self):
        bad = s(NOTIFY_S + 1, state='error', project='bad', since=NOTIFY_S + 1, turn=60)
        busy = s(5, project='busy', turn=300)
        self.assertIs(summarize([bad, busy], NOW).main, bad)

    def test_a_busy_session_that_went_quiet_does_not_take_over(self):
        done = s(NOTIFY_S + 1, state='waiting', project='done', since=NOTIFY_S + 1, turn=60)
        stuck = s(BUSY_S + 1, state='tool', project='stuck', turn=BUSY_S + 100)
        self.assertIs(summarize([done, stuck], NOW).main, done)

    def test_a_new_session_says_hello_briefly(self):
        busy, new = s(5, project='busy', turn=30), s(2, state='hello', project='new', since=2, turn=None)
        self.assertIs(summarize([busy, new], NOW).main, new)
        later = s(NOTIFY_S + 5, state='hello', project='new', since=NOTIFY_S + 5)
        self.assertIs(summarize([busy, later], NOW).main, busy)

    def test_old_files_without_times_fall_back_to_mtime(self):
        a = Session(mtime=NOW - 5, state='tool', text='', project='a')
        b = Session(mtime=NOW - 60, state='tool', text='', project='b')
        self.assertIs(summarize([a, b], NOW).main, a)


class NewSoundTest(unittest.TestCase):
    def test_nothing_plays_on_start(self):
        sound, seen = new_sound([s(1, state='waiting', sound='done')], None)
        self.assertIsNone(sound)
        self.assertEqual(seen, {'a': NOW - 1})

    def test_a_new_write_with_a_sound_plays_whichever_session_it_is(self):
        old, seen = s(60, project='a'), None
        _, seen = new_sound([old], seen)
        sound, _ = new_sound([old, s(1, state='waiting', project='b', sound='done')], seen)
        self.assertEqual(sound, 'done')

    def test_the_same_write_plays_once(self):
        a = s(1, state='waiting', sound='done')
        _, seen = new_sound([s(60)], None)
        sound, seen = new_sound([a], seen)
        self.assertEqual(sound, 'done')
        self.assertIsNone(new_sound([a], seen)[0])

    def test_error_wins(self):
        _, seen = new_sound([], None)
        items = [s(1, state='waiting', project='a', sound='done'), s(1, state='error', project='b', sound='error')]
        self.assertEqual(new_sound(items, seen)[0], 'error')


if __name__ == '__main__':
    unittest.main()
