# -*- coding: utf-8 -*-
"""
QQ群聊消息监控（Windows UIAutomation）
依赖：uiautomation
"""
import time
import platform
from collections import deque

if platform.system() == "Windows":
    import uiautomation as ua
else:
    ua = None


class QQMonitor:
    def __init__(self, group_names=None, dedup_size=200):
        self.group_names = set(group_names) if group_names else set()
        self.dedup_size = dedup_size
        self.seen_keys = deque(maxlen=dedup_size)
        self._last_status = None  # "online" / "offline"

    def find_qq_window(self):
        """查找QQ主窗口，找到返回控件，找不到返回 None"""
        if ua is None:
            return None
        # QQNT 新版
        for name in ["QQ", "QQNT"]:
            try:
                win = ua.WindowControl(Name=name)
                if win.Exists(maxSearchSeconds=1):
                    return win
            except Exception:
                pass
        # 旧版 QQ 按类名查找
        for class_name in ["TXGuiFoundation", "QQ_Dll_Default"]:
            try:
                win = ua.WindowControl(ClassName=class_name)
                if win.Exists(maxSearchSeconds=1):
                    return win
            except Exception:
                pass
        return None

    def is_running(self):
        """QQ是否已启动并登录"""
        return self.find_qq_window() is not None

    def get_current_chat_name(self, qq_window):
        """尝试获取当前打开的聊天窗口名称"""
        for subname in ["聊天标题", "群名", "会话名"]:
            try:
                ctrl = qq_window.TextControl(SubName=subname)
                if ctrl.Exists(maxSearchSeconds=0.3):
                    name = ctrl.Name
                    if name and name != "QQ":
                        return name
            except Exception:
                pass
        # 从窗口标题判断
        win_name = qq_window.Name or ""
        if win_name and win_name not in ("QQ", "QQNT"):
            return win_name
        return None

    def read_messages(self, qq_window):
        """读取当前QQ聊天窗口的消息列表"""
        messages = []

        for name in ["消息记录", "消息", "MessageList"]:
            msg_list = qq_window.ListControl(Name=name)
            if msg_list.Exists(maxSearchSeconds=0.5):
                break
        else:
            msg_list = qq_window.ListControl(SubName="消息记录")
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
                    if not sender:
                        sender = cname
                    else:
                        content = cname

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

        qq_window = self.find_qq_window()
        if not qq_window:
            if self._last_status != "offline":
                self._last_status = "offline"
            return new_messages, "offline"

        if self._last_status != "online":
            self._last_status = "online"

        # 不切换群聊，只读取当前窗口消息（不抢焦点）
        msgs = self.read_messages(qq_window)
        chat_name = self.get_current_chat_name(qq_window)

        for m in msgs:
            key = f"{chat_name}:{m['sender']}:{m['content']}"
            if key not in self.seen_keys:
                self.seen_keys.append(key)
                new_messages.append({
                    "platform": "QQ",
                    "group": chat_name or "",
                    "sender": m["sender"],
                    "content": m["content"],
                })

        return new_messages, "online"
