# -*- coding: utf-8 -*-
"""久静开窗的整轮验证：python test_silent_integration.py（不打微信、不调 DeepSeek）

口径（m07464）：我自己 30 分钟没说话 + 新消息没触发活跃 → 5% 直接开窗。
"""
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_chat_loop as L
import wx_multi as M

ME = "wxid_SELF"
ROOM = "<群idB>@chatroom"
OTHER = "wxid_OWNER"
_sent = []
fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def run(rand, my_msg_ago=45):
    _sent.clear()
    for f in (M.DONE_FILE, M.ACTIVE_FILE, M.SILENT_FILE, M.COUNT_FILE):
        try:
            os.remove(f)
        except (FileNotFoundError, OSError):
            pass
    M.DONE_FILE = os.path.join(tempfile.gettempdir(), "done_silent_it.json")
    M.COUNT_FILE = os.path.join(tempfile.gettempdir(), "count_silent_it.json")
    M.SILENT_FILE = os.path.join(tempfile.gettempdir(), "silent_it.json")

    now = datetime.datetime.now()
    rows = [
        ("[20:10]", "我", "在呢", ME, now - datetime.timedelta(minutes=my_msg_ago)),
        ("[20:40]", "威", "有人吗", OTHER, now - datetime.timedelta(minutes=4)),
        ("[20:44]", "威", "今天好热啊", OTHER, now - datetime.timedelta(seconds=5)),
    ]

    M.send_lines = lambda room, acct, lines, bypass, tag: (_sent.extend(lines), len(lines))[1]
    M.REPLY_LOSS_P = 0.0
    M.random.random = lambda: rand
    L.gen = lambda r, is_group=True, todo=None, sticker_p=None, sticker_hot=False: "回一句试试"
    L.transcript = lambda db, room, me, n=50: (rows, [])
    L.relates_to_recent = lambda mine, body, max_n=5: (True, "stub-搭")
    L.should_reply = lambda r, name, is_private=False: (False, "不是在跟我说话")
    L.in_curfew = lambda now=None: False
    M.load_rooms = lambda p: [{"name": "测试群", "room": ROOM, "account": ME, "enabled": True}]
    L.open_db = lambda a: None
    L.my_name = lambda db, a: "鱼鱼"
    L.mute_left = lambda room=None: 0
    sys.argv = ["wx_multi.py", "--rounds", "1", "--gap", "0"]
    M.main()
    return list(_sent), M.active_left(ROOM)


def main():
    sent, left = run(0.01, my_msg_ago=45)
    check("① 我 45 分钟没说话 + 掷中 5% → 开窗并回话", sent and left > 0)

    sent, left = run(0.9, my_msg_ago=45)
    check("② 我 45 分钟没说话但没掷中 → 不回、不开窗", not sent and left <= 0)

    sent, left = run(0.01, my_msg_ago=2)
    check("③ 我 2 分钟前刚说话 → 连骰子都不掷", not sent and left <= 0)

    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
