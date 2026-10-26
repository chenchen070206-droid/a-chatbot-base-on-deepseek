# -*- coding: utf-8 -*-
"""离线测 curfew_beat：宵禁开始道晚安 / 结束问早安，每群每天一次，窗口外不补。

跑法：python test_curfew.py
"""
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_multi as M          # noqa: E402
import wx_chat_loop as L      # noqa: E402

SENT = []
M.CURFEW_STATE = os.path.join(tempfile.gettempdir(), "curfew_state_test.json")
if os.path.exists(M.CURFEW_STATE):
    os.remove(M.CURFEW_STATE)

ROOMS = [
    {"room": "<群idB>@chatroom", "account": "acct", "name": "群A", "enabled": True},
    {"room": "<群idC>@chatroom", "account": "acct", "name": "群B", "enabled": True},
    {"room": "wxid_OWNER", "account": "acct", "name": "威", "enabled": True},
]
DBS = {"acct": None}


def fake_transcript(db, room, me, n=50):
    now = datetime.datetime.now()
    rows = [(me, "我", "刚说完一句", me, now), ("u1", "张德帅", "在聊 java 老师", "u1", now)]
    return rows, []


def fake_gen(rows, night=True, is_group=True):
    return "晚安啦，明天见" if night else "早，醒了"


M.L.mute_left = lambda room=None: 0   # 测试里没有静音（m07608）


def stub_send(room, acct, lines, bypass_mute, tag, bypass_curfew=False):
    SENT.append((room, list(lines), tag, bypass_curfew))
    return len(lines)


M.L.transcript = fake_transcript
M.L.gen_curfew = fake_gen
M.send_lines = stub_send
L.mute_left = lambda room=None: 0     # 测试里没有静音（m07608 起宵禁播报要查静音）


def at(hh, mm):
    return datetime.datetime(2026, 10, 22, hh, mm, 0)


def boom(msg):
    raise AssertionError(msg)


def main():
    state = {}

    # ① 00:40 宵禁中、没记录 → 只发给两个群（私聊不发），带 bypass_curfew
    M.L.in_curfew = lambda now=None: True
    n = M.curfew_beat(ROOMS, DBS, state, 0, now=at(0, 40))
    assert n == 2, n
    assert [s[0] for s in SENT] == ["<群idB>@chatroom", "<群idC>@chatroom"], SENT
    assert all(s[3] for s in SENT), "宵禁播报必须带 bypass_curfew"
    assert state.get("night") == "2026-10-22", state
    print("① 00:40 道晚安 → 2 个群 OK")

    # ② 同一晚再来一轮 → 不发
    SENT.clear()
    n = M.curfew_beat(ROOMS, DBS, state, 1, now=at(0, 55))
    assert n == 0 and not SENT, (n, SENT)
    print("② 同晚重复 → 不发 OK")

    # ③ 07:40 宵禁结束 → 问早安
    SENT.clear()
    M.L.in_curfew = lambda now=None: False
    n = M.curfew_beat(ROOMS, DBS, state, 2, now=at(7, 40))
    assert n == 2 and state.get("morning") == "2026-10-22", (n, state)
    print("③ 07:40 问早安 → 2 个群 OK")

    # ④ 12:00 不在窗口内 → 不发（哪怕日期没记）
    SENT.clear()
    state2 = {}
    n = M.curfew_beat(ROOMS, DBS, state2, 3, now=at(12, 0))
    assert n == 0 and not SENT and state2 == {}, (n, SENT, state2)
    print("④ 12:00 窗口外 → 不发 OK")

    # ⑤ 00:40 但状态里今晚已发过 → 不发
    SENT.clear()
    n = M.curfew_beat(ROOMS, DBS, {"night": "2026-10-22"}, 4, now=at(0, 40))
    assert n == 0 and not SENT, (n, SENT)
    print("⑤ 今晚已发 → 不发 OK")

    # ⑥ 状态落盘
    assert os.path.exists(M.CURFEW_STATE), "状态没写盘"
    print("⑥ 状态已落盘 →", M.load_curfew_state())
    print("ALL PASS")


if __name__ == "__main__":
    main()
