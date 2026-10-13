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


class WeChatMonitor:
    def __init__(self, group_names=None, dedup_size=200):
        self.group_names = set(group_names) if group_names else set()
        self.dedup_size = dedup_size
        self.seen_keys = deque(maxlen=dedup_size)
        self._last_status = None  # "online" / "offline"

    def find_wechat_window(self):
        """查找微信主窗口，找到返回控件，找不到返回 None"""
        if ua is None:
            return None
        for class_name in ["WeChatMainWndForPC"]:
            try:
                win = ua.WindowControl(ClassName=class_name)
                if win.Exists(maxSearchSeconds=1):
                    return win
            except Exception:
                pass
        return None

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

        if self._last_status != "online":
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
