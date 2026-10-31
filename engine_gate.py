# -*- coding: utf-8 -*-
"""每个模拟器实例一把互斥锁。

主号队列、小号通道、剪贴板进家园都会操作同一批 VisitEngine（同一个模拟器），
visit_one 这类流程不是线程安全的（截图→点击→等状态，中途插入别人的操作会互相干扰）。
所有要跑 engine.visit_one / 大段引擎操作的地方，先 lock_for(name) 拿锁。
"""
import threading

_locks = {}
_guard = threading.Lock()


def lock_for(name):
    with _guard:
        lk = _locks.get(name)
        if lk is None:
            lk = _locks[name] = threading.Lock()
        return lk
