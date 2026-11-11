# Claude 娘桌宠悬浮窗
# mod 把每个会话的状态写进 <运行目录>/sessions/<会话id>.json，这里读最新的那个来换表情和气泡。
# 默认她坐在你当前窗口的顶边上陪你，窗口一换她就跳过去；也可以关掉，固定在一个位置。
# 用法：pythonw pet.pyw <运行目录>
import ctypes
import ctypes.wintypes as wt
import functools
import glob
import json
import math
import os
import random
import socket
import subprocess
import sys
import time
import tkinter as tk
import traceback
import winsound
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFont, ImageOps

from blink import closed as eyes_closed, start as start_blink, step as step_blink
from breaks import ACK_LINES, BreakConfig, BreakEvent, BreakState, acknowledge, is_night, pick_line
from breaks import step as break_step
from chatter import CHAT_SHOW_S, Context, load_lines, next_line, pick, start as start_chatter
from commands import parse as parse_command
from effects import zz_marks
from files import write_json_atomic
from interact import HEAD_SHARE, REACT_S, Touch, on_click, on_double as touch_double, on_move, on_tick
from motion import (
    FIXED_SPRITES, Posture, Swing, build_surfaces, choose_pose, choose_surface, fixed_sprite, hold, hop_to, release,
    settle, spawn, swing_step, tick,
)
from screen import choose_z, is_fullscreen, place_bubble, visible_span
from sessions import BUSY, Session, busy, new_sound, summarize

HERE = os.path.dirname(os.path.abspath(__file__))
SOUNDS = os.path.join(os.path.dirname(HERE), 'sounds')
RUN_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser('~'), '.claude', 'desk-pet')
SESSIONS = os.path.join(RUN_DIR, 'sessions')
SETTINGS = os.path.join(RUN_DIR, 'settings.json')
COMMAND = os.path.join(RUN_DIR, 'command.json')   # 插件收到 /pet 子命令后写在这里
COMMAND_TTL_MS = 30_000                           # 比这更早的命令不执行（桌宠重启后不重放旧命令）
LINES = os.path.join(HERE, 'lines.json')          # 闲聊、摸头、戳她的台词，可以随便改
PATCHES = os.path.join(HERE, 'sprites', 'patches.json')   # 眨眼补丁的位置（tools/blink_patch.py 生成）

# 只允许开一个桌宠：占住一个本地端口，占不到说明已经有一个在跑
try:
    _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _lock.bind(('127.0.0.1', int(os.environ.get('DESK_PET_PORT', 47321))))  # 环境变量只给调试用
except OSError:
    sys.exit(0)

import dpi  # noqa: E402

dpi.enable()  # 要在创建窗口之前：按显示器感知缩放，窗口坐标都是真实像素

import winapi  # noqa: E402  要等 DPI 感知设好以后再导入，窗口坐标才是真实像素

SPRITES = FIXED_SPRITES  # Claude 的状态 → 立绘
REACTIONS = {'pet': 'shy', 'poke': 'poked', 'angry': 'remind-3'}  # 摸头、戳、连戳生气时的立绘（'huff' 不换立绘）
OTHER_SPRITES = ('remind-1', 'remind-2', 'remind-3', 'sleep-1', 'sleep-2', 'held', 'held-swing',
                 'jump', 'land', 'fall', 'sit-down-1', 'sit-down-2', 'sit-swing-1', 'sit-swing-2', 'sit-watch')
SIZES = {'小': 140, '中': 190, '大': 250}  # 角色高度（100% 缩放下的像素）
BASE_H = 400          # 立绘源图里角色大约多高；各张图按同一个比例缩放，姿势之间才能保持相对大小
IDLE_AFTER = 10 * 60  # 等你回复超过 10 分钟就开始打瞌睡
OOPS_S = 3.0          # 一次工具调用失败：皱眉这么久，然后接着想办法
SLEEP_S = 1.2         # 打瞌睡两帧交替的间隔
SWING_V = 250         # 拖得比这快（像素/秒，100% 缩放），被拎着的她就往反方向歪
SWING_HOLD_S = 0.3    # 甩动停下后，身体还歪着多久才回正
SETTLE_S = 0.8        # 前台窗口稳定这么久，她才跳过去（Alt+Tab 路过的不算）
THROW_MAX = 1600      # 松手时甩出去的最大速度（像素/秒，100% 缩放）
LEG_ROOM = 0.32       # 坐在窗口边上时，线下面留给垂下来的两条腿的高度（占角色高度的比例）
ORANGE = (232, 116, 59, 255)   # 气泡描边
CREAM = (255, 250, 243, 255)   # 气泡底色
INK = (58, 42, 26, 255)        # 气泡文字
SUB_INK = (150, 136, 122, 255)  # 第二行小字（别的会话的情况）
ANIMATED = {'thinking', 'tool', 'reading'}  # 这些状态下，台词末尾的“…”会变成跳动的点点点
DOTS = '...'
SS = 4      # 气泡先按 4 倍大小画再缩小，圆角和尾巴的边缘更平滑
HOP = 14    # 换状态时跳起的高度
UP = 7      # 平时动作往上的最大幅度（急着跺脚时最高）
BOB = 3     # 平时动作往下的最大幅度
GAP = 4     # 跳到最高时，头顶和气泡尖之间至少留这么多
BUBBLE_W = 340  # 窗口（气泡最宽能到）多宽；角色在中间，气泡跟着文字变宽，两边多出来的地方是透明的
SUB_H = 16  # 第二行小字占的高度；窗口顶上一直留着，有小字时气泡往上长，角色位置不变

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi


# ---------- 分层窗口（逐像素透明） ----------
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD), ('biSizeImage', wt.DWORD),
                ('biXPelsPerMeter', wt.LONG), ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte),
                ('SourceConstantAlpha', ctypes.c_ubyte), ('AlphaFormat', ctypes.c_ubyte)]


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 2
GA_ROOT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9

# 64 位 Python 下句柄是指针大小，必须声明类型，否则会被截断成 32 位
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetWindowLongW.restype = wt.LONG
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = wt.LONG
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION),
                                       wt.DWORD]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsZoomed.argtypes = [wt.HWND]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE,
                                   wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]


class Layered:
    """用 UpdateLayeredWindow 把一张 RGBA 图贴到窗口上：每个像素有自己的透明度，边缘不会有锯齿和毛边，
    完全透明的地方鼠标还能直接点穿。"""

    def __init__(self, widget):
        self.widget = widget
        self.screen = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(self.screen)
        self.bits = ctypes.c_void_p()
        self.bmp = self.old = None
        self.size = (0, 0)

    def hwnd(self):
        # Tk 的顶层窗口是 winfo_id() 那个子窗口的外层；每次现取，防止 Tk 重建外层窗口后失效
        return user32.GetAncestor(self.widget.winfo_id(), GA_ROOT)

    def show(self, im):
        hwnd = self.hwnd()
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not ex & WS_EX_LAYERED:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        w, h = im.size
        if (w, h) != self.size:
            if self.bmp:
                gdi32.SelectObject(self.dc, self.old)
                gdi32.DeleteObject(self.bmp)
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # 高度取负 = 从上往下存
            self.bmp = gdi32.CreateDIBSection(self.screen, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
            self.old = gdi32.SelectObject(self.dc, self.bmp)
            self.size = (w, h)
        data = im.tobytes('raw', 'BGRa')  # Windows 要的是 BGRA 顺序、颜色预乘透明度
        ctypes.memmove(self.bits.value, data, len(data))
        user32.UpdateLayeredWindow(hwnd, self.screen, None, ctypes.byref(wt.SIZE(w, h)), self.dc,
                                   ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)),
                                   ULW_ALPHA)


# ---------- 显示器 ----------
MONITORINFO = winapi.MONITORINFO  # 和 winapi 共用同一个结构：两边都给 user32 设参数类型，类不同会互相打架

user32.MonitorFromPoint.restype = wt.HMONITOR
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetMonitorInfoW.restype = wt.BOOL
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]


def monitor_at(x, y):
    """返回 (x, y) 所在显示器（不在任何显示器上就取最近的那个）的 (整个屏幕, 去掉任务栏的工作区)，
    都是 (左, 上, 右, 下)。"""
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    mon = user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        m, w = info.rcMonitor, info.rcWork
        return (m.left, m.top, m.right, m.bottom), (w.left, w.top, w.right, w.bottom)
    full = (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    return full, full


# ---------- Claude 窗口、前台窗口 ----------
def process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ''
    finally:
        kernel32.CloseHandle(h)


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def claude_is_foreground():
    hwnd = user32.GetForegroundWindow()
    return bool(hwnd) and process_name(window_pid(hwnd)) == 'claude.exe'


def find_claude_window():
    found = []
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 \
                and process_name(window_pid(hwnd)) == 'claude.exe':
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(PROC(cb), 0)
    return found[0] if found else None


def foreground_frame():
    """前台窗口的 (外框, 类名, 是否最大化)；没有前台窗口或是桌宠自己时返回 None。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or window_pid(hwnd) == os.getpid():
        return None
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return (r.left, r.top, r.right, r.bottom), buf.value, bool(user32.IsZoomed(hwnd))


def claude_running():
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq claude.exe', '/NH'], capture_output=True,
                             text=True, timeout=10, creationflags=0x08000000).stdout
        return 'claude.exe' in out.lower()
    except Exception:
        return True


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('dwTime', wt.DWORD)]


kernel32.GetTickCount.restype = wt.DWORD


def idle_seconds():
    """距离上一次键盘/鼠标操作过了多少秒；系统接口失败时当作"刚操作过"。"""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


# ---------- 设置和状态文件 ----------
def load_settings():
    try:
        with open(SETTINGS, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        write_json_atomic(SETTINGS, s)  # 先写临时文件再替换，写到一半被关掉也不会把设置弄坏
    except Exception:
        log_error()


def read_sessions():
    """读所有会话的状态；顺手清掉已经关掉的会话（ended）和一天没动静的旧文件。"""
    out, now = [], time.time()
    for p in glob.glob(os.path.join(SESSIONS, '*.json')):
        try:
            mtime = os.path.getmtime(p)
            if now - mtime > 86400:
                os.remove(p)
                continue
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
            state = str(data.get('state', 'waiting'))
            if state == 'ended':
                os.remove(p)
                continue
            out.append(Session(mtime=mtime, state=state, text=str(data.get('text', '')),
                               project=str(data.get('project', '')), sound=str(data.get('sound') or ''),
                               key=os.path.splitext(os.path.basename(p))[0],
                               since=seconds(data.get('since')), turn=seconds(data.get('turn')),
                               helpers=int(data.get('helpers') or 0)))
        except Exception:
            continue
    return out


def seconds(ms):
    """mod 写的时间是毫秒，没有就是 0。"""
    return ms / 1000 if isinstance(ms, (int, float)) and ms > 0 else 0.0


def log_error(message=None):
    """把错误追加到 pet-error.log；桌宠不能因为一次出错就停下。"""
    try:
        with open(os.path.join(RUN_DIR, 'pet-error.log'), 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + (message or traceback.format_exc()) + '\n')
    except Exception:
        pass


def load_patches():
    """眨眼补丁：{立绘名: (x, y)}。文件坏了就当没有补丁（不眨眼），记一次日志。"""
    try:
        with open(PATCHES, encoding='utf-8') as f:
            return {name: (int(v['x']), int(v['y'])) for name, v in json.load(f).items()}
    except FileNotFoundError:
        return {}
    except Exception:
        log_error()
        return {}


# ---------- 画图 ----------
def prepare(path, k, flip=False, patch=None):
    """按 k 倍缩放立绘（可水平翻转）。patch=(补丁路径, x, y) 时先把补丁贴上去（眨眼）。
    先换成预乘透明度再缩放，半透明的发丝边缘不会混进透明像素里的杂色。"""
    im = Image.open(path).convert('RGBA')
    if patch:
        im.alpha_composite(Image.open(patch[0]).convert('RGBA'), patch[1:])
    if flip:
        im = ImageOps.mirror(im)
    size = (max(1, round(im.width * k)), max(1, round(im.height * k)))
    return im.convert('RGBa').resize(size, Image.LANCZOS).convert('RGBA')


@functools.lru_cache(maxsize=16)
def load_font(px):
    fonts = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
    for name, index in (('msyh.ttc', 1), ('msyh.ttc', 0), ('msyh.ttf', 0), ('simhei.ttf', 0)):
        try:
            return ImageFont.truetype(os.path.join(fonts, name), px, index=index)
        except OSError:
            continue
    try:
        return ImageFont.load_default(px)
    except TypeError:  # Pillow 10.1 以前不支持指定大小
        return ImageFont.load_default()


def fit_text(text, max_w, measure):
    """文字太长就从中间省略，保留开头（“在改”）和结尾（扩展名）。"""
    if measure(text) <= max_w:
        return text
    head, tail = len(text) // 2, len(text) - len(text) // 2
    while head + tail > 1:
        if head >= tail:
            head -= 1
        else:
            tail -= 1
        cut = text[:head] + '…' + text[len(text) - tail:]
        if measure(cut) <= max_w:
            return cut
    return '…'


def draw_marks(image, marks, x, y):
    """把 z Z 画到 image 上（就地修改）。(x, y) 是记号飘出来的起点，marks 来自 effects.zz_marks。
    浅色字、深色描边，什么背景上都看得清。"""
    layer = Image.new('RGBA', image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for m in marks:
        if m.alpha > 0:
            d.text((x + m.dx, y + m.dy), m.text, font=load_font(m.size), fill=CREAM[:3] + (m.alpha,),
                   stroke_width=max(1, round(m.size / 10)), stroke_fill=INK[:3] + (m.alpha,))
    image.alpha_composite(layer)


def bob_for(file, t):
    """立绘自己的小动作（上下偏移，像素，100% 缩放）；负数是往上。"""
    if file in ('stone', 'held', 'held-swing', 'jump', 'land', 'fall') or file.startswith('sit-down'):
        return 0                                # 悬空、落地、坐下站起的立绘自带动作，不能再叠加起伏
    if file.startswith('sleep'):
        return 1.0 * math.sin(t * 1.6)          # 打瞌睡：慢慢地呼吸
    if file == 'remind-3':
        return -UP * abs(math.sin(t * 4))      # 急了、生气：原地一下一下跺脚
    if file == 'remind-2':
        return 2.5 * math.sin(t * 7)            # 催促：着急地晃
    if file == 'typing':
        return 2.5 * math.sin(t * 9)            # 敲键盘：快速小幅抖动
    if file == 'panic':
        return 2 * math.sin(t * 25)             # 慌张：发抖
    if file.startswith('sit'):
        return 1.2 * math.sin(t * 2.2)          # 坐着：轻轻晃
    return BOB * math.sin(t * 2.2)              # 平时：慢慢呼吸


class Pet:
    def __init__(self):
        self.settings = load_settings()
        self.started_ms = time.time() * 1000   # 启动时刻；比它早的 /pet 命令是上一个桌宠留下的，不执行
        self.root = tk.Tk()
        self.root.title('Claude 桌宠')
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        x, y = self.settings.get('x'), self.settings.get('y')
        self.scale = dpi.scale_at(x, y) if isinstance(x, int) and isinstance(y, int) else dpi.scale_at(0, 0)

        # 画面全部由 Layered 贴上去，这个画布只用来接收鼠标事件
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.font = ('Microsoft YaHei UI', 10)  # 右键菜单用

        self.state, self.text, self.project = 'waiting', '我来啦～', ''
        self.state_since = time.time()
        self.last_seen = None          # 桌宠上次显示的是哪个会话的哪次写入：(会话 id, 写入时间)
        self.sound_seen = None         # 各会话上次的写入时间，用来判断谁刚写了提示音
        self.hop_at = time.time()
        self.hidden = False
        self.forced_hidden = False     # /pet hide 藏起来了
        self.fullscreen = False        # 她所在的那块屏正在全屏
        self.drag = None
        self.press_at, self.moved = None, False
        self.drag_samples = []         # 最近几次拖动的 (时刻, 脚底 x, 脚底 y)，算甩动、甩出去的速度
        self.swing = Swing()           # 被拎着时身体往哪边歪
        self.last_alive = time.time()
        self.last_frame = None
        self.last_command = None       # 处理过的最后一条 /pet 命令的 id
        self.speech = None             # (气泡文字, 截止时刻, 立绘或 None)：摸头、戳、闲聊时临时说一句
        self.sub = None                # 多个会话时气泡里的第二行小字
        self.bubble_key = None
        self.touch = Touch()           # 摸头、戳的识别状态
        self.lines, err = load_lines(LINES)
        if err:
            log_error(err)
        self.chatter = start_chatter(time.time(), random)
        self.blink = start_blink(time.time(), random)
        self.patches = load_patches()
        self.sprite_box = (0, 0)       # 上一帧立绘在窗口里的 (上, 高)，判断鼠标是不是在头上
        self.break_cfg = BreakConfig.from_settings(self.settings)
        self.break_state = BreakState()
        self.remind_text = ''
        self.pending_break = None      # 全屏期间到点的休息提醒，等退出全屏再出

        self.roam = self.settings.get('roam', True) is not False   # 坐在窗口上；关掉就固定在一个位置
        self.following = self.settings.get('follow', True) is not False  # 自动跟着当前窗口；被你拖走后暂停
        self.motion = None             # 坐窗口时她的位置和动作（motion.Motion）
        self.target_hwnd = None        # 当前窗口（前台窗口稳定之后才算），"跟着当前窗口"时她要去它那儿
        self.candidate = None          # (窗口, 开始当前台的时刻)：稳定了才换
        self.work_areas = winapi.monitor_work_areas()
        self.surfaces = []             # 此刻所有的地板（每 0.25 秒刷新）
        self.posture = Posture()       # 坐着还是站着（播坐下/站起的过渡用）
        self.win_pos = None            # 坐窗口时上一次摆窗口的位置
        self.last_tick = time.time()
        self.z = None                  # 上一次设置的层级（'top' 或窗口句柄）
        self.z_failed = None           # 贴着它设层级失败过的窗口句柄；在它身上就不再重试，直接置顶

        self.build()
        self.place_window()
        self.make_menu()
        self.root.update_idletasks()
        self.layer = Layered(self.root)

        self.canvas.bind('<ButtonPress-1>', self.on_press)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)
        self.canvas.bind('<Double-Button-1>', self.on_double)
        self.canvas.bind('<Motion>', self.on_motion)
        self.canvas.bind('<Leave>', lambda e: self.on_leave())
        self.canvas.bind('<Button-3>', lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.poll()
        self.watch_surface()
        self.animate()
        self.watch_focus()
        self.watch_claude()
        self.watch_chat()
        self.watch_break()

    # ---------- 布局 ----------
    def build(self):
        s = self.scale
        self.k = SIZES.get(self.settings.get('size', '中'), 190) * s / BASE_H
        self.cache = {}
        sizes = []
        for name in {*SPRITES.values(), *REACTIONS.values(), *OTHER_SPRITES}:
            with Image.open(os.path.join(HERE, 'sprites', name + '.png')) as im:
                sizes.append(im.size)
        self.char_h = round(max(h for _, h in sizes) * self.k)
        self.char_w = round(max(w for w, _ in sizes) * self.k)
        self.W = max(self.char_w, round(BUBBLE_W * s))
        self.span, self.span_at = (0, self.W), None   # 窗口里在屏幕上的那一段（气泡别伸出屏幕）
        # 气泡在最上面，下面给跳跃和晃动留够空间，角色怎么动都碰不到气泡，也不会出窗口底边
        self.bubble_bottom = round((38 + SUB_H) * s)
        self.tail_tip = self.bubble_bottom + round(10 * s)
        self.floor = self.tail_tip + round((GAP + HOP + UP) * s) + self.char_h  # 站着时脚底、坐着时屁股落在这条线上
        # 坐窗口时线下面要留出垂下来的腿；固定位置时只留往下晃的那一点
        self.H = self.floor + (round(LEG_ROOM * self.char_h) if self.roam else round(BOB * s))
        self.pfont = load_font(round(10 * 96 / 72 * s))  # 10 磅
        self.sfont = load_font(round(8 * 96 / 72 * s))   # 第二行小字 8 磅
        self.canvas.config(width=self.W, height=self.H)
        self.last_frame = None
        self.win_pos = None
        self.draw_bubble()

    def sprite(self, file, flip=False, blink=False):
        key = (file, flip, blink)
        if key not in self.cache:
            patch = None
            if blink:
                x, y = self.patches[file]
                patch = (os.path.join(HERE, 'sprites', file + '.blink.png'), x, y)
            self.cache[key] = prepare(os.path.join(HERE, 'sprites', file + '.png'), self.k, flip, patch)
        return self.cache[key]

    def draw_bubble(self):
        """按当前台词排好气泡：算出文字和气泡的位置，画好不带文字的气泡底图。"""
        s, measure = self.scale, self.pfont.getlength
        pad = round(10 * s)
        lo, hi = max(1, self.span[0]), min(self.W - 2, self.span[1])
        max_w = max(round(100 * s), hi - lo - 2 * pad - 4)   # 文字最宽多少：窗口多宽、屏幕边上还剩多少
        state = self.effective_state()
        text, sub, self.dots_base = self.display_text(), self.bubble_sub(), None
        self.bubble_key = (state, text, sub, self.span)
        if (state in ANIMATED and not self.speaking() and not self.break_state.level
                and (text.endswith('…') or text == '思考中')):
            # 末尾的省略号换成一跳一跳的点点点；文字左对齐，按三个点的宽度留好位置，点数变化时文字不会左右晃
            self.dots_base = fit_text(text.rstrip('…'), max_w - measure(DOTS), measure)
            text = self.dots_base + DOTS
        else:
            text = fit_text(text, max_w, measure)
        text_w = measure(text)
        self.bubble_text = text
        # 有第二行小字时气泡往上多长一行；没有时，上面那一行的位置空着（透明）
        y0 = 2 if sub else 2 + round(SUB_H * s)
        self.text_y = y0 + round(18 * s)
        if sub:
            sub = fit_text(sub, max_w, self.sfont.getlength)
        inner = max(text_w, self.sfont.getlength(sub) if sub else 0)
        # 气泡跟着文字变宽，以角色为中心；靠近屏幕边时往里挪，尾巴还是指着她
        width = math.ceil(inner) + 2 * pad
        x0 = round(place_bubble(width, self.W / 2, lo, hi, round(18 * s)))
        x1 = x0 + width
        mid = (x0 + x1) / 2
        self.text_x = mid - text_w / 2
        self.bubble_bg = self.bubble_image(x0, x1, y0)
        if sub:
            ImageDraw.Draw(self.bubble_bg).text((mid, self.text_y + round(17 * s)), sub, font=self.sfont,
                                                fill=SUB_INK, anchor='mm')
        self.bubble_cache = {}

    def bubble_image(self, x0, x1, y0):
        s = self.scale
        y1, tip = self.bubble_bottom, self.tail_tip
        lw = max(2, round(2 * s))
        r = round(12 * s)
        cx, half = self.W / 2, 6 * s
        big = Image.new('RGBA', (self.W * SS, (tip + 2) * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)

        def k(*pts):
            return [(x * SS, y * SS) for x, y in pts]

        # 尾巴是个倒三角：先画橙色的外轮廓，再在里面画一个往里缩了一圈描边宽度的奶油色三角
        slope = half / (tip - y1)                              # 尾巴斜边每往下 1 像素，往里收多少
        inner_tip = tip - lw * math.hypot(1, slope) / slope    # 三角往里缩 lw 后，尖端上移的距离
        top = y1 - lw                                          # 内三角的上边藏进气泡里，和底色连成一片
        d.rounded_rectangle(k((x0, y0), (x1, y1)), radius=r * SS, fill=ORANGE)
        d.polygon(k((cx - half - lw * slope, y1 - lw), (cx + half + lw * slope, y1 - lw), (cx, tip)), fill=ORANGE)
        d.rounded_rectangle(k((x0 + lw, y0 + lw), (x1 - lw, y1 - lw)), radius=(r - lw) * SS, fill=CREAM)
        d.polygon(k((cx - (inner_tip - top) * slope, top), (cx + (inner_tip - top) * slope, top), (cx, inner_tip)),
                  fill=CREAM)
        return big.convert('RGBa').resize((self.W, tip + 2), Image.BOX).convert('RGBA')

    def bubble_with(self, text):
        im = self.bubble_cache.get(text)
        if im is None:
            im = self.bubble_bg.copy()
            ImageDraw.Draw(im).text((self.text_x, self.text_y), text, font=self.pfont, fill=INK,
                                    anchor='lm')
            self.bubble_cache[text] = im
        return im

    def render(self, sprite_key, anchor, offset, text, t):
        """anchor：立绘里落在线上的那条线（占高度的比例，站着是 1.0）。坐着时立绘往下挪，气泡也跟着往下挪。"""
        sleeping = sprite_key[0].startswith('sleep')
        key = (sprite_key, anchor, offset, text, id(self.bubble_bg), int(t * 15) if sleeping else None)
        if key == self.last_frame:
            return
        self.last_frame = key
        frame = Image.new('RGBA', (self.W, self.H), (0, 0, 0, 0))
        sprite = self.sprite(*sprite_key)
        sink = round((1 - anchor) * sprite.height)
        top = self.floor + sink + offset - sprite.height
        self.sprite_box = (top, sprite.height)
        frame.alpha_composite(sprite, (self.W // 2 - sprite.width // 2, max(0, top)))
        frame.alpha_composite(self.bubble_with(text), (0, sink))
        if sleeping:  # z Z 从头顶右上方冒出来（离开头发，免得压在黑色蝴蝶结上看不见）
            draw_marks(frame, zz_marks(t, self.scale), self.W // 2 + round(sprite.width * 0.36),
                       top + round(sprite.height * 0.02))
        self.layer.show(frame)

    def display_text(self):
        if self.speaking():
            return self.speech[0]
        if self.break_state.level:
            return self.remind_text
        if self.effective_state() == 'idle':
            return '在发呆～ zZ'
        return self.text

    def bubble_sub(self):
        """第二行小字只跟着 Claude 的状态出现；临时说话（摸头、闲聊）、提醒休息时不出现。"""
        return None if self.speaking() or self.break_state.level else self.sub

    def effective_state(self):
        age = time.time() - self.state_since
        if self.state == 'waiting' and age > IDLE_AFTER:
            return 'idle'
        if self.state == 'oops':  # 一次工具调用失败：皱一下眉，然后接着想办法
            return 'error' if age < OOPS_S else 'thinking'
        return self.state

    def speaking(self):
        return self.speech is not None and time.time() < self.speech[1]

    def say(self, text, seconds, sprite=None):
        self.speech = (text, time.time() + seconds, sprite)
        self.hop_at = time.time()

    def current_pose(self, t):
        """现在该用哪张立绘：(文件名, 是否翻转, 落在线上的比例)。"""
        speech = self.speech[2] if self.speaking() else None
        state, age = self.effective_state(), t - self.state_since
        if self.roam and self.motion:
            pose = choose_pose(self.motion, t, state, age, remind=self.break_state.level, speech=speech,
                               swing=self.swing.direction)
            self.posture, pose = settle(self.posture, pose, t)   # 站坐之间先播过渡帧
            return pose.file, pose.flip, pose.anchor
        if self.drag is not None and self.moved:  # 被拎起来了：手脚垂着；甩得快，身体往反方向歪
            if self.swing.direction:
                return 'held-swing', self.swing.direction > 0, 1.0  # 立绘画的是往左甩（身体落在右边），往右甩就翻过来
            return 'held', False, 1.0
        if speech:
            return REACTIONS.get(speech) or SPRITES.get(speech, 'smile'), False, 1.0
        if self.break_state.level:
            return f'remind-{self.break_state.level}', False, 1.0
        if state == 'idle':  # 等你太久：打瞌睡，头一点一点
            return f'sleep-{1 + int(t / SLEEP_S) % 2}', False, 1.0
        return fixed_sprite(state, age), False, 1.0   # 刚回复完敲一会儿门，之后安静地等你

    def place_window(self, x=None, y=None):
        if x is None:
            x, y = self.settings.get('x'), self.settings.get('y')
        if not isinstance(x, int) or not isinstance(y, int):
            # 第一次打开：主显示器（坐标原点所在的那块）工作区的右下角
            _, (left, top, right, bottom) = monitor_at(0, 0)
            x, y = right - (self.W + self.char_w) // 2 - round(40 * self.scale), bottom - self.H
        # 拉回到窗口中心所在（或最近）的那块显示器里，副屏拔掉之后也不会跑到看不见的地方
        (left, top, right, bottom), _ = monitor_at(x + self.W // 2, y + self.H // 2)
        side = (self.W - self.char_w) // 2   # 窗口两边透明的部分可以伸出屏幕，角色本身不出屏
        x = min(max(left - side, x), right - self.W + side)
        y = min(max(top, y), bottom - self.H)
        self.root.geometry(f'{self.W}x{self.H}+{x}+{y}')

    def put_window(self, x, y):
        """坐窗口时：把窗口摆到"脚底（坐着时是屁股）落在 (x, y)"的位置。"""
        pos = (round(x - self.W / 2), round(y - self.floor))
        if pos != self.win_pos:
            self.win_pos = pos
            self.root.geometry(f'{self.W}x{self.H}+{pos[0]}+{pos[1]}')

    def update_span(self):
        """窗口挪动后，重新算窗口里哪一段在屏幕上（只在窗口位置变了时才查显示器）。"""
        if self.roam and self.win_pos:
            x, y = self.win_pos
        else:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        if (x, y) != self.span_at:
            self.span_at = (x, y)
            monitor, _ = monitor_at(x + self.W // 2, y + self.floor)
            self.span = visible_span(x, self.W, monitor)

    def feet(self):
        """脚底中点的屏幕坐标。"""
        if self.roam and self.motion:
            return round(self.motion.x), round(self.motion.y)
        return self.root.winfo_x() + self.W // 2, self.root.winfo_y() + self.floor

    def make_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, font=self.font)
        size_menu = tk.Menu(self.menu, tearoff=0, font=self.font)
        self.size_var = tk.StringVar(value=self.settings.get('size', '中'))
        for name in SIZES:
            size_menu.add_radiobutton(label=name, variable=self.size_var, value=name, command=self.resize)
        self.menu.add_cascade(label='大小', menu=size_menu)
        self.roam_var = tk.BooleanVar(value=self.roam)
        self.menu.add_checkbutton(label='坐在窗口上', variable=self.roam_var, command=self.toggle_roam)
        self.follow_var = tk.BooleanVar(value=self.following)
        self.menu.add_checkbutton(label='自动跟着当前窗口', variable=self.follow_var, command=self.toggle_follow)
        self.hide_var = tk.BooleanVar(value=self.settings.get('hideOnFocus', False))
        self.menu.add_checkbutton(label='切到 Claude 时自动隐藏', variable=self.hide_var, command=self.toggle_hide)
        self.sound_var = tk.BooleanVar(value=self.settings.get('sound', True))
        self.menu.add_checkbutton(label='提示音', variable=self.sound_var, command=self.toggle_sound)
        self.chat_var = tk.BooleanVar(value=self.settings.get('chat', True) is not False)
        self.menu.add_checkbutton(label='闲聊', variable=self.chat_var, command=self.toggle_chat)
        # 状态栏语言：插件那边读 settings.json 的 lang，下一次刷新状态栏时生效
        lang_menu = tk.Menu(self.menu, tearoff=0, font=self.font)
        self.lang_var = tk.StringVar(value=self.settings.get('lang', 'auto'))
        for value, label in (('auto', '跟随系统 / Auto'), ('zh', '中文'), ('en', 'English')):
            lang_menu.add_radiobutton(label=label, variable=self.lang_var, value=value, command=self.set_lang)
        self.menu.add_cascade(label='状态栏语言 / Language', menu=lang_menu)
        self.menu.add_separator()
        self.break_var = tk.BooleanVar(value=self.break_cfg.enabled)
        self.menu.add_checkbutton(label='休息提醒', variable=self.break_var, command=self.toggle_break)
        work_menu = tk.Menu(self.menu, tearoff=0, font=self.font)
        self.work_var = tk.StringVar(value=f'{self.break_cfg.work_min:g}')
        for minutes in (30, 45, 50, 60, 90):
            work_menu.add_radiobutton(label=f'{minutes} 分钟', variable=self.work_var, value=str(minutes),
                                      command=self.set_work_minutes)
        self.menu.add_cascade(label='多久提醒一次', menu=work_menu)
        preview_menu = tk.Menu(self.menu, tearoff=0, font=self.font)
        for level, name in ((1, '温柔'), (2, '催促'), (3, '急了')):
            preview_menu.add_command(label=name, command=lambda lv=level: self.preview_break(lv))
        self.menu.add_cascade(label='试一下提醒', menu=preview_menu)
        self.menu.add_command(label='我去休息了', command=self.acknowledge_break)
        self.menu.add_separator()
        self.menu.add_command(label='退出桌宠', command=self.root.destroy)

    def resize(self):
        self.settings['size'] = self.size_var.get()
        save_settings(self.settings)
        self.rebuild()

    def rebuild(self):
        """换大小、换缩放后重建；脚底位置不变，变大后超出屏幕就挪回来（坐窗口时按她的位置重新摆）。"""
        bottom_x, bottom_y = self.feet()
        self.build()
        if self.roam and self.motion:
            self.put_window(self.motion.x, self.motion.y)
        else:
            self.place_window(bottom_x - self.W // 2, bottom_y - self.floor)

    def check_dpi(self):
        """她落在缩放不同的另一块屏上了：按那块屏的缩放重建。拖着、在空中的时候不换，免得大小一路跳。"""
        if self.drag is not None or (self.roam and self.motion and self.motion.mode != 'perch'):
            return
        x, y = self.feet()
        scale = dpi.scale_at(x, y - 1)
        if abs(scale - self.scale) > 0.01:
            self.scale = scale
            self.rebuild()

    def go_home(self):
        """坐窗口时：回到当前窗口上，重新开始跟着窗口。不坐窗口时：回到主显示器右下角（第一次打开时的位置）。"""
        if self.roam:
            self.set_following(True)
            return
        self.settings.pop('x', None)
        self.settings.pop('y', None)
        save_settings(self.settings)
        self.place_window()
        self.check_dpi()

    def toggle_roam(self):
        self.roam = self.roam_var.get()
        self.settings['roam'] = self.roam
        if not self.roam:  # 不坐窗口了：就停在她现在的位置，回到置顶
            self.settings['x'], self.settings['y'] = self.root.winfo_x(), self.root.winfo_y()
        save_settings(self.settings)
        self.motion = None  # 打开时下一帧重新坐到当前窗口（或屏幕底边）上
        self.rebuild()      # 窗口高度不一样（要不要给垂下来的腿留地方）
        self.posture = Posture()
        self.z = None

    def set_following(self, on, announce=False):
        self.following = on
        self.settings['follow'] = on
        save_settings(self.settings)
        self.follow_var.set(on)
        if announce:
            self.say('我就待在这儿啦～', 3.0)

    def toggle_follow(self):
        self.set_following(self.follow_var.get())

    def toggle_hide(self):
        self.settings['hideOnFocus'] = self.hide_var.get()
        save_settings(self.settings)

    def toggle_sound(self):
        self.settings['sound'] = self.sound_var.get()
        save_settings(self.settings)

    def toggle_chat(self):
        self.settings['chat'] = self.chat_var.get()
        save_settings(self.settings)

    def set_lang(self):
        self.settings['lang'] = self.lang_var.get()
        save_settings(self.settings)

    def toggle_break(self):
        self.settings['breakEnabled'] = self.break_var.get()
        save_settings(self.settings)
        self.break_cfg = BreakConfig.from_settings(self.settings)
        self.break_state = BreakState()  # 开关一下就重新计时；正在提醒的话也就此作罢
        self.pending_break = None        # 全屏期间攒下的提醒也一并作废

    def set_work_minutes(self):
        self.settings['breakWork'] = float(self.work_var.get())
        save_settings(self.settings)
        self.break_cfg = BreakConfig.from_settings(self.settings)

    # ---------- 声音、说话、休息提醒 ----------
    def play(self, name):
        if not self.sound_var.get():
            return
        try:
            winsound.PlaySound(os.path.join(SOUNDS, name + '.wav'),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass

    def wake(self):
        """她在打瞌睡（等你太久）的话把她叫醒：当作你刚回来，重新开始计时。"""
        if self.effective_state() == 'idle':
            self.state_since = time.time()

    def react(self, kind):
        """摸头、戳、连戳生气、哼：说一句，换上对应的立绘（'huff' 不换）。"""
        self.wake()
        line = pick(self.lines.get(kind) or [''], '', random)
        self.say(line, REACT_S[kind], kind if kind in REACTIONS else None)

    def on_break_event(self, ev):
        self.remind_text = pick_line(ev.level, is_night(time.localtime().tm_hour), last=self.remind_text)
        self.speech = None
        self.hop_at = time.time()
        self.play('error' if ev.level >= 3 else 'done')

    def acknowledge_break(self):
        self.break_state = acknowledge(self.break_state, time.time())
        self.pending_break = None        # 已经去休息了，攒着的提醒不用再出
        self.say(random.choice(ACK_LINES), 3.5, 'idle')

    def preview_break(self, level):
        now = time.time()
        self.break_state = replace(self.break_state, level=level, reminded_at=now, last_event=now)
        self.on_break_event(BreakEvent(level))

    def watch_break(self):
        try:
            if self.break_cfg.enabled:  # 关掉的时候不检查；"试一下提醒"的预览会留到你点她为止
                self.break_state, ev = break_step(self.break_state, time.time(), idle_seconds(), self.break_cfg)
                if ev and self.fullscreen:
                    self.pending_break = ev   # 全屏的时候不打扰，退出全屏再提醒
                elif ev:
                    self.on_break_event(ev)
        except Exception:
            log_error()
        finally:
            self.root.after(2000, self.watch_break)

    def watch_chat(self):
        """闲聊：当天第一次见面问好、饭点提醒吃饭、平时偶尔说一句。只在等你说话、没在说别的时候开口。"""
        try:
            now = time.time()
            local = time.localtime(now)
            quiet = (not self.chat_var.get() or self.hidden or self.effective_state() != 'waiting'
                     or self.break_state.level or self.speaking())
            ctx = Context(now=now, date=time.strftime('%Y-%m-%d', local), hour=local.tm_hour, quiet=bool(quiet),
                          project=self.project)
            self.chatter, line = next_line(self.chatter, ctx, self.lines, random)
            if line:
                self.say(line, CHAT_SHOW_S)
        except Exception:
            log_error()
        finally:
            self.root.after(5000, self.watch_chat)

    # ---------- 鼠标 ----------
    def on_press(self, e):
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        self.press_at, self.moved = (e.x_root, e.y_root), False
        self.drag_samples = []

    def on_drag(self, e):
        if not self.drag:
            return
        if abs(e.x_root - self.press_at[0]) + abs(e.y_root - self.press_at[1]) > 4:
            self.moved = True
        if self.moved:  # 手抖一下不算拖：单击时窗口不会跟着挪一两个像素
            x, y = e.x_root - self.drag[0], e.y_root - self.drag[1]
            self.root.geometry(f'+{x}+{y}')
            feet = (x + self.W / 2, y + self.floor)
            self.drag_samples = (self.drag_samples + [(time.time(), *feet)])[-6:]
            if self.roam and self.motion:
                self.motion = hold(self.motion, *feet)  # 她被你拎起来了：位置完全跟着光标走
            self.wake()  # 拎起来就醒了

    def throw_velocity(self):
        """最近一小段拖动的速度 (水平, 竖直)，像素/秒；松手时按这个甩出去。"""
        samples = [s for s in self.drag_samples if time.time() - s[0] < 0.12]
        if len(samples) < 2 or samples[-1][0] - samples[0][0] < 0.02:
            return 0.0, 0.0
        dt = samples[-1][0] - samples[0][0]
        limit = THROW_MAX * self.scale
        return tuple(max(-limit, min(limit, (samples[-1][i] - samples[0][i]) / dt)) for i in (1, 2))

    def on_release(self, e):
        self.drag = None
        self.swing = Swing()
        if self.moved and self.roam and self.motion:
            if self.motion.mode == 'held':
                self.motion = release(self.motion, *self.throw_velocity())  # 松手：自由落体，落在下面第一块地板上
            if self.following:
                self.set_following(False, announce=True)  # 你把她放在哪儿，她就待在哪儿，不会自己跑回去
            self.win_pos = None
        elif self.moved:
            self.settings['x'], self.settings['y'] = self.root.winfo_x(), self.root.winfo_y()
            save_settings(self.settings)
            self.hop_at = time.time()  # 放下来时轻轻蹦一下
        elif self.break_state.level:
            self.acknowledge_break()  # 提醒的时候点她一下，就是"我去休息了"
        else:
            self.touch = on_click(self.touch, time.time())  # 等一下，确认不是双击再算戳

    def on_motion(self, e):
        """鼠标在她身上（没按键）移动：在头上来回划就是摸头。"""
        if self.drag is not None:
            return
        top, h = self.sprite_box
        in_head = h > 0 and top <= e.y <= top + HEAD_SHARE * h
        self.touch, r = on_move(self.touch, time.time(), e.x, in_head)
        if r:
            self.react(r)

    def on_leave(self):
        """鼠标离开她：摸头的轨迹作废，下次进来不会接着上次的坐标算。"""
        self.touch, _ = on_move(self.touch, time.time(), 0, False)

    def on_double(self, e):
        # 双击：把 Claude 窗口叫到前台（并取消等着确认的那次单击）
        self.touch = touch_double(self.touch)
        hwnd = find_claude_window()
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)

    # ---------- /pet 命令 ----------
    def check_command(self):
        """读插件写来的 /pet 命令；同一条只执行一次，太旧的不执行。读到写了一半的文件就等下次。"""
        try:
            with open(COMMAND, encoding='utf-8') as f:
                data = json.load(f)
            cid = str(data.get('id', ''))
            ts = float(data.get('ts', 0))
            args = str(data.get('args', ''))
        except (FileNotFoundError, ValueError, TypeError, AttributeError):
            return
        if not cid or cid == self.last_command:
            return
        self.last_command = cid
        if ts < self.started_ms - 1000:   # 桌宠启动之前写的命令（上一个桌宠的），不重放；留 1 秒余量给"刚启动就写入"的 show
            return
        if time.time() * 1000 - ts > COMMAND_TTL_MS:
            return
        try:
            self.run_command(args)
        except Exception:
            log_error()

    def run_command(self, args):
        cmd = parse_command(args)
        if isinstance(cmd, str):
            self.forced_hidden = False
            self.say(cmd, 5.0)
            return
        name, value = cmd.name, cmd.value
        if name != 'hide':
            self.forced_hidden = False
        if name == 'show':
            self.say('我在这儿～', 3.0)
        elif name == 'hide':
            self.forced_hidden = True
        elif name == 'home':
            self.go_home()
            self.say('我回来啦～', 3.0)
        elif name == 'quit':
            self.root.destroy()
        elif name == 'rest':
            self.acknowledge_break()
        elif name == 'size':
            self.size_var.set(value)
            self.resize()
            self.say(f'变成{value}号啦', 3.0)
        elif name in ('follow', 'roam', 'sound', 'chat'):
            var, toggle, label = {
                'follow': (self.follow_var, self.toggle_follow, '自动跟着当前窗口'),
                'roam': (self.roam_var, self.toggle_roam, '坐在窗口上'),
                'sound': (self.sound_var, self.toggle_sound, '提示音'),
                'chat': (self.chat_var, self.toggle_chat, '闲聊'),
            }[name]
            var.set(value)
            toggle()
            self.say(f'{label}已{"打开" if value else "关闭"}', 3.0)
        elif name == 'break':
            self.break_var.set(value)
            self.toggle_break()
            self.say(f'休息提醒已{"打开" if value else "关闭"}', 3.0)
        elif name == 'break-minutes':
            self.work_var.set(str(value))
            self.set_work_minutes()
            self.say(f'以后每 {value} 分钟提醒你休息', 3.0)
        elif name == 'break-test':
            self.preview_break(1)

    # ---------- 循环 ----------
    def poll(self):
        try:
            now, items = time.time(), read_sessions()
            sound, self.sound_seen = new_sound(items, self.sound_seen)  # 哪个会话干完了都响，不只是正在跟的那个
            if sound:
                self.play(sound)
            summary = summarize(items, now)   # 跟哪个会话；别的会话的情况写进第二行小字
            self.sub = summary.sub
            main = summary.main
            # 说自己在忙、却很久没动静的会话（卡住了，或者 Claude 被关掉时没来得及说再见）：当成在发呆，不再一直"思考中"
            quiet = main is not None and main.state in BUSY and not busy(main, now)
            if main and (main.key, main.mtime, quiet) != self.last_seen:
                self.last_seen = (main.key, main.mtime, quiet)
                state = 'waiting' if main.state == 'hello' or quiet else main.state
                state = state if state in SPRITES or state == 'oops' else 'waiting'
                changed = state != self.state
                self.state, self.text, self.project = state, main.text, main.project
                # 状态从什么时候开始按会话里记的算：换回一个早就干完的会话时不会重新敲门；只是换了文字也不重新计时
                if quiet:
                    self.state_since = main.mtime - IDLE_AFTER   # 直接打瞌睡
                else:
                    self.state_since = min(now, main.started) if main.since else (now if changed else self.state_since)
                if changed:
                    self.hop_at = now
            self.check_command()
        except Exception:
            log_error()
        finally:
            self.root.after(250, self.poll)

    def animate(self):
        try:
            self.frame()
        except Exception:
            log_error()
        finally:
            self.root.after(33, self.animate)

    def frame(self):
        t = time.time()
        dt = min(0.1, max(0.0, t - self.last_tick))
        self.last_tick = t
        if self.roam:
            self.update_motion(t, dt)
        self.touch, r = on_tick(self.touch, t)
        if r:
            self.react(r)
        if self.speech and not self.speaking():
            self.speech = None
        self.blink = step_blink(self.blink, t, random)
        if self.drag is not None and self.moved:
            self.swing = swing_step(self.swing, self.throw_velocity()[0], t, SWING_V * self.scale, SWING_HOLD_S)
        self.update_span()
        if (self.effective_state(), self.display_text(), self.bubble_sub(), self.span) != self.bubble_key:
            self.draw_bubble()  # 换了台词、小字，开始打瞌睡，或者挪到了屏幕边上
        file, flip, anchor = self.current_pose(t)
        blink = file in self.patches and eyes_closed(self.blink, t)
        since = t - self.hop_at
        hop = -HOP * math.sin(since / 0.28 * math.pi) if since < 0.28 else 0  # 换状态时小跳一下
        offset = round((bob_for(file, t) + hop) * self.scale)
        text = self.bubble_text
        if self.dots_base is not None:
            # “思考中…” 这类进行中的台词，后面的点点点一跳一跳
            text = self.dots_base + '.' * (int(t * 2.5) % 3 + 1)
        if not self.hidden:
            self.render((file, flip, blink), anchor, offset, text, t)
        if self.roam and self.motion and self.drag is None:
            self.put_window(self.motion.x, self.motion.y)

    def check_fullscreen(self):
        fg = foreground_frame()
        if fg is None:
            return False
        rect, cls, zoomed = fg
        monitor, _ = monitor_at(*self.feet())
        return is_fullscreen(rect, cls, monitor, zoomed)

    def watch_focus(self):
        try:
            self.fullscreen = self.check_fullscreen()
            auto_hide = (self.hide_var.get() and claude_is_foreground() and self.drag is None
                         and not self.break_state.level)  # 正在提醒休息的时候不躲
            should_hide = self.forced_hidden or self.fullscreen or auto_hide
            if should_hide and not self.hidden:
                self.root.withdraw()
                self.hidden = True
            elif not should_hide and self.hidden:
                self.root.deiconify()
                self.root.attributes('-topmost', True)
                self.hidden = False
                self.last_frame = None  # 重新显示后强制重画一帧
                self.win_pos = None
                self.z = None           # 层级也要重新设
            if not self.fullscreen and self.pending_break is not None:
                ev, self.pending_break = self.pending_break, None
                if self.break_state.level:   # 期间已经取消/确认过的就不补发了
                    self.on_break_event(ev)
            self.check_dpi()
        except Exception:
            log_error()
        finally:
            self.root.after(300, self.watch_focus)

    # ---------- 坐在窗口上 ----------
    def watch_surface(self):
        """每 0.25 秒：刷新所有地板，找出"当前窗口"（前台窗口稳定 SETTLE_S 秒才算，Alt+Tab 路过的不算），设好层级。"""
        try:
            if self.roam:
                now = time.time()
                self.work_areas = winapi.monitor_work_areas()
                self.surfaces = build_surfaces(winapi.visible_windows(os.getpid()), self.work_areas)
                win = winapi.foreground_window(os.getpid())
                if win is None:
                    self.candidate = None
                elif self.target_hwnd is None or win.hwnd == self.target_hwnd:
                    self.candidate = None
                    self.target_hwnd = win.hwnd
                elif self.candidate is None or self.candidate[0] != win.hwnd:
                    self.candidate = (win.hwnd, now)
                elif now - self.candidate[1] >= SETTLE_S:
                    self.target_hwnd, self.candidate = win.hwnd, None
                if self.target_hwnd and winapi.window_info(self.target_hwnd) is None:
                    self.target_hwnd = win.hwnd if win else None  # 当前窗口关了/最小化了
            self.apply_z()
        except Exception:
            log_error()
        finally:
            self.root.after(250, self.watch_surface)

    def apply_z(self):
        """坐在窗口上时贴着那个窗口（会被一起盖住），其余时候置顶。失败就退回置顶，只记一次日志。"""
        if self.hidden:
            return
        m = self.motion if self.roam else None
        want = choose_z(m.mode if m else None, m.support if m else None, self.break_state.level, self.speaking())
        if want != 'top' and want == self.z_failed:
            want = 'top'   # 这个窗口贴不上，别每 0.25 秒重试一遍
        elif want != self.z_failed:
            self.z_failed = None
        hwnd = self.layer.hwnd()
        if want == 'top':
            if self.z != 'top':
                winapi.set_topmost(hwnd)
                self.z = 'top'
            return
        if not winapi.place_above(hwnd, want):
            log_error(f'SetWindowPos 失败，退回置顶（窗口 {want}）')
            self.z_failed = want
            winapi.set_topmost(hwnd)
            self.z = 'top'
            return
        self.z = want

    def follow_surface(self):
        """"跟着当前窗口"时她该去的地方：当前窗口的顶边；上方放不下就是屏幕底边（限在这个窗口的左右范围内）。"""
        win = winapi.window_info(self.target_hwnd) if self.target_hwnd else None
        return choose_surface(win, self.work_areas[0], need_above=self.floor)

    def fresh_surfaces(self, follow):
        """地板列表。她脚下这块每帧都重新量一次，拖动窗口时她才不会慢半拍；窗口最大化、最小化了就从列表里消失，
        她就掉下去。"""
        surfaces = [s for s in self.surfaces if s.key != follow.key]
        support = self.motion.support if self.motion else None
        if isinstance(support, int) and support != follow.key:
            info = winapi.window_info(support)
            surfaces = [s for s in surfaces if s.key != support] + (build_surfaces([info], []) if info else [])
        return surfaces + [follow]

    def can_swing(self):
        """闲着等你的时候才偶尔晃晃腿。"""
        return (self.drag is None and not self.break_state.level and not self.speaking()
                and self.effective_state() == 'waiting')

    def update_motion(self, now, dt):
        follow = self.follow_surface()
        if self.motion is None:
            ground = choose_surface(None, self.work_areas[0], need_above=self.floor)
            self.motion = spawn(follow if self.following else ground, 0.8, now)  # 一开始坐在当前窗口靠右的位置
            return
        m = self.motion
        if self.following and m.mode == 'perch' and m.support != follow.key:
            m = hop_to(m, follow, now, self.scale)  # 你切到了别的窗口：她跳过去
        self.motion = tick(m, self.fresh_surfaces(follow), now, dt, scale=self.scale, can_swing=self.can_swing())

    def watch_claude(self):
        # Claude 关掉两分钟后，桌宠也自己退出
        if claude_running():
            self.last_alive = time.time()
        elif time.time() - self.last_alive > 120:
            self.root.destroy()
            return
        self.root.after(30000, self.watch_claude)


if __name__ == '__main__':
    try:
        Pet().root.mainloop()
    except Exception:
        log_error()
