# -*- coding: utf-8 -*-
"""
微信群聊消息监控（Windows UIAutomation）
依赖：uiautomation
"""
import time
import platform
from collections import deque

if platform.system() == "Windows":
    import uiautomation as ua
else:
    ua = None


# ── 微信主窗口识别 ──
# 微信 3.x 主窗口类名固定为 WeChatMainWndForPC；
# 微信 4.x 换成 Qt 自绘窗口（mmui），类名随 Qt 版本变化，窗口标题仍为「微信」。
WECHAT_WINDOW_CLASSES = (
    "WeChatMainWndForPC",     # 微信 3.x
    "Qt51514QWindowIcon",     # 微信 4.x (Qt 5.15.14)
    "Qt5156QWindowIcon",      # 微信 4.x 变体
    "Qt5152QWindowIcon",
    "Qt5QWindowIcon",
)
WECHAT_WINDOW_NAMES = ("微信", "WeChat")

# UIA 子树节点数低于该阈值 → 窗口只是个空壳，说明控件树没暴露出来
UIA_SHELL_THRESHOLD = 6

WECHAT_BLOCKED_HINT = (
    "已找到微信窗口，但微信 4.x 没向系统暴露控件树，读不到任何消息。"
    "这是微信 4.0.5+ 的「按需暴露」策略：只有检测到无障碍客户端（讲述人）时才加载完整 UI。"
    "解决办法：① 完全退出微信 → ② 开启 Windows「讲述人」(Win+Ctrl+Enter) → "
    "③ 再登录微信登录并保持讲述人开启 5 分钟以上 → ④ 之后可关闭讲述人，微信会记住该状态"
)


def uia_node_count(win, max_depth=3, cap=500):
    """统计窗口下的 UIA 节点数，用于判断控件树是否真的暴露了"""
    count = 0

    def walk(ctrl, depth):
        nonlocal count
        if depth > max_depth or count >= cap:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for child in children:
            count += 1
            if count >= cap:
                return
            walk(child, depth + 1)

    try:
        walk(win, 0)
    except Exception:
        return -1
    return count


class WeChatMonitor:
    def __init__(self, group_names=None, dedup_size=200):
        self.group_names = set(group_names) if group_names else set()
        self.dedup_size = dedup_size
        self.seen_keys = deque(maxlen=dedup_size)
        self._last_status = None  # "online" / "offline"

    def find_wechat_window(self):
        """查找微信主窗口（兼容 3.x / 4.x），找到返回控件，找不到返回 None"""
        if ua is None:
            return None

        # 1) 类名 + 标题精确匹配（首选，避免误匹配 MuMu 等其它 Qt 窗口）
        for class_name in WECHAT_WINDOW_CLASSES:
            for win_name in WECHAT_WINDOW_NAMES:
                try:
                    win = ua.WindowControl(searchDepth=1, ClassName=class_name,
                                           Name=win_name)
                    if win.Exists(maxSearchSeconds=0.4):
                        return win
                except Exception:
                    pass

        # 2) 只按类名（3.x 窗口标题可能是「微信」，4.x 主窗口标题也是「微信」）
        for class_name in WECHAT_WINDOW_CLASSES:
            try:
                win = ua.WindowControl(searchDepth=1, ClassName=class_name)
                if win.Exists(maxSearchSeconds=0.3):
                    return win
            except Exception:
                pass

        # 3) 兜底：只按标题
        for win_name in WECHAT_WINDOW_NAMES:
            try:
                win = ua.WindowControl(searchDepth=1, Name=win_name)
                if win.Exists(maxSearchSeconds=0.4):
                    return win
            except Exception:
                pass

        return None

    def check(self):
        """检测微信，区分「没开」/「开了但控件树被屏蔽」/「可用」

        返回 (state, detail)，state ∈ {"not_found", "uia_blocked", "ok"}
        """
        win = self.find_wechat_window()
        if win is None:
            return "not_found", "未检测到微信窗口，请确保微信已启动并登录（窗口不能最小化）"

        nodes = uia_node_count(win)
        if 0 <= nodes < UIA_SHELL_THRESHOLD:
            return "uia_blocked", f"{WECHAT_BLOCKED_HINT}（当前 UIA 节点数={nodes}）"

        try:
            chat = self.get_current_chat_name(win)
        except Exception:
            chat = None
        detail = "已检测到微信窗口"
        if chat:
            detail += f"，当前聊天: {chat}"
        return "ok", detail

    def is_running(self):
        """微信是否已启动并登录"""
        return self.find_wechat_window() is not None

    def get_current_chat_name(self, wechat):
        """尝试获取当前打开的聊天窗口名称"""
        # 聊天窗口标题区域
        title = wechat.TextControl(Name="")
        # 尝试从顶部标题栏读取当前聊天名
        for selector in [
            lambda w: w.TextControl(SubName="聊天标题"),
            lambda w: w.TextControl(SubName=""),
        ]:
            try:
                ctrl = selector(wechat)
                if ctrl.Exists(maxSearchSeconds=0.3):
                    name = ctrl.Name
                    if name and name != "微信":
                        return name
            except Exception:
                pass
        # 退而求其次：从窗口标题判断
        win_name = wechat.Name or ""
        if win_name and win_name != "微信":
            return win_name
        return None

    def read_messages(self, wechat):
        """读取当前聊天窗口的消息列表"""
        messages = []

        # 尝试多种控件名定位消息列表
        for name in ["消息", "聊天消息", "MessageListView"]:
            msg_list = wechat.ListControl(Name=name)
            if msg_list.Exists(maxSearchSeconds=0.5):
                break
        else:
            msg_list = wechat.ListControl(SubName="消息")
            if not msg_list.Exists(maxSearchSeconds=0.5):
                return messages

        for item in msg_list.GetChildren():
            try:
                children = item.GetChildren()
                sender = ""
                content = ""

                for child in children:
                    cname = child.Name or ""
                    if not cname:
                        continue
                    # 第一个非空文本通常是发送者，第二个是内容
                    if not sender:
                        sender = cname
                    else:
                        content = cname

                # 兜底：直接取 item 自身 Name
                if not content and item.Name:
                    content = item.Name

                if content:
                    messages.append({
                        "sender": sender,
                        "content": content,
                    })
            except Exception:
                continue

        return messages

    def poll(self):
        """轮询一次，返回新消息列表"""
        new_messages = []

        wechat = self.find_wechat_window()
        if not wechat:
            if self._last_status != "offline":
                self._last_status = "offline"
            return new_messages, "offline"

        # 窗口在，但微信 4.x 没暴露控件树 → 视为不可用，读不到消息
        if self._last_status != "online":
            nodes = uia_node_count(wechat)
            if 0 <= nodes < UIA_SHELL_THRESHOLD:
                self._last_status = "blocked"
                return new_messages, "blocked"
            self._last_status = "online"

        # 不切换群聊，只读取当前窗口消息（不抢焦点）
        msgs = self.read_messages(wechat)

        # 当前聊天名（用于输出，可选）
        chat_name = self.get_current_chat_name(wechat)

        for m in msgs:
            key = f"{chat_name}:{m['sender']}:{m['content']}"
            if key not in self.seen_keys:
                self.seen_keys.append(key)
                new_messages.append({
                    "platform": "微信",
                    "group": chat_name or "",
                    "sender": m["sender"],
                    "content": m["content"],
                })

        return new_messages, "online"
