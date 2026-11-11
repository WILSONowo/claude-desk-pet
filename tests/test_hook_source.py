"""引擎加载 hooks/register.ts 时有些写法会被拒绝（整个模块不加载：没有状态栏，桌宠也收不到状态）。
tsc 查不出来，这里把踩过的坑挡住；发版前还要跑一遍 `claude plugin validate .`。"""
import os
import re
import unittest

SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'hooks', 'register.ts')


class HookSourceTest(unittest.TestCase):
    def test_dollar_is_never_passed_with_a_spread_argument(self):
        with open(SOURCE, encoding='utf-8') as f:
            src = f.read()
        self.assertIsNone(re.search(r'\(\s*\$\s*,[^)]*\.\.\.', src), '$ 和 ...展开 不能出现在同一个调用里')


if __name__ == '__main__':
    unittest.main()
