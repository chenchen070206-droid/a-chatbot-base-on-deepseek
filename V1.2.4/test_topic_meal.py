# -*- coding: utf-8 -*-
"""主动起话题 / 饭点报菜单 的测试（用户 m06545）。"""
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx_multi as M
import wx_chat_loop as L

tmp = tempfile.mkdtemp()
M.IDLE_FILE = os.path.join(tmp, "idle_topic.json")
M.MEAL_FILE = os.path.join(tmp, "meal_beat.json")

ROOM = "<群idA>@chatroom"
PRIVATE = "wxid_OWNER"
ACCT = "wxid_SELF"
ROOMS = [{"room": ROOM, "account": ACCT, "name": "群A", "enabled": True},
         {"room": PRIVATE, "account": ACCT, "name": "威", "enabled": True}]
DBS = {ACCT: None}

SENT = []


def clean():
    SENT.clear()
    for f in (M.IDLE_FILE, M.MEAL_FILE):
        if os.path.exists(f):
            os.remove(f)


class FakeRandom:
    def __init__(self, r=0.0, pick=0):
        self._r = r
        self._pick = pick

    def random(self):
        return self._r

    def randint(self, a, b):
        return max(a, min(b, self._pick))

    def uniform(self, a, b):
        return a


def fake_send(room, acct, lines, bypass_mute, tag, bypass_curfew=False):
    SENT.append((room, list(lines), tag))
    return len(lines)


def setup(rows, r=0.0, pick=0):
    L.mute_left = lambda room=None: 0      # 默认没静音（线上 mute.json 不该影响测试）
    L.transcript = lambda db, room, me, n=50: (rows, [])
    L.gen_topic = lambda rows=None, is_group=True: ["今天谁还没吃饭"]
    L.gen_meal = lambda rows=None, is_group=True: ["本鱼中午吃了两碗白饭"]
    M.send_lines = fake_send
    M.random = FakeRandom(r, pick)


def mkrow(minutes_ago, now):
    t = now - datetime.timedelta(minutes=minutes_ago)
    return ("[%s]" % t.strftime("%H:%M"), "威", "在吗", "wxid_OWNER", t)


def test_idle_sends():
    clean()
    now = datetime.datetime(2026, 10, 23, 15, 0, 0)
    setup([mkrow(70, now)], r=0.05)
    n = M.idle_topic_beat(ROOMS, DBS, {}, 0, now=now)
    assert n == 1 and SENT and SENT[0][0] == ROOM and SENT[0][2] == "idle", (n, SENT)
    print("✅ 静 70 分钟（>1 小时）+ 抽中 10%% → 抛话题")


def test_idle_interval():
    clean()
    now = datetime.datetime(2026, 10, 23, 15, 0, 0)
    setup([mkrow(70, now)], r=0.0)
    state = {}
    assert M.idle_topic_beat(ROOMS, DBS, state, 0, now=now) == 1
    SENT.clear()
    later = now + datetime.timedelta(minutes=3)      # 不到 10 分钟，不掷
    assert M.idle_topic_beat(ROOMS, DBS, state, 1, now=later) == 0 and not SENT
    print("✅ 掷过一次后 10 分钟内不再掷")


def test_idle_miss():
    clean()
    now = datetime.datetime(2026, 10, 23, 15, 0, 0)
    setup([mkrow(70, now)], r=0.5)                   # 没抽中
    state = {}
    assert M.idle_topic_beat(ROOMS, DBS, state, 0, now=now) == 0 and not SENT
    assert ROOM in state                              # 时间记下了，10 分钟后再掷
    print("✅ 没抽中不发文，时间点记下")


def test_idle_quiet_and_private():
    clean()
    now = datetime.datetime(2026, 10, 23, 15, 0, 0)
    setup([mkrow(5, now)], r=0.0)                     # 刚聊过
    assert M.idle_topic_beat(ROOMS, DBS, {}, 0, now=now) == 0 and not SENT
    setup([mkrow(60, now)], r=0.0)
    L.transcript = lambda db, room, me, n=50: ([mkrow(60, now)], [])
    # 私聊也会拿到这行；确认私聊不触发
    state = {}
    n = M.idle_topic_beat([ROOMS[1]], DBS, state, 0, now=now)
    assert n == 0 and not SENT
    print("✅ 刚聊过不触发；私聊不触发")


def test_idle_and_meal_skip_muted():
    """群被 /闭嘴 之后，不主动抛话题、也不报菜单（用户 m07583）。"""
    clean()
    now = datetime.datetime(2026, 10, 23, 12, 0, 0)
    setup([mkrow(70, now)], r=0.0)
    L.mute_left = lambda room=None: 600        # 所有会话都在静音
    try:
        assert M.idle_topic_beat(ROOMS, DBS, {}, 0, now=now) == 0 and not SENT
        setup([mkrow(5, now)], r=0.0, pick=0)
        L.mute_left = lambda room=None: 600    # setup() 会把它清掉，这里再设一次
        M.meal_beat(ROOMS, DBS, {}, 0, now=now)
        assert not SENT, SENT
    finally:
        L.mute_left = lambda room=None: 0
    clean()
    print("✅ 静音的群：不抛话题、不报菜单")


def test_idle_curfew():
    clean()
    now = datetime.datetime(2026, 10, 23, 3, 0, 0)
    setup([mkrow(60, now)], r=0.0)
    assert M.idle_topic_beat(ROOMS, DBS, {}, 0, now=now) == 0 and not SENT
    print("✅ 宵禁里不抛话题")


def test_meal_sends():
    clean()
    now = datetime.datetime(2026, 10, 23, 11, 45, 0)
    setup([mkrow(5, now)], r=0.0)
    state = {"%s|0|2026-10-23" % ROOM: {"at": "2026-10-23", "pick": now.timestamp() - 60, "sent": False}}
    n = M.meal_beat(ROOMS, DBS, state, 0, now=now)
    assert n == 1 and SENT and SENT[0][2] == "meal", (n, SENT)
    SENT.clear()
    assert M.meal_beat(ROOMS, DBS, state, 1, now=now + datetime.timedelta(minutes=1)) == 0 and not SENT
    print("✅ 饭点挑中的时间点到了 + 半小时内有消息 → 报菜单；同一天不重复")


def test_meal_no_activity():
    clean()
    now = datetime.datetime(2026, 10, 23, 12, 10, 0)
    setup([mkrow(180, now)], r=0.0)
    state = {"%s|0|2026-10-23" % ROOM: {"at": "2026-10-23", "pick": now.timestamp() - 60, "sent": False}}
    assert M.meal_beat(ROOMS, DBS, state, 0, now=now) == 0 and not SENT
    assert state["%s|0|2026-10-23" % ROOM]["sent"] is True
    print("✅ 最近半小时没消息 → 不发（标 sent）")


def test_meal_pick_range():
    clean()
    now = datetime.datetime(2026, 10, 23, 17, 5, 0)   # 晚饭窗口 17:00-19:00
    setup([mkrow(2, now)], r=0.0, pick=0)
    state = {}
    M.meal_beat(ROOMS, DBS, state, 0, now=now)
    rec = state.get("%s|1|2026-10-23" % ROOM)
    assert rec is not None
    t = datetime.datetime.fromtimestamp(rec["pick"])
    assert t.hour == 17 and t.minute == 0, t         # pick=0 → 窗口起点
    print("✅ 晚饭窗口也会挑时间点（17:00 起算）")


def test_meal_outside_window():
    clean()
    now = datetime.datetime(2026, 10, 23, 14, 0, 0)
    setup([mkrow(2, now)], r=0.0)
    state = {}
    assert M.meal_beat(ROOMS, DBS, state, 0, now=now) == 0 and not SENT and not state
    print("✅ 非饭点什么都不做")


if __name__ == "__main__":
    test_idle_sends()
    test_idle_interval()
    test_idle_miss()
    test_idle_quiet_and_private()
    test_idle_curfew()
    test_idle_and_meal_skip_muted()
    test_meal_sends()
    test_meal_no_activity()
    test_meal_pick_range()
    test_meal_outside_window()
    print("\n全部通过")
