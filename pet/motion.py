"""桌宠的"身体"：坐在哪、怎么跳、会不会掉下去、该摆什么姿势。

参考 Shimeji 这类桌宠的做法（改编自 iRyougi/claude-desk-pet，去掉了走路）：角色有位置，脚下有"地板"
（任何窗口的顶边，或者屏幕底）就坐着，没有就下落；被拿起来就跟着光标走，松手后自由落体，落到下面第一块地板就停住，
不会自己"回家"。窗口只是地板：她坐在哪个窗口上，窗口动她就跟着动，窗口没了她就掉下去。
她不会自己走来走去；换窗口时是跳过去的。

不碰界面和系统接口：pet.pyw 每帧告诉它"现在有哪些地板"，它返回新的位置和模式。
所有坐标是屏幕像素，y 向下，(x, y) 是她"脚底/屁股"落地点。
"""
from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace

SWING_FOR = (3.0, 6.0)        # 一次晃腿持续多久
SWING_EVERY = (20.0, 45.0)    # 两次晃腿之间隔多久。坐着大部分时间是静止的，晃腿只是偶尔抽中的小行为
SWING_S = 0.7                 # 晃腿时每一帧停多久
LAND_POSE_S = 0.3             # 落地后摆多久的落地姿势
NOTIFY_S = 8.0                # Claude 刚回复完，她先敲门（干了大活就撒花）这么久，再安静下来
GRAVITY = 2600.0              # 像素/秒²（100% 缩放）
TERMINAL = 2600.0             # 最大下落速度，像素/秒
AIR_X = 1.6                   # 被甩出去时水平速度的衰减，1/秒
LAND_TOL = 8.0                # 下落时，地板在这么近的范围内也算接住（窗口可能刚好动了一下）
TRANSITION_S = 0.24           # 坐下/站起的过渡一共多久（两帧各一半）
SLEEP_S = 1.2                 # 打瞌睡两帧交替的间隔
HOP_FRAC = (0.1, 0.9)         # 跳去别的窗口时，落在窗口顶边的哪一段（比例）

# Claude 的状态 → 立绘（不坐窗口时，以及"站起来"的事件姿势）
FIXED_SPRITES = {
    'thinking': 'smile-closed',
    'tool': 'typing',
    'reading': 'notes',
    'waiting': 'knock',
    'celebrate': 'confetti',
    'idle': 'smile',
    'error': 'panic',
    'aborted': 'guilty',
    'stone': 'stone',
}
EVENT_STANDING = {'error': 'panic', 'aborted': 'guilty', 'stone': 'stone'}
NOTIFY_SPRITES = {'waiting': 'knock', 'celebrate': 'confetti'}   # 刚回复完的提示姿势，NOTIFY_S 秒后就安静下来
REACTION_SPRITES = {'pet': 'shy', 'poke': 'poked', 'angry': 'remind-3'}   # 摸头、戳、连戳生气时的立绘
# 立绘从上往下数，"屁股坐在窗口边上"那条线的位置（占立绘高度的比例）；站着的立绘是 1.0（脚底）
ANCHOR = {'sit-swing-1': 0.70, 'sit-swing-2': 0.70, 'sit-watch': 0.70, 'typing': 0.80,
          'sit-down-1': 1.0, 'sit-down-2': 0.85, 'sleep-1': 0.70, 'sleep-2': 0.70}
SITTING = frozenset({'sit-swing-1', 'sit-swing-2', 'sit-watch', 'typing', 'sleep-1', 'sleep-2'})
AIRBORNE = frozenset({'jump', 'fall', 'held', 'held-swing', 'land'})   # 这些姿势切换时不播坐下/站起过渡


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left


@dataclass(frozen=True)
class WinInfo:
    hwnd: int
    rect: Rect
    work: Rect              # 窗口所在显示器的工作区（不含任务栏）
    maximized: bool = False


@dataclass(frozen=True)
class Surface:
    """她能坐的一条水平线：窗口的顶边，或者屏幕底边（任务栏上沿）。"""
    key: object
    left: float
    right: float
    y: float
    kind: str  # 'window' | 'ground'


@dataclass(frozen=True)
class Jump:
    t0: float
    dur: float
    x0: float
    y0: float
    peak: float


@dataclass(frozen=True)
class Motion:
    mode: str                         # 'perch' 坐着 | 'hop' 跳去别处 | 'fall' 下落 | 'held' 被拿着
    x: float
    y: float
    support: object | None            # 她脚下那块地板的 key；下落/被拿着时是 None，'hop' 时是要跳去的那块
    frac: float                       # 在地板上的位置（0 最左，1 最右），地板移动时靠它跟着走
    face: int = 1                     # 1 朝右，-1 朝左
    vx: float = 0.0
    vy: float = 0.0
    jump: Jump | None = None
    landed_at: float = -1e9
    next_swing: float = 0.0           # 下一次晃腿的时刻
    swing_until: float = 0.0          # 晃腿到什么时候为止（过了就是静坐）


@dataclass(frozen=True)
class Pose:
    file: str      # pet/sprites/<file>.png
    flip: bool     # 是否水平翻转
    anchor: float  # 立绘里"接触地板"的那条线（占高度的比例）


@dataclass(frozen=True)
class Swing:
    """被拎着时身体往哪边甩：-1 是往左拖（身体落在右边），1 是往右拖，0 不甩。"""
    direction: int = 0
    until: float = 0.0


@dataclass(frozen=True)
class Posture:
    """她现在算坐着还是站着，从什么时候开始的；用来播坐下/站起的过渡。"""
    sitting: bool = False
    since: float = -1e9


def _ground(work: Rect, within: Rect | None = None, owner: object = None, margin: int = 24) -> Surface:
    """屏幕底边（任务栏上沿）。给了 within，她就只待在那个窗口的左右范围内，不满屏幕乱站。"""
    left, right = float(work.left), float(work.right)
    if within is not None:
        left, right = max(left, within.left + margin), min(right, within.right - margin)
    return Surface(key=('ground', work.left, work.top, owner), left=left, right=right, y=float(work.bottom),
                   kind='ground')


def choose_surface(win: WinInfo | None, fallback_work: Rect, *, need_above: int, margin: int = 24,
                   min_width: int = 240) -> Surface:
    """"跟着当前窗口"时她该去哪：窗口顶边上方放得下她就坐上去；放不下（最大化、贴着屏幕顶），就坐在屏幕底边、
    待在这个窗口的左右范围内；窗口太窄或者没有窗口，坐在整条屏幕底边。"""
    if win is None:
        return _ground(fallback_work)
    r = win.rect
    if r.width < min_width:
        return _ground(win.work)
    if win.maximized or r.top - need_above < win.work.top:
        return _ground(win.work, within=r, owner=win.hwnd, margin=margin)
    return Surface(key=win.hwnd, left=float(r.left + margin), right=float(r.right - margin), y=float(r.top),
                   kind='window')


def build_surfaces(windows: Sequence[WinInfo], work_areas: Sequence[Rect], margin: int = 24,
                   min_width: int = 240) -> list:
    """所有能坐的地板：每块显示器一条屏幕底边，加上每个普通窗口的顶边（最大化的、太窄的不算）。"""
    out = [_ground(work) for work in work_areas]
    for win in windows:
        r = win.rect
        if win.maximized or r.width < min_width:
            continue
        out.append(Surface(key=win.hwnd, left=float(r.left + margin), right=float(r.right - margin),
                           y=float(r.top), kind='window'))
    return out


def x_at(surface: Surface, frac: float) -> float:
    return surface.left + frac * (surface.right - surface.left)


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def _later(now: float, span: tuple, rng) -> float:
    return now + rng.uniform(*span)


def spawn(surface: Surface, frac: float, now: float, face: int = 1, rng=None) -> Motion:
    """直接坐在某块地板上。"""
    rng = rng or random
    return Motion(mode='perch', x=x_at(surface, frac), y=surface.y, support=surface.key, frac=frac, face=face,
                  next_swing=_later(now, SWING_EVERY, rng))


def drop(x: float, y: float, now: float, vx: float = 0.0, vy: float = 0.0, face: int = 1, rng=None) -> Motion:
    """从半空开始下落。"""
    rng = rng or random
    return Motion(mode='fall', x=x, y=y, support=None, frac=0.5, face=face, vx=vx, vy=vy,
                  next_swing=_later(now, SWING_EVERY, rng))


def hold(m: Motion, x: float, y: float) -> Motion:
    """被用户拿起来：位置完全由光标决定，她自己不动。"""
    return replace(m, mode='held', x=x, y=y, support=None, vx=0.0, vy=0.0, jump=None)


def release(m: Motion, vx: float = 0.0, vy: float = 0.0) -> Motion:
    """用户松手：带着甩出去的速度自由落体。不是被拿着的就什么都不做。"""
    if m.mode != 'held':
        return m
    return replace(m, mode='fall', vx=vx, vy=vy)


def hop_to(m: Motion, surface: Surface, now: float, scale: float = 1.0) -> Motion:
    """起跳，去另一块地板（比如跟着切换过来的前台窗口）。终点跟着那块地板走，落地前它动了也能落准。"""
    frac = _clamp(m.frac, *HOP_FRAC)
    dist = math.hypot(x_at(surface, frac) - m.x, surface.y - m.y)
    jump = Jump(t0=now, dur=min(1.0, 0.45 + dist / 2500), x0=m.x, y0=m.y, peak=40 * scale + 0.1 * dist)
    return replace(m, mode='hop', support=surface.key, frac=frac, face=1 if x_at(surface, frac) >= m.x else -1,
                   jump=jump, vx=0.0, vy=0.0)


def _land(m: Motion, s: Surface, x: float, now: float, rng) -> Motion:
    width = s.right - s.left
    frac = _clamp((x - s.left) / width, 0.0, 1.0) if width > 0 else 0.5
    return replace(m, mode='perch', x=x, y=s.y, support=s.key, frac=frac, vx=0.0, vy=0.0, jump=None,
                   landed_at=now, next_swing=_later(now, SWING_EVERY, rng))


def _fall(m: Motion, surfaces: Sequence[Surface], now: float, dt: float, scale: float, rng) -> Motion:
    vy = min(m.vy + GRAVITY * scale * dt, TERMINAL * scale)
    vx = m.vx * max(0.0, 1 - AIR_X * dt)
    x1, y1 = m.x + vx * dt, m.y + vy * dt
    tol = LAND_TOL * scale
    # 这一帧从 m.y 落到 y1，中间穿过的地板都算（帧再长也不会穿过去）；取最高的那块
    reachable = [s for s in surfaces if s.left <= x1 <= s.right and m.y - tol <= s.y <= y1 + tol]
    if reachable:
        return _land(m, min(reachable, key=lambda s: s.y), x1, now, rng)
    if surfaces and y1 >= max(s.y for s in surfaces):
        # 掉到所有地板的下面了（比如被甩出了屏幕）：落在最低的那块上，x 夹进它的范围
        lowest = max(s.y for s in surfaces)
        s = min((s for s in surfaces if s.y == lowest), key=lambda s: max(s.left - x1, 0.0, x1 - s.right))
        return _land(m, s, _clamp(x1, s.left, s.right), now, rng)
    return replace(m, x=x1, y=y1, vx=vx, vy=vy)


def _fly(m: Motion, s: Surface, now: float, rng) -> Motion:
    j = m.jump
    p = _clamp((now - j.t0) / j.dur, 0.0, 1.0)
    dest_x, dest_y = x_at(s, m.frac), s.y
    if p >= 1.0:
        return _land(m, s, dest_x, now, rng)
    return replace(m, x=j.x0 + (dest_x - j.x0) * p, y=j.y0 + (dest_y - j.y0) * p - 4 * j.peak * p * (1 - p))


def _perch(m: Motion, s: Surface, now: float, can_swing: bool, rng) -> Motion:
    m = replace(m, x=x_at(s, m.frac), y=s.y)
    if not can_swing or m.swing_until > now:  # 忙着（Claude 在干活、提醒休息）或者正在晃腿：不另起行为
        return m
    if now >= m.next_swing:
        dur = rng.uniform(*SWING_FOR)
        return replace(m, swing_until=now + dur, next_swing=now + dur + rng.uniform(*SWING_EVERY))
    return m


def tick(m: Motion, surfaces: Sequence[Surface], now: float, dt: float, *, scale: float = 1.0,
         can_swing: bool = True, rng=None) -> Motion:
    """走一帧。surfaces 是此刻所有的地板：她脚下那块动了她就跟着动，没了她就掉下去。"""
    rng = rng or random
    if m.mode == 'held':
        return m
    if m.mode == 'fall':
        return _fall(m, surfaces, now, dt, scale, rng)
    s = {surface.key: surface for surface in surfaces}.get(m.support)
    if s is None:  # 脚下的窗口关了、最小化了，或者要跳去的那块没了
        return replace(m, mode='fall', support=None, jump=None, vx=0.0, vy=0.0)
    if m.mode == 'hop':
        return _fly(m, s, now, rng)
    return _perch(m, s, now, can_swing, rng)


def swing_step(s: Swing, vx: float, now: float, threshold: float, hold_s: float = 0.3) -> Swing:
    """拖得比 threshold 快就按拖动方向歪；慢下来以后再歪 hold_s 秒才回正。"""
    if abs(vx) >= threshold:
        return Swing(1 if vx > 0 else -1, now + hold_s)
    return s if now < s.until else Swing()


def settle(p: Posture, target: Pose, now: float) -> tuple:
    """坐下/站起的过渡：目标姿势从站变坐（或反过来）时，先播 TRANSITION_S 秒的过渡帧，站起就倒着播。
    在空中的姿势（被拎着、下落、跳）直接切，不过渡。"""
    if target.file in AIRBORNE:
        return Posture(False), target
    want = target.file in SITTING
    if want != p.sitting:
        p = Posture(want, now)
    age = now - p.since
    if age >= TRANSITION_S:
        return p, target
    frames = ('sit-down-1', 'sit-down-2') if p.sitting else ('sit-down-2', 'sit-down-1')
    file = frames[min(1, int(age / (TRANSITION_S / 2)))]
    return p, Pose(file, target.flip, ANCHOR[file])


def fixed_sprite(state: str, state_age: float) -> str:
    """不坐窗口时 Claude 状态对应的立绘：刚回复完敲门/撒花 NOTIFY_S 秒，之后安静地站着等你。"""
    if state in NOTIFY_SPRITES and state_age >= NOTIFY_S:
        return 'smile'
    return FIXED_SPRITES.get(state, 'smile')


def _reaction(kind: str) -> str:
    return REACTION_SPRITES.get(kind) or FIXED_SPRITES.get(kind, 'smile')


def choose_pose(m: Motion, now: float, state: str, state_age: float, remind: int = 0,
                speech: str | None = None, swing: int = 0) -> Pose:
    """坐窗口时根据"在干嘛"挑立绘。swing：被拎着时身体往哪边甩（见 Swing）。"""
    if m.mode == 'held':
        if swing:
            return Pose('held-swing', swing > 0, 1.0)  # 立绘画的是往左拖（身体落在右边）；往右拖就翻过来
        return Pose('held', False, 1.0)
    if m.mode == 'fall':
        return Pose('fall', m.face < 0, 1.0)
    if m.mode == 'hop':
        return Pose('jump', m.face < 0, 1.0)
    if now - m.landed_at < LAND_POSE_S:
        return Pose('land', m.face < 0, 1.0)
    if speech:
        return Pose(_reaction(speech), False, 1.0)
    if remind:
        return Pose(f'remind-{remind}', False, 1.0)
    if state in EVENT_STANDING:
        return Pose(EVENT_STANDING[state], False, 1.0)
    if state in NOTIFY_SPRITES and state_age < NOTIFY_S:
        return Pose(NOTIFY_SPRITES[state], False, 1.0)
    if state == 'idle':  # 等你回复太久：坐着打瞌睡，头一点一点
        file = f'sleep-{1 + int(now / SLEEP_S) % 2}'
        return Pose(file, False, ANCHOR[file])
    if state in ('thinking', 'reading'):
        return Pose('sit-watch', m.frac < 0.5, ANCHOR['sit-watch'])  # 在左半边就朝右看，朝着窗口里面
    if state == 'tool':
        return Pose('typing', False, ANCHOR['typing'])
    # 平时静静地坐着；只有"晃腿"这个小行为进行的时候，两帧才慢慢交替
    file = f'sit-swing-{1 + int(now / SWING_S) % 2}' if m.swing_until > now else 'sit-swing-1'
    return Pose(file, False, ANCHOR[file])
