# -*- coding: utf-8 -*-
"""拍一拍规则自测：python test_pat.py

规则（m05541 起 20% → m06588 改成被拍就回）：不发「（拍了拍X）」，回一句交给正常流程。
"""
import sys
import time

sys.path.insert(0, r"<程序目录>")
import wx_chat_loop as L
import wx_multi as M

ME = "wxid_SELF"
ROOM = "<群idA>@chatroom"
OTHER = "wxid_ym9t8yc4xrox19"


def pat_msg(frm, to, ts=None, chat=ROOM):
    ts = ts or time.time()
    return {"local_type": "266287972401", "create_time": str(int(ts)),
            "sender_username": "", "message_content": L.pat_xml(chat, frm, to, "某人", "鱼鱼🐳")}


class FakeDB:
    def __init__(self, msgs):
        self.msgs = msgs

    def get_messages(self, room, limit=8, offset=0):
        return self.msgs[-limit:], None


sent = []


def fake_send(room, acct, lines, bypass_mute, tag, bypass_curfew=False):
    sent.append((tag, list(lines)))
    return True


M.send_lines = fake_send
M.save_done = lambda d: None
L.mute_left = lambda room=None: 0
L.in_curfew = lambda now=None: False

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


# ① 抽中（random 0.05 < 0.2）→ 不回文字，但交回正常流程（不在 done 里）
M.random.random = lambda: 0.05
sent.clear()
done = {}
db = FakeDB([pat_msg(OTHER, ME)])
res = M.pat_beat(db, ROOM, ME, done)
check("① 抽中 → 交给正常流程", res is True and not sent and (int(time.time()), OTHER) not in done.get(ROOM, set()))

# ② 概率没抽中（把 PAT_REPLY_P 临时设 0）→ 标记 done、不回
M.random.random = lambda: 0.9
_keep_p, M.PAT_REPLY_P = M.PAT_REPLY_P, 0.0
sent.clear()
done = {}
res = M.pat_beat(FakeDB([pat_msg(OTHER, ME, ts=time.time() - 1)]), ROOM, ME, done)
check("② 没抽中 → 不回且标 done", res is False and not sent and len(done.get(ROOM, set())) == 1)
M.PAT_REPLY_P = _keep_p

# ③ 拍别人 → 不管
M.random.random = lambda: 0.05
sent.clear()
done = {}
check("③ 拍别人 → 不处理", M.pat_beat(FakeDB([pat_msg(OTHER, "wxid_xxx")]), ROOM, ME, done) is False)

# ④ 静音中 → 不处理
L.mute_left = lambda room=None: 120
done = {}
check("④ 静音中不处理", M.pat_beat(FakeDB([pat_msg(OTHER, ME)]), ROOM, ME, done) is False)
L.mute_left = lambda room=None: 0

# ⑤ 同一条处理过一次 → 不再处理
M.random.random = lambda: 0.9
done = {}
db2 = FakeDB([pat_msg(OTHER, ME)])
M.pat_beat(db2, ROOM, ME, done)
check("⑤ 同一条不重复处理", M.pat_beat(db2, ROOM, ME, done) is False)

# ⑥ 十几分钟前的拍不补
M.random.random = lambda: 0.05
done = {}
check("⑥ 太老的拍不补", M.pat_beat(FakeDB([pat_msg(OTHER, ME, ts=time.time() - 900)]), ROOM, ME, done) is False)

# ⑦ 文案分档：1 下 / 2 下 / 3 下以上
M.random.random = lambda: 0.05
done = {}
db7 = FakeDB([pat_msg(OTHER, ME)])
M.pat_beat(db7, ROOM, ME, done)
check("⑦ 第一下记为 1", M.PAT_STREAK.get(ROOM, 0) == 1)

print("\n失败 %d 项" % len(fails))
sys.exit(1 if fails else 0)
