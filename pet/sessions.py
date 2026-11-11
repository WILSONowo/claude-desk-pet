"""多个会话同时开着时，桌宠跟着哪一个、第二行小字怎么说别的会话。

跟哪个会话（按顺序）：
1. 刚有事要告诉你的会话（干完了、这一轮出错了、新会话打招呼）先插播 NOTIFY_S 秒：响提示音、敲门；
2. 否则跟你最近发过消息的那个会话（按"这一轮开始的时间"，不按谁刚写了文件，所以几个会话同时在忙时不会来回跳）；
3. 它已经干完了（在等你）而别的会话还在忙，就换成在忙的那个；它这一轮出错了就留着它，等你去看。

第二行小字："当前项目｜PicGO 在等你、2 个在忙"（竖线前是桌宠正在跟的会话）。出错只算"这一轮以出错结束"和石化；
一次工具调用失败（oops）很常见，Claude 会自己处理，不算出错。新会话打招呼（hello）不算"在等你"。"""
from __future__ import annotations

from dataclasses import dataclass

ACTIVE_S = 30 * 60.0   # 最近这么久有更新的会话才算"进行中"
NOTIFY_S = 10.0        # 有事要告诉你的会话插播这么久
BUSY_S = 10 * 60.0     # 忙着的会话这么久没更新，就当它卡住了或者被关掉了，不再跟它
BUSY = frozenset({'thinking', 'tool', 'reading', 'oops'})
DONE = frozenset({'waiting', 'celebrate', 'aborted'})   # 干完了/被打断了：都是在等你
FAILED = frozenset({'error', 'stone'})
ALERT = DONE | FAILED | {'hello'}
SOUNDS = ('done', 'error')


@dataclass(frozen=True)
class Session:
    mtime: float          # 状态文件最近一次写入的时间（秒）
    state: str
    text: str
    project: str
    sound: str = ''       # 这次写入带的提示音（done / error），没有就是空
    key: str = ''         # 会话 id（文件名）
    since: float = 0.0    # 当前状态从什么时候开始（秒）；旧版本没写就用 mtime
    turn: float = 0.0     # 这一轮（你最近一次发消息）开始的时间（秒）；旧版本没写就用 mtime
    helpers: int = 0      # 正在干活的小助手（子代理）个数

    @property
    def started(self) -> float:
        return self.since or self.mtime

    @property
    def asked(self) -> float:
        return self.turn or self.mtime


@dataclass(frozen=True)
class Summary:
    main: Session | None     # 桌宠跟着的会话，姿势和第一行跟着它
    sub: str | None          # 第二行小字；只有一个会话在进行时是 None


def busy(s: Session, now: float) -> bool:
    return s.state in BUSY and now - s.mtime <= BUSY_S


def pick_main(items: list, now: float) -> Session:
    live = [s for s in items if now - s.mtime <= ACTIVE_S] or [max(items, key=lambda s: s.mtime)]
    alerts = [s for s in live if s.state in ALERT and now - s.started <= NOTIFY_S]
    if alerts:
        return max(alerts, key=lambda s: s.started)
    focus = max(live, key=lambda s: s.asked)
    if busy(focus, now) or focus.state in FAILED:
        return focus
    working = [s for s in live if busy(s, now)]
    if working:
        return max(working, key=lambda s: s.asked)
    return max(live, key=lambda s: s.started)


def _part(group: list, verb: str) -> str | None:
    if not group:
        return None
    if len(group) == 1:
        return f'{group[0].project or "另一个会话"} {verb}'
    return f'{len(group)} 个{verb}'


def summarize(items: list, now: float) -> Summary:
    if not items:
        return Summary(None, None)
    main = pick_main(items, now)
    others = [s for s in items if s is not main and now - s.mtime <= ACTIVE_S and s.state != 'hello'
              and not (s.state in BUSY and not busy(s, now))]
    if now - main.mtime > ACTIVE_S or not others:
        return Summary(main, None)
    groups = [(g, verb) for g, verb in (
        ([s for s in others if s.state in FAILED], '出错了'),
        ([s for s in others if s.state in DONE], '在等你'),
        ([s for s in others if s.state in BUSY], '在忙'),
    ) if g]
    if not groups:
        return Summary(main, None)
    # 气泡很窄：最要紧的那类写名字（只有一个时），第二类只写个数，再往后的不写
    parts = [_part(*groups[0])] + [f'{len(g)} 个{verb}' for g, verb in groups[1:2]]
    return Summary(main, f'{main.project or "当前会话"}｜{"、".join(parts)}')


def new_sound(items: list, seen: dict | None) -> tuple:
    """哪个会话刚写了带提示音的状态（不管桌宠现在跟着谁）：返回 (要响的提示音或 None, 新的 seen)。
    seen 是上次看到的 {会话 id: 写入时间}；第一次传 None，只记下来不响（刚启动时不重播旧提示音）。
    同时有几个，出错的优先。"""
    now_seen = {s.key: s.mtime for s in items}
    if seen is None:
        return None, now_seen
    fresh = {s.sound for s in items if s.sound in SOUNDS and seen.get(s.key) != s.mtime}
    return ('error' if 'error' in fresh else 'done' if fresh else None), now_seen
