# -*- coding: utf-8 -*-
"""触发开窗概率：被@/私聊必开，昵称开头/被点名/聊吃的 10%（用户 m07785）。

跑法：python test_trigger_window.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx_multi as M

ROOM = "test@chatroom"
fails = []
_orig_random = M.random.random


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + str(extra)))
    if not cond:
        fails.append(name)


def reset(room=ROOM):
    M.ACTIVE_FILE = os.path.join(tempfile.gettempdir(), "trigger_window_test.json")
    try:
        os.remove(M.ACTIVE_FILE)
    except (FileNotFoundError, OSError):
        pass


def main():
    check("常量 TRIGGER_OPEN_P = 0.10", abs(M.TRIGGER_OPEN_P - 0.10) < 1e-9, M.TRIGGER_OPEN_P)

    for why in ("被@", "私聊"):
        reset()
        M.random.random = lambda: 0.99          # 骰子最大也照样开
        check("%s 一定开窗" % why, M.trigger_opens(ROOM, why))

    for why in ("点名开头", "被点名", "聊吃的"):
        reset()
        M.random.random = lambda: 0.05          # < 10% → 开
        check("%s 抽中 10%% → 开窗" % why, M.trigger_opens(ROOM, why))
        reset()
        M.random.random = lambda: 0.50          # > 10% → 不开
        check("%s 没抽中 → 不开窗" % why, not M.trigger_opens(ROOM, why))

    reset()
    M.active_touch(ROOM)                        # 窗口已经开着
    M.random.random = lambda: 0.99
    check("窗口已开 → 昵称开头直接续窗（不掷骰）", M.trigger_opens(ROOM, "点名开头"))

    reset()
    M.random.random = lambda: 0.5
    for _ in range(200):
        M.trigger_opens(ROOM, "聊吃的")
    # 200 次都掷 0.5 恒不开，这里只验证概率分支没抛错
    check("反复调用不抛异常", True)

    M.random.random = _orig_random
    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
