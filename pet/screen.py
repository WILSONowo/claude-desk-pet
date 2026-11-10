"""全屏判断、层级选择。纯函数，窗口和显示器的矩形由 pet.pyw 取好传进来，都是 (左, 上, 右, 下)。"""
from __future__ import annotations

DESKTOP_CLASSES = frozenset({'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd'})


def is_fullscreen(rect: tuple, cls: str, monitor: tuple, zoomed: bool = False) -> bool:
    """前台窗口是不是盖满了这块显示器（连任务栏也盖住）。桌面本身不算；最大化的窗口不算（任务栏自动隐藏时它也会盖满）。"""
    if cls in DESKTOP_CLASSES or zoomed:
        return False
    left, top, right, bottom = rect
    m_left, m_top, m_right, m_bottom = monitor
    return left <= m_left and top <= m_top and right >= m_right and bottom >= m_bottom


def choose_z(mode: str | None, support: object, remind: int, speaking: bool):
    """她该在哪一层：'top' 置顶；否则返回她坐着的那个窗口的句柄，贴在它正上方一层（被别的窗口盖住时一起被盖住）。
    mode/support 来自 motion.Motion；不坐窗口时 mode 是 None。提醒休息、说话的时候总是置顶，免得被挡住。"""
    if mode != 'perch' or remind or speaking:
        return 'top'
    return support if isinstance(support, int) else 'top'
