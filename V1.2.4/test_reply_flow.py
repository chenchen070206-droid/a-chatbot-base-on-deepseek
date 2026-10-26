# -*- coding: utf-8 -*-
"""离线验证「按时间先后回完欠账」的主循环逻辑（不打微信、不调 DeepSeek）。

跑法: python test_reply_flow.py
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
T0 = datetime.datetime.now() - datetime.timedelta(minutes=10)   # 全部落在 PENDING_AGE(3 分钟) 之内
_sent = []


def row(minute, who, text):
    sender = ME if who == "我" else who
    when = datetime.datetime.now() - datetime.timedelta(seconds=(4 - minute) * 40)  # 0~160 秒前
    return ("[18:%02d]" % minute, who, text, sender, when)


def scenario(name, rows, loss=0.0):
    _sent.clear()
    M.DONE_FILE = os.path.join(tempfile.gettempdir(), "pending_done_test_%s.json" % name)
    try:
        os.remove(M.DONE_FILE)
    except FileNotFoundError:
        pass
    # 活跃期状态清零，否则上一个用例的开窗会漏到下一个（m06274 新机制）
    try:
        os.remove(M.ACTIVE_FILE)
    except FileNotFoundError:
        pass

    def fake_send(room, acct, lines, bypass, tag, bypass_curfew=False, **kw):
        _sent.extend(lines)
        return len(lines)

    def fake_gen(r, is_group=True, todo=None, sticker_p=None, sticker_hot=False):
        if todo:
            # 每条欠账一次调用：欠账原文进回稿，方便断言「各回各的、不混」
            out = []
            for i, (_w, t, _k) in enumerate(todo):
                if "长回答" in t:          # 单条欠账拆两个气泡
                    out.append("回%d-1:%s" % (i + 1, t))
                    out.append("回%d-2:%s（接着说）" % (i + 1, t))
                else:
                    out.append("回%d:%s" % (i + 1, t))
            return "\n".join(out)
        return "回最后一条"

    M.TUNE.apply = lambda *a, **k: None   # 别让 tuning.json 覆盖测试里设的常量
    M.send_lines = fake_send
    M.REPLY_LOSS_P = loss            # 默认关掉 10% 丢失，单独用第 4 例测
    L.gen = fake_gen
    L.transcript = lambda db, room, me, n=50: (rows, [])
    L.relates_to_recent = lambda mine, body, max_n=5: (False, "stub-无关")
    L.in_curfew = lambda now=None: False
    M.load_rooms = lambda p: [{"name": "测试群", "room": ROOM, "account": ME, "enabled": True}]
    L.open_db = lambda a: None
    L.my_name = lambda db, a: "鱼鱼"
    L.mute_left = lambda room=None: 0
    sys.argv = ["wx_multi.py", "--rounds", "1", "--gap", "0"]
    print("\n=== %s ===" % name)
    M.main()
    print("发出 %d 条:" % len(_sent))
    for s in _sent:
        print("   ", s)
    return list(_sent)


if __name__ == "__main__":
    os.chdir(HERE)
    # 1. 我最后发言 → 没欠账
    s1 = scenario("我最后一条", [row(0, "张三", "在吗"), row(1, "我", "在的")])
    assert s1 == [], s1

    # 2. 两条 @我 的欠账 → 各回各的（每条欠账独立出稿，允许 1~3 个气泡，但不许混在一起）
    s2 = scenario("两条欠账", [row(0, "张三", "在吗"),
                               row(1, "我", "在的"),
                               row(2, "张三", "@鱼鱼🐳 帮我看看这个"),
                               row(3, "李四", "今天好热"),
                               row(4, "李四", "@鱼鱼🐳 你也觉得热吧")])
    assert len(s2) == 2, s2
    assert "帮我看看这个" in s2[0] and "你也觉得热吧" not in s2[0], s2
    assert "你也觉得热吧" in s2[1] and "帮我看看这个" not in s2[1], s2

    # 2b. 单条欠账拆成两个气泡 → 允许，且两个气泡都属于同一件事
    s2b = scenario("一条欠账拆两句", [row(0, "我", "在的"),
                                     row(1, "张三", "@鱼鱼🐳 给我长回答这个问题")])
    assert len(s2b) == 2, s2b
    assert all("长回答" in x for x in s2b), s2b

    # 3. 只有无关消息（不在活跃期、也没触发词）→ 不回
    s3 = scenario("无关消息", [row(0, "我", "我先说一句"),
                               row(1, "张三", "今天好热啊")])
    assert s3 == [], s3

    # 4. 活跃期里抽中 10% 丢失 → 该条不回，且不影响后面（loss=1.0 必丢）
    s4 = scenario("10% 丢失", [row(0, "我", "在的"),
                               row(1, "张三", "@鱼鱼🐳 在吗")], loss=1.0)
    assert s4 == [], s4

    print("\n全部通过")
