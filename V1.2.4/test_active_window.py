# -*- coding: utf-8 -*-
"""活跃窗口时长：对话 60 秒 / 拍一拍 10 秒（用户 m07728）。python test_active_window.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx_multi as M

ROOM = "test@chatroom"
fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + str(extra)))
    if not cond:
        fails.append(name)


def main():
    M.ACTIVE_FILE = os.path.join(tempfile.gettempdir(), "active_window_test.json")
    try:
        os.remove(M.ACTIVE_FILE)
    except (FileNotFoundError, OSError):
        pass

    check("常量：对话 60 秒", M.ACTIVE_SECS == 60, M.ACTIVE_SECS)
    check("常量：拍一拍 10 秒", M.PAT_ACTIVE_SECS == 10, M.PAT_ACTIVE_SECS)

    M.active_touch(ROOM)
    left = M.active_left(ROOM)
    check("对话触发 → 窗口约 60 秒", 55 <= left <= 61, left)

    M.active_touch(ROOM, M.PAT_ACTIVE_SECS)
    left = M.active_left(ROOM)
    check("拍一拍触发 → 窗口约 10 秒（覆盖成短的）", 8 <= left <= 11, left)

    M.ACTIVE_FILE = os.path.join(tempfile.gettempdir(), "active_window_test2.json")
    try:
        os.remove(M.ACTIVE_FILE)
    except (FileNotFoundError, OSError):
        pass
    check("没开过窗 → 0 秒", M.active_left(ROOM) <= 0)

    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
