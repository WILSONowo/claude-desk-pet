import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pet'))

from sessions import ACTIVE_S, BUSY_S, NOTIFY_S, Session, Summary, summarize  # noqa: E402

NOW = 10_000.0


def s(age, state='tool', project='a', text=''):
    return Session(mtime=NOW - age, state=state, text=text, project=project)


class SummarizeTest(unittest.TestCase):
    def test_nothing(self):
        self.assertEqual(summarize([], NOW), Summary(None, None))

    def test_one_session_has_no_second_line(self):
        a = s(5)
        self.assertEqual(summarize([a], NOW), Summary(a, None))

    def test_latest_wins_and_others_are_summarised(self):
        a, b = s(5, project='a'), s(60, state='tool', project='b')
        self.assertEqual(summarize([b, a], NOW), Summary(a, 'a · 另有 1 个在忙'))

    def test_finished_sessions_count_as_waiting_not_busy(self):
        a = s(5, project='a')
        self.assertEqual(summarize([a, s(60, state='celebrate', project='b')], NOW).sub, 'a · b 在等你')
        self.assertEqual(summarize([a, s(60, state='aborted', project='b')], NOW).sub, 'a · b 在等你')

    def test_waiting_beats_busy(self):
        a, b, c = s(5, project='a'), s(60, project='b'), s(90, state='waiting', project='c')
        self.assertEqual(summarize([a, b, c], NOW).sub, 'a · c 在等你')

    def test_several_waiting(self):
        a, b, c = s(5, project='a'), s(60, state='waiting', project='b'), s(90, state='waiting', project='c')
        self.assertEqual(summarize([a, b, c], NOW).sub, 'a · 2 个会话在等你')

    def test_error_beats_waiting(self):
        a, b, c = s(5), s(60, state='waiting', project='b'), s(90, state='error', project='c')
        self.assertEqual(summarize([a, b, c], NOW).sub, 'a · c 出错了')

    def test_stale_sessions_are_ignored(self):
        a, old = s(5), s(ACTIVE_S + 1, state='waiting', project='old')
        self.assertEqual(summarize([a, old], NOW).sub, None)

    def test_unnamed_project(self):
        a, b = s(5, project=''), s(60, state='waiting', project='')
        self.assertEqual(summarize([a, b], NOW).sub, '当前会话 · 另一个会话 在等你')


class MainSessionTest(unittest.TestCase):
    def test_a_finished_session_is_shown_first(self):
        done, busy = s(3, state='waiting', project='done'), s(60, state='tool', project='busy')
        self.assertEqual(summarize([busy, done], NOW), Summary(done, 'done · 另有 1 个在忙'))

    def test_after_a_moment_the_busy_session_takes_over(self):
        done, busy = s(NOTIFY_S + 1, state='celebrate', project='done'), s(60, state='tool', project='busy')
        self.assertEqual(summarize([busy, done], NOW), Summary(busy, 'busy · done 在等你'))

    def test_without_a_busy_session_your_turn_stays(self):
        done, other = s(NOTIFY_S + 1, state='waiting', project='done'), s(60, state='waiting', project='b')
        self.assertIs(summarize([done, other], NOW).main, done)

    def test_errors_keep_the_main_slot(self):
        bad, busy = s(NOTIFY_S + 1, state='error', project='bad'), s(60, state='tool', project='busy')
        self.assertIs(summarize([bad, busy], NOW).main, bad)

    def test_a_busy_session_that_went_quiet_does_not_take_over(self):
        done, stuck = s(NOTIFY_S + 1, state='waiting', project='done'), s(BUSY_S + 1, state='tool', project='stuck')
        self.assertIs(summarize([done, stuck], NOW).main, done)

    def test_the_most_recent_busy_session_wins(self):
        done = s(NOTIFY_S + 1, state='waiting', project='done')
        a, b = s(30, state='thinking', project='a'), s(90, state='reading', project='b')
        self.assertIs(summarize([done, b, a], NOW).main, a)


if __name__ == '__main__':
    unittest.main()
