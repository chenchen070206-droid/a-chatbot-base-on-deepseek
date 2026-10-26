# -*- coding: utf-8 -*-
"""测 mute_heads_up：/闭嘴 落在我自己回复之前也要能被扫到（m04751）。

跑法：python test_mute_scan.py
"""
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_multi as M          # noqa: E402
import wx_chat_loop as L      # noqa: E402

ME = "wxid_SELF"
ADMIN = "wxid_OWNER"
ROOM = "<群idA>@chatroom"
NOW = time.time()
ROWS = [
    ("[19:19:55]", "威", "滚蛋", ADMIN, datetime.datetime.fromtimestamp(NOW - 60)),
    ("[19:20:02]", "威", "/ 闭嘴 5", ADMIN, datetime.datetime.fromtimestamp(NOW - 50)),
    ("[19:20:02]", "我", "凶什么凶 本鱼走就是了", ME, datetime.datetime.fromtimestamp(NOW - 45)),
    ("[19:20:06]", "我", "尾巴甩给你看", ME, datetime.datetime.fromtimestamp(NOW - 40)),
]

CALLS = {"mute": [], "sent": []}


def fake_transcript(db, room, me, n=50):
    return ROWS[-n:], []


M.L.transcript = fake_transcript
M.L.load_admins = lambda path=None: {ADMIN}
M.L.scope_for = lambda room: room if room.endswith("@chatroom") else "all"
M.L.set_mute = lambda minutes, scope="all", by=None: CALLS["mute"].append((minutes, scope, by))
M.L.mute_left = lambda room=None: 0      # 别被线上 mute.json 影响（m07608：静音中不回台词）
M.send_lines = lambda room, acct, lines, bypass_mute, tag, bypass_curfew=False: (
    CALLS["sent"].append((room, list(lines), tag)) or len(lines))


def main():
    global ROWS
    # ① 管理员 /闭嘴 5 落在我两条回复之前（不是最后一条）→ 仍要扫到并执行
    done = {}
    changed = M.mute_heads_up(None, ROOM, ME, done)
    assert changed, "没扫到 /闭嘴"
    assert CALLS["mute"] == [(5, ROOM, ADMIN)], CALLS["mute"]
    assert (int(ROWS[1][4].timestamp()), ADMIN) in done[ROOM], "没记 done"
    print("① 指令在我回复之前 → 扫到并静音 5 分钟 OK")

    # ② 同一批再扫一遍 → 不重复执行
    CALLS["mute"].clear()
    changed = M.mute_heads_up(None, ROOM, ME, done)
    assert not changed and not CALLS["mute"], (changed, CALLS["mute"])
    print("② 重复扫描 → 不重复执行 OK")

    # ③ 路人提到 /闭嘴 → 回固定台词（且带 bypass，不然静音期间发不出去）
    ROWS = [("[19:30:00]", "张德帅", "你能不能 /闭嘴 3 啊", "wxid_xvg7m284nbfk22",
             datetime.datetime.fromtimestamp(NOW - 5))]
    done2 = {}
    CALLS["sent"].clear()
    M.mute_heads_up(None, ROOM, ME, done2)
    assert CALLS["sent"] and CALLS["sent"][0][2] == "taunt", CALLS["sent"]
    assert not CALLS["mute"], "路人不该改静音"
    print("③ 路人 /闭嘴 → 回固定台词 OK")

    # ④ 管理员没写分钟数 → 不执行、不记 done（留给普通消息）
    ROWS = [("[19:31:00]", "威", "/ 闭嘴", ADMIN, datetime.datetime.fromtimestamp(NOW - 3))]
    done3 = {}
    CALLS["mute"].clear()
    M.mute_heads_up(None, ROOM, ME, done3)
    assert not CALLS["mute"] and not done3.get(ROOM), (CALLS["mute"], done3)
    print("④ 管理员没写分钟数 → 不执行 OK")

    print("ALL PASS")


if __name__ == "__main__":
    main()
