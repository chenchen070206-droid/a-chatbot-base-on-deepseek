# -*- coding: utf-8 -*-
"""久静开窗骰自测：python test_silent_roll.py

规则（m06873，m07464 改口径）：我自己 30 分钟没说话、新消息又没触发活跃 → 5% 概率直接开窗；
同一群 10 分钟内只掷一次。
"""
import datetime
import os
import sys
import tempfile
import time

sys.path.insert(0, r"<程序目录>")
import wx_multi as M

ROOM = "<群idA>@chatroom"
ME = "wxid_SELF"
OTHER = "wxid_OWNER"
fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def row(minutes_ago, now, sender=OTHER):
    t = now - datetime.timedelta(minutes=minutes_ago)
    return ("[%s]" % t.strftime("%H:%M"), "威", "在吗", sender, t)


def main():
    tmp = tempfile.mkdtemp(prefix="silent_")
    M.SILENT_FILE = os.path.join(tmp, "silent_roll.json")

    now = datetime.datetime(2026, 10, 23, 21, 0, 0)
    # 我 40 分钟前说过话，然后群里我刚又有人说话 → 允许掷
    rows = [row(45, now, ME), row(40, now, OTHER), row(0, now, OTHER)]
    check("① 我自己 40 分钟没说话 → 允许掷", M.silent_gap_ok(rows, 2, ME) is True)

    # 我 5 分钟前刚说过 → 不掷
    rows2 = [row(30, now, OTHER), row(5, now, ME), row(0, now, OTHER)]
    check("② 我 5 分钟前刚说过 → 不掷", M.silent_gap_ok(rows2, 2, ME) is False)

    # 我 25 分钟前说过（不到 30）→ 不掷
    rows3 = [row(35, now, OTHER), row(25, now, ME), row(0, now, OTHER)]
    check("③ 我 25 分钟前说过（不到 30）→ 不掷", M.silent_gap_ok(rows3, 2, ME) is False)

    # 窗口里没我说过话 → 算很久没说了
    rows4 = [row(40, now, OTHER), row(0, now, OTHER)]
    check("④ 窗口里没我说过话 → 允许掷", M.silent_gap_ok(rows4, 1, ME) is True)

    check("⑤ 单条消息（没有上一条）→ 不掷", M.silent_gap_ok([row(0, now)], 0, ME) is False)

    state = {}
    t0 = time.time()
    M.random.random = lambda: 0.01
    check("⑥ 掷中 5% → True", M.silent_roll(ROOM, state, now=t0) is True)
    check("⑦ 掷中后记时间", abs(state[ROOM] - t0) < 1)

    M.random.random = lambda: 0.0
    check("⑧ 10 分钟冷却内不再掷", M.silent_roll(ROOM, state, now=t0 + 300) is False)

    M.random.random = lambda: 0.9
    check("⑨ 冷却过后再掷，没中也记冷却", M.silent_roll(ROOM, state, now=t0 + 601) is False)
    check("⑩ 没中也算掷过（冷却生效）",
          M.silent_roll(ROOM, state, now=t0 + 900) is False)

    check("⑪ 别的群互不影响", M.silent_roll("<群idB>@chatroom", state, now=t0 + 602) is False)

    M.random.random = lambda: 0.04
    check("⑫ 冷却过后掷中 → True", M.silent_roll(ROOM, state, now=t0 + 1300) is True)

    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
