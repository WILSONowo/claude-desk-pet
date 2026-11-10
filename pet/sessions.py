"""多个会话同时开着时，桌宠跟着最新的那个，再用一行小字说说别的会话：有出错的先报出错，其次在等你的，最后在忙的。

例外：最新的会话只是"干完了、在等你"，而另一个会话还在忙的话，"轮到你啦"只占主位 NOTIFY_S 秒（提示音、敲门），
之后主位让给在忙的那个，干完的那个挪到第二行小字里（"xx 在等你"）。"""
from __future__ import annotations

from dataclasses import dataclass

ACTIVE_S = 30 * 60.0   # 最近这么久有更新的会话才算"进行中"
NOTIFY_S = 10.0        # 干完的会话先占主位这么久，再让给还在忙的会话
BUSY_S = 10 * 60.0     # 忙着的会话这么久没更新，就不抢主位了（可能卡住了或者被关掉了）
BUSY = frozenset({'thinking', 'tool', 'reading'})
DONE = frozenset({'waiting', 'celebrate', 'aborted'})  # 干完了/被打断了：都是在等你


@dataclass(frozen=True)
class Session:
    mtime: float
    state: str
    text: str
    project: str
    sound: str = ''   # 会话文件里带的提示音（done / error），没有就是空


@dataclass(frozen=True)
class Summary:
    main: Session | None     # 桌宠跟着的会话（通常是最新的那个），姿势和第一行跟着它
    sub: str | None          # 第二行小字；只有一个会话在进行时是 None


def summarize(items: list, now: float) -> Summary:
    if not items:
        return Summary(None, None)
    main = max(items, key=lambda s: s.mtime)
    if main.state in DONE and now - main.mtime > NOTIFY_S:
        busy = [s for s in items if s.state in BUSY and now - s.mtime <= BUSY_S]
        if busy:
            main = max(busy, key=lambda s: s.mtime)
    others = [s for s in items if s is not main and now - s.mtime <= ACTIVE_S]
    if now - main.mtime > ACTIVE_S or not others:
        return Summary(main, None)
    head = main.project or '当前会话'
    errors = [s for s in others if s.state in ('error', 'stone')]
    waiting = [s for s in others if s.state in DONE]
    if errors:
        tail = f'{errors[0].project or "另一个会话"} 出错了'
    elif len(waiting) == 1:
        tail = f'{waiting[0].project or "另一个会话"} 在等你'
    elif waiting:
        tail = f'{len(waiting)} 个会话在等你'
    else:
        tail = f'另有 {len(others)} 个在忙'
    return Summary(main, f'{head} · {tail}')
