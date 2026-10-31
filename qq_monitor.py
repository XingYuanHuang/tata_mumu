# -*- coding: utf-8 -*-
"""
QQ群聊消息监控（Windows UIAutomation）
依赖：uiautomation

支持两种 QQ：
- QQNT (9.x, Electron)：类名 Chrome_WidgetWin_1。群聊可能是**独立窗口**，
  标题就是群名（如「塔塔夺金MVP」），而不是 "QQ"。
  消息列表控件是 WindowControl(Name="消息列表")，**不是 ListControl**。
- 旧版 QQ (TXGuiFoundation)：消息列表用 ListControl。
"""
import os
import time
import ctypes
import platform
from collections import deque

if platform.system() == "Windows":
    import uiautomation as ua
else:
    ua = None

from monitor import uia_node_count, UIA_SHELL_THRESHOLD

QQ_BLOCKED_HINT = (
    "未检测到 QQ 窗口。请打开 QQ 并点开要监控的群聊窗口"
    "（群聊独立窗口或 QQ 主面板里的会话都行），且不能最小化"
)

# QQNT 主面板标题
QQ_MAIN_TITLES = ("QQ", "QQNT")

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pid_to_exe_name(pid):
    """用 kernel32 直接查进程可执行文件名（避免依赖 tasklist / WMI）"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return None


class QQMonitor:
    # 控件树巡检间隔（秒）：在线状态下也定期数节点，发现被 QQNT 熄灯就重新点亮
    PROBE_INTERVAL = 300
    # 重新激活（SetActive 抢焦点）的最小间隔，避免反复抢焦点
    ACTIVATE_COOLDOWN = 300
    # 连续读空 N 次且节点数正常 → 也强制激活一次（消息列表可能被虚拟化剪掉了）
    EMPTY_REACTIVATE = 8

    def __init__(self, group_names=None, dedup_size=200, target_group=None,
                 auto_activate=True, on_log=None):
        self.group_names = set(group_names) if group_names else set()
        self.dedup_size = dedup_size
        # 指定要监控的群名（窗口标题包含它即可），None = 自动挑第一个聊天窗口
        self.target_group = target_group
        # QQNT 后台窗口的 UIA 树是懒加载的（实测：后台只有 8 个节点，
        # 激活一次后变 320 个节点且之后一直保持）。开启后会在连接不上时
        # 自动激活一次 QQ 窗口来"点亮"控件树，成功后不再抢焦点。
        # 注意：QQNT 后台放久了（1~2 小时）还会再次"熄灯"（Electron 节流），
        # 所以 poll() 里会定期巡检节点数 + 连续读空时重新激活，不是只激活一次。
        self.auto_activate = auto_activate
        self.on_log = on_log or (lambda s: None)
        self.seen_keys = deque(maxlen=dedup_size)
        self._last_status = None  # "online" / "blocked" / "offline"
        self._last_probe = 0.0    # 上次节点数巡检时间
        self._last_activate = 0.0 # 上次激活（SetActive）时间
        self._empty_polls = 0     # 连续读空次数

    # ── 窗口查找 ──

    def _qq_candidate_windows(self):
        """列出属于 QQ.exe / QQNT.exe 进程的顶层可见窗口

        uiautomation 的桌面控件树本来就只含可见窗口，所以这里不用再判断 IsWindowVisible。
        只看 ClassName=Chrome_WidgetWin_1 的窗口，再按进程名过滤（否则会混进 Chrome 浏览器）。
        """
        if ua is None:
            return []
        candidates = []
        try:
            desktop = ua.GetRootControl()
            children = desktop.GetChildren()
        except Exception:
            return []
        for w in children:
            try:
                if (w.ClassName or "") != "Chrome_WidgetWin_1":
                    continue
                exe = _pid_to_exe_name(w.ProcessId)
                if exe and exe.lower() in ("qq.exe", "qqnt.exe"):
                    candidates.append(w)
            except Exception:
                continue
        return candidates

    def _has_message_list(self, w):
        try:
            return w.WindowControl(searchDepth=12, Name="消息列表").Exists(maxSearchSeconds=0.3)
        except Exception:
            return False

    def _pick_chat_window(self, candidates):
        """从候选里挑出可用的聊天窗口（有「消息列表」）"""
        chat_windows = [w for w in candidates if self._has_message_list(w)]
        if not chat_windows:
            return None

        # 1) 指定了群名 → 标题匹配的优先
        if self.target_group:
            for w in chat_windows:
                try:
                    if self.target_group in (w.Name or ""):
                        return w
                except Exception:
                    pass

        # 2) 群聊独立窗口（标题不是 "QQ"）优先
        for w in chat_windows:
            try:
                if (w.Name or "") not in QQ_MAIN_TITLES:
                    return w
            except Exception:
                pass

        # 3) 主面板里的会话
        return chat_windows[0]

    def _activate_candidates(self, candidates):
        """逐个激活候选窗口来"点亮"QQNT 懒加载控件树（有冷却限制）"""
        now = time.time()
        if now - self._last_activate < self.ACTIVATE_COOLDOWN:
            return False
        self._last_activate = now
        for w in candidates:
            try:
                self.on_log(f"激活 QQ 窗口「{w.Name}」重新点亮控件树（后台太久被 Electron 熄灯了）")
                w.SetActive()
                time.sleep(0.7)
            except Exception:
                continue
        return True

    def find_qq_window(self):
        """查找可用的 QQ 聊天窗口（群聊独立窗口优先于主面板）"""
        if ua is None:
            return None

        candidates = self._qq_candidate_windows()
        if not candidates:
            # 兼容旧版 QQ
            for class_name in ("TXGuiFoundation", "QQ_Dll_Default"):
                try:
                    win = ua.WindowControl(searchDepth=1, ClassName=class_name)
                    if win.Exists(maxSearchSeconds=0.4):
                        return win
                except Exception:
                    pass
            return None

        chat = self._pick_chat_window(candidates)
        if chat is not None:
            return chat

        # 没找到聊天窗口 → 多半是 QQNT 的懒加载控件树还没点亮，激活一次再试
        if self.auto_activate:
            self._activate_candidates(candidates)
            chat = self._pick_chat_window(self._qq_candidate_windows())
            if chat is not None:
                return chat

        # 回退：主面板 / 任意 QQ 窗口
        for w in candidates:
            try:
                if (w.Name or "") in QQ_MAIN_TITLES:
                    return w
            except Exception:
                pass
        return candidates[0] if candidates else None

    def is_running(self):
        """QQ是否已启动并登录"""
        return self.find_qq_window() is not None

    def check(self):
        """检测QQ，返回 (state, detail)，state ∈ {"not_found", "uia_blocked", "ok"}"""
        win = self.find_qq_window()
        if win is None:
            return "not_found", QQ_BLOCKED_HINT

        nodes = uia_node_count(win)
        if 0 <= nodes < UIA_SHELL_THRESHOLD:
            return "uia_blocked", (
                f"已找到QQ窗口但控件树是空的（节点数={nodes}），"
                "请把QQ窗口切到前台（不要最小化）后重试"
            )

        try:
            chat = self.get_current_chat_name(win)
        except Exception:
            chat = None
        try:
            count = len(self.read_messages(win))
        except Exception:
            count = 0
        detail = f"已检测到QQ窗口（{win.Name}）"
        if chat:
            detail += f"，当前会话: {chat}"
        detail += f"，可见消息 {count} 条"
        return "ok", detail

    def get_current_chat_name(self, qq_window):
        """获取当前聊天窗口名称（群名）"""
        win_name = qq_window.Name or ""
        # 群聊独立窗口：窗口标题就是群名
        if win_name and win_name not in QQ_MAIN_TITLES:
            return win_name
        # 主面板：从标题控件读
        for subname in ["聊天标题", "群名", "会话名"]:
            try:
                ctrl = qq_window.TextControl(SubName=subname)
                if ctrl.Exists(maxSearchSeconds=0.3):
                    name = ctrl.Name
                    if name and name not in QQ_MAIN_TITLES:
                        return name
            except Exception:
                pass
        return None

    # ── 消息读取 ──

    def _read_messages_qqnt(self, qq_window):
        """QQNT：消息列表是 WindowControl(Name="消息列表")

        实测结构（每条消息）：
            GroupControl(name='')                       ← 消息行
              └ GroupControl
                  └ GroupControl                         ← 两个子节点
                      ├ GroupControl(name='发送者昵称')  ← 发送者
                      │   └ GroupControl                  ← 头像
                      └ GroupControl                      ← 气泡
                          └ GroupControl
                              └ TextControl(name='正文')

        坑：消息列表的嵌套层数**不稳定**（有时 消息列表→行 只有 1 层，
        有时 消息列表→容器→行 有 2 层），写死层数会漏读。
        所以这里用「特征匹配」递归：只要某节点的子节点里
        第一个是带名字的 GroupControl（发送者）、并且后面还有个含
        TextControl 的兄弟节点（气泡），就认定为一条消息。

        顺序：DOM 里是从新到旧（最后一条在最下面），返回前反转成时间正序。
        """
        msg_list = qq_window.WindowControl(searchDepth=12, Name="消息列表")
        if not msg_list.Exists(maxSearchSeconds=0.5):
            return []

        found = []  # [(sender, content)]
        MAX_ROWS = 400

        def try_parse(node):
            """node 若是一条消息，返回 (sender, content)，否则 None"""
            try:
                kids = node.GetChildren()
            except Exception:
                return None
            if len(kids) < 2:
                return None
            sender = (kids[0].Name or "").strip()
            if not sender:
                return None
            texts = self._collect_texts(kids[1])
            if not texts:
                return None
            return sender, "".join(texts).strip()

        def walk(node, depth=0):
            if depth > 8 or len(found) >= MAX_ROWS:
                return
            try:
                kids = node.GetChildren()
            except Exception:
                return
            for k in kids:
                m = try_parse(k)
                if m:
                    found.append(m)
                else:
                    walk(k, depth + 1)

        walk(msg_list)
        return [{"sender": s, "content": c} for s, c in reversed(found)]

    @staticmethod
    def _collect_texts(ctrl, max_depth=4):
        """递归收集控件下所有非空 TextControl 文本"""
        texts = []

        def collect(c, depth=0):
            if depth > max_depth:
                return
            try:
                children = c.GetChildren()
            except Exception:
                return
            for ch in children:
                try:
                    if ch.ControlTypeName == "TextControl":
                        t = (ch.Name or "").strip()
                        if t:
                            texts.append(t)
                    else:
                        collect(ch, depth + 1)
                except Exception:
                    continue

        collect(ctrl)
        return texts

    def _read_messages_legacy(self, qq_window):
        """旧版 QQ：消息列表是 ListControl"""
        messages = []
        msg_list = None
        for name in ["消息记录", "消息", "MessageList"]:
            ctrl = qq_window.ListControl(Name=name)
            if ctrl.Exists(maxSearchSeconds=0.4):
                msg_list = ctrl
                break
        if msg_list is None:
            msg_list = qq_window.ListControl(SubName="消息记录")
            if not msg_list.Exists(maxSearchSeconds=0.4):
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
                    messages.append({"sender": sender, "content": content})
            except Exception:
                continue
        return messages

    def read_messages(self, qq_window):
        """读取当前QQ聊天窗口可见的消息列表"""
        messages = self._read_messages_qqnt(qq_window)
        if messages:
            return messages
        return self._read_messages_legacy(qq_window)

    # ── 轮询 ──

    def _probe_tree(self, qq_window):
        """检查控件树是否还活着。返回 True=正常，False=被熄灯（节点数过少）。

        在线状态下也定期巡检（PROBE_INTERVAL）：QQNT 后台放久了 Electron 会把
        控件树休眠掉，不巡检的话会一直显示"在线"却读不到任何消息。
        """
        nodes = uia_node_count(qq_window)
        if nodes < 0 or nodes >= UIA_SHELL_THRESHOLD:
            return True
        self.on_log(f"QQ 窗口控件树已休眠（节点数={nodes}），尝试重新点亮")
        if self.auto_activate:
            self._activate_candidates([qq_window])
            time.sleep(0.5)
            fresh = self.find_qq_window()
            nodes2 = uia_node_count(fresh) if fresh else -1
            if nodes2 < 0 or nodes2 >= UIA_SHELL_THRESHOLD:
                self.on_log("QQ 控件树已重新点亮")
                return True
            self.on_log(f"重新点亮失败（节点数={nodes2}），请手动把 QQ 窗口切到前台一次")
        return False

    def poll(self):
        """轮询一次，返回 (新消息列表, 状态)"""
        new_messages = []

        qq_window = self.find_qq_window()
        if not qq_window:
            if self._last_status != "offline":
                self._last_status = "offline"
            return new_messages, "offline"

        now = time.time()
        # 状态非 online 时必须先确认树活着；online 时每隔 PROBE_INTERVAL 巡检一次
        if self._last_status != "online" or now - self._last_probe >= self.PROBE_INTERVAL:
            self._last_probe = now
            if not self._probe_tree(qq_window):
                self._empty_polls = 0
                self._last_status = "blocked"
                return new_messages, "blocked"
            self._last_status = "online"

        # 不切换群聊，只读取当前窗口消息（不抢焦点）
        msgs = self.read_messages(qq_window)
        chat_name = self.get_current_chat_name(qq_window)

        if not msgs:
            # 树活着但连续读空：消息列表可能被 Electron 虚拟化剪掉了，强制激活一次
            self._empty_polls += 1
            if self._empty_polls >= self.EMPTY_REACTIVATE:
                self._empty_polls = 0
                self.on_log(f"连续 {self.EMPTY_REACTIVATE} 次读不到消息（窗口看着正常），"
                            "强制激活一次防止控件树休眠")
                if self.auto_activate:
                    self._activate_candidates([qq_window])
                    self._last_probe = 0.0  # 下一轮立即重新巡检
        else:
            self._empty_polls = 0

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
