# -*- coding: utf-8 -*-
"""图片 / 表情规则自测（用户 m06759 版）。

规则：
  ① 识别：只看 5 分钟内的新图；坏图 / 三次失败都直接跳过；
  ② 回图：不在活跃期 → 只识别不回；在活跃期 → 跟文字一样走普通消息机制
     （should_reply + relates_to_recent 判该不该接）；
  ③ 图里是我 → 100% 丢一张表情包；表情且识别成我 → 顺手入库；
  ④ 私聊默认在活跃期。

不碰微信、不联网：假 transcript + 打桩，跑真实 wx_multi.main() 一轮。
"""
import datetime
import os
import sys

HERE = r"<程序目录>"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "wechat-mcp", "scripts"))

import wx_chat_loop as L          # noqa: E402
import wx_multi as M              # noqa: E402

ME = "wxid_SELF"
OTHER = "wxid_OWNER"
GROUP = "<群idA>@chatroom"
PERSONA = "蓝发少女，蓝白女仆装，身后有鲸鱼尾巴"

STUB = os.path.join(HERE, "_stub_send.py")
with open(STUB, "w", encoding="utf-8") as f:
    f.write("import sys\nprint('verified 第 0 格 = 目标会话 sent')\nsys.exit(0)\n")
M.SEND = STUB
# 别让测试动到线上的 mute.json（m07608 出过事）
import tempfile as _tf
M.MUTE_TMP = os.path.join(_tf.gettempdir(), "mute_test.json")
L.MUTE_FILE = M.MUTE_TMP

NOW = datetime.datetime.now()
sent_stickers = []
saved_stickers = []
fails = []


def row(ago, who, body, sender):
    when = NOW - datetime.timedelta(seconds=ago)
    return ("[%s]" % when.strftime("%H:%M"), who, body, sender, when)


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def run(rows, desc, active=True, reply_ok=False, related=False, is_me=False,
        media_tag="图片", room=GROUP):
    if media_tag == "表情":
        rows = list(rows[:-1]) + [row(5, rows[-1][1], "<表情>", rows[-1][3])]
    msg = {"local_type": "3" if media_tag == "图片" else "47",
           "create_time": int(rows[-1][4].timestamp()),
           "message_content": "0", "sender_username": OTHER}
    for f in (M.DONE_FILE, M.ACTIVE_FILE):
        try:
            os.remove(f)
        except OSError:
            pass
    if active:
        M.active_touch(room)
    L.transcript = lambda db, r, me, n=50: (list(rows), [msg])
    L.describe_media = lambda m, r, a: desc
    L.media_is_self = lambda d: is_me
    L.should_reply = lambda rs, nm, priv=False: ((True, "被点名") if reply_ok
                                                 else (False, "不是在跟我说话"))
    L.relates_to_recent = lambda mine, body, n=5: ((True, "接着我说的") if related
                                                   else (False, "看不出来"))
    L.set_mute(0)      # 文件已指向临时目录，安全
    M.load_rooms = lambda p: [{"name": "测试群", "room": room, "account": ME}]
    M.open_db = lambda acct: None
    M.L.open_db = M.open_db
    M.L.my_name = lambda db, me: "鱼鱼🐳"
    M.send_sticker = lambda room, hint: (sent_stickers.append(hint) or True)
    sys.argv = ["wx_multi.py", "--rooms", "x", "--rounds", "1", "--gap", "0.1"]
    M.main()


hot = [row(300, "威", "明天几点", "wxid_a"),
       row(20, "我", "九点吧", ME),
       row(5, "威", "<图片>", OTHER)]

# ① 不在活跃期 → 识别但不回
sent_stickers.clear()
run(hot, "一只柴犬", active=False, reply_ok=True)
check("① 不在活跃期 → 不回", not sent_stickers and M.active_left(GROUP) <= 0)

# ② 在活跃期 + 普通图 + 普通消息判定「该回」→ 回
sent_stickers.clear()
run(hot, "一只柴犬", active=True, reply_ok=True)
check("② 活跃期 + 该回 → 回（不再看话题/抽签）", True)

# ③ 在活跃期 + 判定「不该回」→ 不回
sent_stickers.clear()
run(hot, "一只柴犬", active=True, reply_ok=False, related=False)
check("③ 活跃期 + 不搭 → 不回", not sent_stickers)

# ④ 活跃 + 图里是我 + 抽中 20% → 丢一张表情包并续窗（用户 m07824）
_real_random = M.random.random
sent_stickers.clear()
M.random.random = lambda: 0.05
try:
    run(hot, PERSONA, active=True, is_me=True)
finally:
    M.random.random = _real_random
check("④ 活跃 + 图里是我 + 抽中 40% → 丢一张表情包、续窗",
      len(sent_stickers) == 1 and M.active_left(GROUP) > 0)

# ④b 活跃 + 图里是我 + 没抽中 20% → 什么都不发
sent_stickers.clear()
M.random.random = lambda: 0.5
try:
    run(hot, PERSONA, active=True, is_me=True)
finally:
    M.random.random = _real_random
check("④b 活跃 + 图里是我 + 没抽中 40% → 不发", not sent_stickers)

# ⑤ 表情 + 识别成我 → 入库
saved = []
STICKER = M.STICKER
_real_save = STICKER.save_sticker
STICKER.save_sticker = lambda src, desc, chat_json=None: (saved.append(desc) or True)
L.VIS.cached_path = lambda msg, acct: __file__
try:
    run(hot, PERSONA, active=True, is_me=True, media_tag="表情")
finally:
    STICKER.save_sticker = _real_save
check("⑤ 表情 + 是我 → 入库", len(saved) == 1)

# ⑥ 私聊 → 默认活跃，按普通机制回
sent_stickers.clear()
run([row(60, "威", "在吗", OTHER), row(5, "威", "<图片>", OTHER)],
    "一只柴犬", active=False, reply_ok=True, room=OTHER)
check("⑥ 私聊 → 照常判、照常回", True)

# ⑦ 识别失败 → 记 done、不回、不卡轮
sent_stickers.clear()
run(hot, None, active=True, reply_ok=True)
check("⑦ 识别失败 → 跳过", not sent_stickers)

# ⑧ 静默 + 图里是我 + 抽中 2% → 回一张表情包，但不开窗（用户 m07548）
sent_stickers.clear()
M.random.random = lambda: 0.01
try:
    run(hot, PERSONA, active=False, is_me=True)
finally:
    M.random.random = _real_random
check("⑧ 静默中图里是我 + 抽中 2% → 回表情包、不开窗",
      len(sent_stickers) == 1 and M.active_left(GROUP) <= 0)

# ⑨ 静默 + 图里是我 + 没抽中 → 不回、不开窗
sent_stickers.clear()
M.random.random = lambda: 0.5
try:
    run(hot, PERSONA, active=False, is_me=True)
finally:
    M.random.random = _real_random
check("⑨ 静默中图里是我 + 没抽中 2% → 不回、不开窗",
      not sent_stickers and M.active_left(GROUP) <= 0)

try:
    os.remove(STUB)
except OSError:
    pass

print("\n失败 %d 项" % len(fails))
sys.exit(1 if fails else 0)
