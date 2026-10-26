# -*- coding: utf-8 -*-
"""测静音解除时那句「憋死我了」（m04866）。

跑法：python test_mute_end.py
"""
import datetime
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_multi as M          # noqa: E402
import wx_chat_loop as L      # noqa: E402

ME = "wxid_SELF"
ADMIN = "wxid_OWNER"
ROOM = "<群idA>@chatroom"
NOW = time.time()

TMP = os.path.join(tempfile.gettempdir(), "mute_end_test.json")
L.MUTE_FILE = TMP                      # 别碰真 mute.json
M.MUTE_PREV.clear()
M.MUTE_FIRED.clear()

CALLS = {"sent": []}
ROWS = []


def write_mute(minutes, until):
    json.dump({"scopes": {"all": {"until": until, "by": ADMIN}},
               "minutes": minutes, "by": ADMIN, "scope": "all",
               "updated": until - minutes * 60},
              open(TMP, "w", encoding="utf-8"), ensure_ascii=False)


def fake_transcript(db, room, me, n=50):
    return ROWS[-n:], []


M.L.transcript = fake_transcript
M.L.gen_mute_end = lambda rows, is_group=True: ["呜啊 憋死本鱼了"]
M.send_lines = lambda room, acct, lines, bypass_mute, tag, bypass_curfew=False: (
    CALLS["sent"].append((room, list(lines), tag)) or len(lines))


def row(txt, who, ts, wxid=ADMIN):
    return ("[x]", who, txt, wxid, datetime.datetime.fromtimestamp(ts))


def main():
    global ROWS
    # ① 静音 5 分钟刚结束，期间有人说话 → 冒一句
    write_mute(5, NOW - 5)
    ROWS = [row("闭嘴", "威", NOW - 200), row("喂 鱼鱼还在吗", "威", NOW - 100),
            row("在吗", "张德帅", NOW - 50, "wxid_xvg7m284nbfk22")]
    M.mute_end_beat(None, ROOM, ME, "群A")
    assert len(CALLS["sent"]) == 1 and CALLS["sent"][0][2] == "mute_end", CALLS["sent"]
    print("① 静音期间有人说话 → 冒一句 OK:", CALLS["sent"][0][1])

    # ② 同一段静音不重复冒
    CALLS["sent"].clear()
    M.mute_end_beat(None, ROOM, ME, "群A")
    assert not CALLS["sent"], CALLS["sent"]
    print("② 同一段只冒一次 OK")

    # ③ 静音期间没人说话 → 不吭声
    M.MUTE_FIRED.clear()
    write_mute(5, NOW - 5)
    ROWS = [row("闭嘴", "威", NOW - 400), row("闭嘴", "威", NOW - 350)]
    CALLS["sent"].clear()
    M.mute_end_beat(None, ROOM, ME, "群A")
    assert not CALLS["sent"], CALLS["sent"]
    print("③ 期间没人说话 → 不吭声 OK")

    # ④ 还在静音里 / 过期太久 → 都不冒
    M.MUTE_FIRED.clear()
    write_mute(5, NOW + 120)
    ROWS = [row("说话", "威", NOW - 10)]
    CALLS["sent"].clear()
    M.mute_end_beat(None, ROOM, ME, "群A")
    assert not CALLS["sent"], "还在静音不该冒"
    write_mute(5, NOW - 600)
    M.mute_end_beat(None, ROOM, ME, "群A")
    assert not CALLS["sent"], "过期太久不该补"
    print("④ 还在静音 / 过期太久 → 不冒 OK")

    # ⑤ 管理员 `/闭嘴 0` 手动解除 → 也要冒（窗口被清空前先记下来）
    M.MUTE_FIRED.clear()
    M.MUTE_LAST.clear()
    write_mute(5, NOW + 180)          # 静音开始于 NOW-120
    ROWS = [row("喂 鱼鱼还在吗", "威", NOW - 60), row("/ 闭嘴 0", "威", NOW - 30)]
    M.L.set_mute = L.set_mute.__wrapped__ if hasattr(L.set_mute, "__wrapped__") else L.set_mute
    M.L.load_admins = lambda path=None: {ADMIN}
    M.L.has_mute_cmd = lambda s: True
    M.L.find_mute = lambda s: 0
    M.L.scope_for = lambda room: "all"
    M.mute_heads_up(None, ROOM, ME, {})
    CALLS["sent"].clear()
    M.mute_end_beat(None, ROOM, ME, "群A")
    assert CALLS["sent"], "手动解除后也该冒一句"
    print("⑤ /闭嘴 0 手动解除 → 也冒一句 OK:", CALLS["sent"][0][1])

    # ⑥ 全体静音（私聊里下命令）→ 群里有新消息 → 私聊发 /闭嘴 0 解除后群照样要冒
    M.MUTE_FIRED.clear()
    M.MUTE_LAST.clear()
    write_mute(10, NOW + 400)                       # 全体静音，10 分钟
    M.MUTE_LAST[ROOM] = M.mute_window(ROOM)         # 主循环在静音期间记下的窗口
    L.set_mute(0)                                   # 私聊 /闭嘴 0：mute.json 清空
    ROWS = [row("略略略说不了话了吧", "威", NOW - 30)]
    CALLS["sent"].clear()
    M.mute_end_beat(None, ROOM, ME, "群A")
    assert CALLS["sent"], "全体静音被手动解除后，群里的那句也要冒"
    print("⑥ 全体静音手动解除 → 群里也冒一句 OK:", CALLS["sent"][0][1])

    os.remove(TMP)
    print("ALL PASS")


if __name__ == "__main__":
    main()
