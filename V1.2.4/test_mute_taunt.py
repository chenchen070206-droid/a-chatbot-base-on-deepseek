# -*- coding: utf-8 -*-
"""静音期间的 /闭嘴 处理（用户 m04067 + m07608）。

规则：
  - 管理员写分钟数 → 立刻 set_mute / 解除，不发台词；
  - 路人提到 /闭嘴 → 回固定台词，**但群在静音中就不回**（m07608）；
  - 只发「闭嘴」不算命令。

不碰微信：假 transcript + 打桩 send_lines / mute_left。
"""
import datetime
import sys

HERE = r"<程序目录>"
sys.path.insert(0, HERE)
sys.path.insert(0, HERE + r"\wechat-mcp\scripts")

import wx_chat_loop as L  # noqa: E402
import wx_multi as M      # noqa: E402

ME = "wxid_SELF"
GROUP = "<群idC>@chatroom"
ADMIN = "wxid_OWNER"
OTHER = "wxid_stf06kdd2fih22"

sent = []
M.send_lines = lambda room, acct, lines, bypass, tag: (sent.append((lines, bypass, tag)) or len(lines))
M.save_done = lambda done: None
M.L.load_admins = lambda: {ADMIN}
M.L.set_mute = lambda *a, **k: None          # 别把测试写进真的 mute.json


def fake_rows(who, body):
    now = datetime.datetime.now()
    return [("[%s]" % now.strftime("%H:%M"), "谁", body, who, now)]


def case(name, who, body, expect_send, expect_ret, muted=0):
    sent.clear()
    M.L.transcript = lambda db, room, acct, n=50: (fake_rows(who, body), [])
    M.L.mute_left = lambda room=None: muted
    done = {}
    ret = M.mute_heads_up(None, GROUP, ME, done)
    ok = (ret == expect_ret) and (bool(sent) == expect_send)
    if sent:
        ok = ok and sent[0][0] == [M.MUTE_TAUNT] and sent[0][1] is True
    print("%s %s  ret=%s sent=%s" % ("[OK]" if ok else "[FAIL]", name, ret, sent))
    return ok


allok = True
# 静音中：一律不回台词（m07608）
allok &= case("路人 /闭嘴 5（静音中）→ 不回", OTHER, "/ 闭嘴 5", False, False, muted=600)
allok &= case("路人 ／闭嘴 3（全角，静音中）→ 不回", OTHER, "／闭嘴 3", False, False, muted=600)
# 没静音：回台词（m04067）
allok &= case("路人 /闭嘴 5（没静音）→ 回台词", OTHER, "/ 闭嘴 5", True, False)
allok &= case("路人 ／闭嘴 3（全角）→ 回台词", OTHER, "／闭嘴 3", True, False)
allok &= case("路人 聊天里夹 /闭嘴 → 回台词", OTHER, "你能不能 /闭嘴 10 啊", True, False)
allok &= case("路人只发「闭嘴」→ 不当命令", OTHER, "闭嘴", False, False)
allok &= case("管理员 /闭嘴 0 → 解除，不发台词", ADMIN, "/闭嘴 0", False, True)
allok &= case("管理员 /闭嘴 5 → 刷新静音，不发台词", ADMIN, "/ 闭嘴 5", False, True)
allok &= case("管理员提 /闭嘴 没写分钟数 → 留给普通消息", ADMIN, "/闭嘴", False, False)
print("\n全部通过" if allok else "\n有失败")
sys.exit(0 if allok else 1)
