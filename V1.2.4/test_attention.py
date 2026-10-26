"""注意力机制（用户 m06485，m07464 调参）的单元测试 + 一轮集成测试。

规则：活跃窗口里回复满 5 次后，每次想回都掷一次——
5 次 5%、6 次 10%、7 次 15%……命中就静默；
计数只被「被@」重置，一次「回复」算一次（不数气泡）。
"""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx_multi as M
import wx_chat_loop as L

tmp = tempfile.mkdtemp()
M.ACTIVE_FILE = os.path.join(tmp, "active_until.json")
M.COUNT_FILE = os.path.join(tmp, "reply_count.json")
M.DONE_FILE = os.path.join(tmp, "pending_done.json")


def clean():
    for f in (M.ACTIVE_FILE, M.COUNT_FILE, M.DONE_FILE):
        if os.path.exists(f):
            os.remove(f)


ROWS = []
ROOM = "<群idA>@chatroom"
ACCT = "wxid_SELF"


def row(ago, who, body, sid, name=None):
    t = datetime.datetime.now() - datetime.timedelta(seconds=ago)
    return ("[%s]" % t.strftime("%H:%M"), name or who,
            body, sid, t)


def test_curve():
    assert M.attention_p(0) == 0.0
    assert M.attention_p(4) == 0.0
    assert abs(M.attention_p(5) - 0.05) < 1e-9
    assert abs(M.attention_p(6) - 0.10) < 1e-9
    assert abs(M.attention_p(10) - 0.30) < 1e-9
    print("✅ 概率曲线：4→0%、5→5%、6→10%、10→30%")


def test_roll_hit_silences():
    clean()
    M.active_touch(ROOM)
    M.save_count({ROOM: 5})
    M.random.random = lambda: 0.0          # 必中
    hit = M.attention_roll(ROOM)
    assert hit is True
    assert ROOM not in M.load_active()     # 活跃窗口被清掉 → 静默
    print("✅ 5 次时掷中 → 窗口清空、进入静默")


def test_roll_miss_keeps_window():
    clean()
    M.active_touch(ROOM)
    M.save_count({ROOM: 5})
    M.random.random = lambda: 0.9          # 必不中
    hit = M.attention_roll(ROOM)
    assert hit is False
    assert M.active_left(ROOM) > 0
    print("✅ 掷不中就照常回，窗口还在")


def test_below_threshold_never_rolls():
    clean()
    M.active_touch(ROOM)
    M.save_count({ROOM: 4})
    M.random.random = lambda: 0.0
    assert M.attention_roll(ROOM) is False
    assert M.active_left(ROOM) > 0
    print("✅ 没到 5 次不掷（哪怕随机数是 0）")


def test_count_inc_reset():
    clean()
    assert M.count_get(ROOM) == 0
    assert M.count_inc(ROOM) == 1
    assert M.count_inc(ROOM) == 2
    M.count_reset(ROOM)
    assert M.count_get(ROOM) == 0
    print("✅ 计数自增 / 被 @ 重置")


def test_reset_then_reply():
    """模拟「被 @」那一支：先 reset 再回一条 → 计数回到 1（7 → 1）。"""
    clean()
    M.save_count({ROOM: 7})
    assert M.count_get(ROOM) == 7
    M.count_reset(ROOM)                     # main 里 why == "被@" 时调它
    assert M.count_inc(ROOM) == 1           # 然后发了一条
    print("✅ 被 @ 重置：7 → 0 → 回一条 → 1")


def test_silence_is_not_sticky():
    """静默只关窗口；计数器照留；重新触发（如被点名）能开窗（用户 m06588）。"""
    clean()
    M.active_touch(ROOM)
    M.save_count({ROOM: 7})
    M.random.random = lambda: 0.0
    assert M.attention_roll(ROOM) is True
    assert M.count_get(ROOM) == 7           # 计数器没被静默清掉
    assert M.active_left(ROOM) <= 0         # 窗口关了
    M.active_touch(ROOM)                    # 被点名/聊吃的重新开窗
    assert M.active_left(ROOM) > 0
    print("✅ 静默不是永久的：计数器保留、重新触发就能开窗")
    M.random.random = lambda: 0.99


def test_config_values():
    assert abs(M.REPLY_LOSS_P - 0.10) < 1e-9, M.REPLY_LOSS_P
    assert abs(M.PAT_REPLY_P - 1.0) < 1e-9, M.PAT_REPLY_P
    assert M.STICKER_CONTEXT is True, "表情包应按语境判断（m07954），不再掷概率"
    assert abs(M.SELF_STICKER_P - 0.40) < 1e-9, M.SELF_STICKER_P
    assert M.ATTENTION_FROM == 5 and abs(M.ATTENTION_STEP - 0.05) < 1e-9
    assert M.SILENT_ROLL_GAP == 1800 and M.SILENT_ROLL_COOL == 600
    assert M.IDLE_TOPIC_EVERY == 1800 and M.MEAL_RECENT == 1200
    print("✅ 常量：丢失 10%、拍一拍 100%、表情包按语境、图里是我 40%、注意力 5 次起 +5%、"
          "久静 30 分钟/冷却 10 分钟、抛话题每 30 分钟、饭点 20 分钟")


if __name__ == "__main__":
    test_curve()
    test_roll_hit_silences()
    test_roll_miss_keeps_window()
    test_below_threshold_never_rolls()
    test_count_inc_reset()
    test_reset_then_reply()
    test_silence_is_not_sticky()
    test_config_values()
    print("\n全部通过")
