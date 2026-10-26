# -*- coding: utf-8 -*-
"""多会话自动回复：同时盯若干个私聊/群，各自判断要不要接、各自出稿发出。

回复语义（用户 m03164 / m03260 / m03284 定）：
  我最后一条消息之后进来的、且判定为「该回」的消息，按时间先后回完；
  这一次最多发 3 条，每条都要回完整，不为一条问题拆两条占额度，也不补更早的旧账。

用法:
  python wx_multi.py --rooms rooms.json --rounds 999999 --gap 2
"""
import argparse
import datetime
import json
import os
import random
import subprocess
import sys
import time

# 控制台可能是 GBK（Start-Process 拉起来时尤其容易），打不出 🐳 会直接崩，先统一成 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_chat_loop as L  # noqa: E402
import hook_send as HS  # noqa: E402   hook 直发通道（含发图）
import sticker_lib as STICKER  # noqa: E402  表情包库
import tuning as TUNE  # noqa: E402  面板可调参数（tuning.json）

SEND = os.path.join(HERE, "send_guard.py")

# 非管理员想让我闭嘴时的固定回击（不含换行，一条消息发完）
MUTE_TAUNT = "诶诶杂鱼，就你还想让我闭嘴(｀∀´)Ψ ざぁこ♡"

MAX_LINES = 4          # 一次回答最多发几条（用户 m04375：一次回应最多 4 句）
LINE_GAP_MIN, LINE_GAP_MAX = 2.0, 4.0   # 同一轮里两条消息之间的间隔（用户 m04417：2~4 秒随机）
PENDING_AGE = 180       # 只追最近 3 分钟还没回的，更早的算翻篇（用户 m03356）
DONE_FILE = os.path.join(HERE, "pending_done.json")

# 同一个群刚回过图，20 秒内再来图：只能在话题里 + 抽中 5% 才回，否则只识别（用户 m04012）

# 活跃期：对话类触发 → 1 分钟内才可能回；拍一拍触发 → 只有 10 秒（用户 m07728）。
# 活跃期外一律静默（图片也一样）；活跃期内的正常回复有 20% 概率被丢掉，丢了不影响后面。
ACTIVE_SECS = 60               # 对话触发的活跃窗口：1 分钟（用户 m07728）
PAT_ACTIVE_SECS = 10           # 拍一拍触发的活跃窗口：10 秒（用户 m07728）
ACTIVE_FILE = os.path.join(HERE, "active_until.json")
# ---- 长时间静默后的随机开窗（用户 m06873）----
# 群静了 20 分钟以上、新消息又没触发活跃 → 5% 概率直接开窗参与话题；
# 同一个群 5 分钟内最多掷一次（掷不中也要等冷却）。
SILENT_ROLL_GAP = 1800       # 我自己 30 分钟没说话（用户 m07464）
SILENT_ROLL_P = 0.05         # 5%
SILENT_ROLL_COOL = 600       # 10 分钟冷却（用户 m07464）
SILENT_FILE = os.path.join(HERE, "silent_roll.json")
COUNT_FILE = os.path.join(HERE, "reply_count.json")
# ---- 主动起话题 / 饭点报菜单（用户 m06545）----
IDLE_TOPIC_AFTER = 3600      # 群里静 1 小时后才开始考虑（用户 m06842）
IDLE_TOPIC_EVERY = 1800      # 之后每 30 分钟掷一次（用户 m07464）
IDLE_TOPIC_P = 0.10          # 掷中的概率 10%
IDLE_FILE = os.path.join(HERE, "idle_topic.json")
MEAL_WINDOWS = (("11:30", "12:30"), ("17:00", "19:00"))   # 两个饭点
MEAL_RECENT = 1200           # 饭点前 20 分钟内有消息才发（用户 m07464）
MEAL_FILE = os.path.join(HERE, "meal_beat.json")
REPLY_LOSS_P = 0.10   # 活跃期内正常回复的丢失概率（用户 m06588：20% → 10%；**只对群聊生效**，私聊不丢 m09368）
TRIGGER_OPEN_P = 0.10          # 昵称开头/被点名/聊吃的 开窗概率（用户 m07785；被@和私聊不受限）
TRIGGER_WHY = ("被@", "点名开头", "被点名", "聊吃的", "私聊")

# 表情包：不再按概率掷骰，改为活跃窗口里由模型按语境判断要不要配一张（用户 m07954）
STICKER_CONTEXT = True

# 图/表情里画的是我自己：不再回那句疑问，改成 10% 概率丢一张表情包（用户 m05991）
SELF_STICKER_P = 0.40    # 活跃窗口里图里是我 → 20% 丢一张表情包（用户 m07824，原 100%）
SELF_STICKER_SILENT_P = 0.02   # 静默状态下图里是我 → 2% 回一张，且不开窗（用户 m07548）
SELF_STICKER_HINT = "大肥鱼 鲸鱼 女仆 本鱼 自己 撒娇 反驳"

# 宵禁开始/结束各发一次播报（用户 m04548）：{"night": "2026-10-22", "morning": "2026-10-22"}
CURFEW_STATE = os.path.join(HERE, "curfew_state.json")
CURFEW_WINDOW = 90    # 只在宵禁开始/结束后 90 分钟内发（单位：分钟），深夜/白天重启不会突兀地补播


def load_curfew_state():
    try:
        return json.load(open(CURFEW_STATE, encoding="utf-8-sig"))
    except Exception:
        return {}


def save_curfew_state(state):
    try:
        with open(CURFEW_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass






def load_done():
    try:
        data = json.load(open(DONE_FILE, encoding="utf-8-sig"))
    except Exception:
        return {}
    out = {}
    for room, items in data.items():
        s = set()
        for it in items or []:
            try:
                s.add((int(it[0]), str(it[1])))
            except Exception:
                continue
        out[room] = s
    return out


def save_done(done):
    data = {}
    for room, s in done.items():
        keep = sorted(s)[-200:]
        data[room] = [[t, who] for (t, who) in keep]
    try:
        with open(DONE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def load_active():
    try:
        return json.load(open(ACTIVE_FILE, encoding="utf-8-sig"))
    except Exception:
        return {}


def save_active(data):
    try:
        with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def active_left(room):
    """这个会话的活跃倒计时还剩几秒（用户 m06274）。"""
    return float(load_active().get(room, 0)) - time.time()


def active_touch(room, secs=None):
    """每次回复都重置倒计时；被 @/点名/聊吃的时也调它开窗口。"""
    data = load_active()
    data[room] = time.time() + (secs or ACTIVE_SECS)
    save_active(data)


def trigger_opens(room, why):
    """这次触发要不要开活跃窗口（用户 m07785）。

    - 被 @ / 私聊：一定开
    - 昵称开头 / 被点名 / 聊吃的：只有 10% 概率开
    - 窗口本来就开着：直接续窗（不算新开）
    """
    if why in ("被@", "私聊"):
        return True
    if active_left(room) > 0:
        return True
    return random.random() < TRIGGER_OPEN_P


def silent_gap_ok(rows_ctx, j, me=None):
    """我自己是不是已经 30 分钟没说过话了（用户 m07464 改的口径）。

    原文是「相邻两条消息间隔 ≥20 分钟」，现在改成「我最后一条消息距这条 ≥30 分钟」；
    窗口里找不到我说过话 → 也算很久没说了，返回 True。
    """
    if j <= 0:
        return False
    try:
        now_t = rows_ctx[j][4]
        last_me = None
        for k in range(j - 1, -1, -1):
            if me is None or rows_ctx[k][3] == me:
                last_me = rows_ctx[k][4]
                break
        if last_me is None:
            return True
        return (now_t - last_me).total_seconds() >= SILENT_ROLL_GAP
    except Exception:
        return False


def silent_roll(room, state, now=None):
    """静默开窗骰：5% 命中；同一群 10 分钟内只掷一次（用户 m07464）。"""
    now = now or time.time()
    if now - float(state.get(room, 0)) < SILENT_ROLL_COOL:
        return False
    state[room] = now
    save_kv(SILENT_FILE, state)
    return random.random() < SILENT_ROLL_P


# ---- 注意力机制（用户 m06485）----
# 一个活跃窗口里已经回了不少于 ATTENTION_FROM 次之后，每次想回都先掷一次：
# 5 次 → 5%、6 次 → 10%、7 次 → 15%……命中就直接静默，直到被 @ 唤醒。
# 计数只被「被@」重置（点名/聊吃的都不重置）。
ATTENTION_FROM = 5
ATTENTION_STEP = 0.05


def load_count():
    try:
        return json.load(open(COUNT_FILE, encoding="utf-8-sig"))
    except Exception:
        return {}


def save_count(data):
    try:
        with open(COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def count_get(room):
    return int(load_count().get(room, 0))


def count_inc(room):
    data = load_count()
    data[room] = int(data.get(room, 0)) + 1
    save_count(data)
    return data[room]


def count_reset(room):
    data = load_count()
    if room in data:
        data.pop(room, None)
        save_count(data)


def attention_p(n):
    """n 次回复之后的静默概率：5 次起 5%，每多一次 +5%（用户 m07464）。"""
    return 0.0 if n < ATTENTION_FROM else ATTENTION_STEP * (n - ATTENTION_FROM + 1)


def attention_roll(room):
    """返回 True 表示这一条先不回、把窗口关掉（用户 m06588）。

    注意：这里关的只是「活跃窗口」，**不是永久静默**——之后被 @、以我昵称开头、
    被点名（蓝鲸/大肥鱼/鱼娘…）或者有人聊吃的，都会照常重新开窗，
    并且会回那条最新的消息。只有掷骰用的计数器才必须靠 @ 清零。
    """
    n = count_get(room)
    p = attention_p(n)
    if p <= 0:
        return False
    if random.random() < p:
        data = load_active()
        data.pop(room, None)
        save_active(data)
        print("      %s 注意力耗尽（本窗口已回 %d 次，静默概率 %.0f%%）→ 这条不回，窗口先关"
              % (room, n, p * 100), flush=True)
        return True
    return False


def window_has_sticker(rows, me, secs=None):
    """最近的活跃窗口里，别人有没有发过表情包（用户 m06390）。"""
    now = time.time()
    span = secs or ACTIVE_SECS
    for r in rows:
        try:
            ts = r[4].timestamp()
        except Exception:
            continue
        if now - ts > span or r[3] == me:
            continue
        body = str(r[2])
        if body.startswith("<表情>") or body.startswith("（表情）"):
            return True
    return False


def load_rooms(path):
    """rooms.json 的 enabled=true 就是白名单（2026-10-25 m09032 合并）。

    以前是 rooms.json ∩ whitelist.json 两份都要，用户嫌麻烦，现在只认 rooms.json；
    whitelist.json 退化成自动生成的镜像（给 send_guard / broadcast / doctor 兜底用）。
    """
    data = json.load(open(path, encoding="utf-8-sig"))
    out = []
    for r in data:
        if not r.get("enabled", True):
            continue
        out.append(r)
    try:                                   # 顺手刷新镜像，保证三个脚本看到的一致
        with open(os.path.join(HERE, "whitelist.json"), "w", encoding="utf-8") as f:
            json.dump([r["room"] for r in out], f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception:
        pass
    return out


def mute_heads_up(db, room, acct, done, scan=10):
    """每轮都扫最近 scan 条里的 `/闭嘴`（m04751）。

    为什么不是只看最后一条：我先回别人的话、管理员那条 `/闭嘴` 落在我的回复之前，
    下一轮它就不再是「我最后一条消息之后」的欠账，永远吃不到（19:20 群里实测）。
    这里同时处理两件事：
      - 管理员写了分钟数 → 立刻 set_mute（群里只停本群、私聊按 scope_for）
      - 路人提到 `/闭嘴` → 回固定台词（静音期间也要能回，用户 m04067）
    返回 True 表示这一轮改了静音状态。
    """
    try:
        rows, _ = L.transcript(db, room, acct, scan)
    except Exception as e:
        print("  扫 /闭嘴 读失败 %s: %s" % (room[:12], str(e)[:40]), flush=True)
        return False
    if not rows:
        return False
    dset = done.setdefault(room, set())
    changed = False
    now = time.time()
    for row in rows:
        if row[3] == acct or not L.has_mute_cmd(row[2]):
            continue
        tkey = (int(row[4].timestamp()), row[3])
        if tkey in dset:
            continue
        if now - row[4].timestamp() > 120:     # 两分钟前的命令不再补执行（防止重启后翻旧账）
            dset.add(tkey)
            continue
        mins = L.find_mute(row[2])
        is_admin = row[3] in L.load_admins()
        if is_admin and mins is not None:
            scope = L.scope_for(room)
            if mins <= 0:
                prev = mute_window(room)          # `/闭嘴 0` 会把窗口清掉，先记着
                if prev:                          # 手动解除：窗口就地结在这一刻
                    MUTE_LAST[room] = (prev[0], min(prev[1], time.time() - 1))
            L.set_mute(mins, scope=scope, by=row[3])
            if mins <= 0:
                print("  %s 管理员 /闭嘴 0，解除静音（作用域 %s）" % (room[:12], scope), flush=True)
            else:
                print("  %s 管理员 /闭嘴 %d，静音刷新（作用域 %s）"
                      % (room[:12], mins, scope), flush=True)
            dset.add(tkey)
            save_done(done)
            changed = True
        elif not is_admin:
            if L.mute_left(room) > 0:
                # 静音期间不回杂鱼台词（用户 m07608）；只记一笔，免得解除后又补发
                dset.add(tkey)
                continue
            print("  %s 非管理员提到 /闭嘴 → 回固定台词" % room[:12], flush=True)
            try:
                send_lines(room, acct, [MUTE_TAUNT], True, "taunt")
            except RuntimeError:
                print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            dset.add(tkey)
            save_done(done)
    return changed


MUTE_PREV = {}          # room -> 上一轮是否在静音（用来判断「刚解除」）
MUTE_FIRED = set()      # (room, until) 已经冒过话的静音段
MUTE_LAST = {}          # room -> 上一段静音窗口（`/闭嘴 0` 会把 mute.json 清空，得先记着）

PAT_REPLY_P = 1.0      # 被拍就回（用户 m06588）        # 被拍一拍之后，只有 20% 概率回一句（用户 m05541）
PAT_LAST = {}
PAT_STREAK = {}
PAT_OPEN = {}            # room -> 已经放回正常流程、还没回完的那一拍（防重复）          # room -> 最近 10 分钟被拍了几下（拍多了会急眼，m06588）           # room -> 上次回拍的时间（只用于日志）


def pat_beat(db, room, acct, done, scan=8):
    """别人拍我一拍：被拍就回一句（交给正常出稿流程），其余直接标记已处理。

    不主动发「（拍了拍X）」：Windows 微信 4.x 客户端没有拍一拍入口，hook 也没有这个接口，
    发不出真正的拍一拍，所以干脆只按概率回话（用户 m05541）。
    """
    if L.mute_left(room) > 0 or L.in_curfew():
        return False
    try:
        msgs, _ = db.get_messages(room, limit=scan, offset=0)
    except Exception:
        return False
    dset = done.setdefault(room, set())
    now = time.time()
    fresh = []          # [(ts, 谁, tkey)] 还没处理过的「拍我」
    for m in msgs:
        body = L.decode_content(m.get("message_content") or "")
        info = L.pat_info(body, acct)
        if not info:
            continue
        ts = int(m.get("create_time") or 0)
        tkey = (ts, info["from"])
        if tkey in dset:
            continue
        fresh.append((ts, info["from"], tkey))
    if not fresh:
        return False
    fresh.sort()
    ts, frm, tkey = fresh[-1]          # 一次只处理最新那一拍，旧的直接作废
    if PAT_OPEN.get(room) == tkey:     # 这条已经放回去了，等正常流程回完，别重复放
        return False
    for _ts, _frm, _k in fresh:
        dset.add(_k)
    save_done(done)
    if now - ts > 600:                 # 十几分钟前的拍别补
        return False
    if random.random() >= PAT_REPLY_P:
        return False
    try:
        PAT_STREAK[room] = len([1 for m in msgs
                                 if now - int(m.get("create_time") or 0) <= 600
                                 and L.pat_info(L.decode_content(m.get("message_content") or ""), acct)])
    except Exception:
        PAT_STREAK[room] = 1
    dset.discard(tkey)                 # 抽中 → 留给下面的正常流程回一句
    PAT_OPEN[room] = tkey
    save_done(done)
    PAT_LAST[room] = now
    print("  %s 被 %s 拍了 → 回一句" % (room, frm), flush=True)
    return True


def mute_window(room):
    """这次静音的开始/结束时间；读原始 mute.json（过期条目也还留在里面）。"""
    try:
        d = json.load(open(L.MUTE_FILE, encoding="utf-8-sig"))
    except Exception:
        return None
    scopes = d.get("scopes") or {}
    until = 0.0
    for k, v in scopes.items():
        if not (k == "all" or k == room or k.startswith("all_except:")):
            continue
        try:
            u = float(v.get("until", 0) if isinstance(v, dict) else v)
        except Exception:
            continue
        until = max(until, u)
    if until <= 0:
        return None
    try:
        mins = float(d.get("minutes") or 0)
    except Exception:
        mins = 0
    start = until - mins * 60 if mins > 0 else until - 300
    return start, until


def mute_end_beat(db, room, acct, name):
    """静音刚解除：这段时间里有人说话 → 冒一句「憋死我了」（用户 m04866）。"""
    now = time.time()
    win = mute_window(room)
    if win:
        start, until = win
        if now < until + 1:
            return                              # 其实还在静音
    else:
        # `/闭嘴 0` 会把 mute.json 清空，窗口只能靠静音期间记下的那份
        win = MUTE_LAST.get(room)
        if not win:
            return
        start, until = win
        until = min(until, now - 1)              # 被手动解除：就当刚刚结束
    MUTE_LAST.pop(room, None)
    if now - until > 120:
        return                                  # 过期太久（重启/睡过去了）不补
    if (room, int(until)) in MUTE_FIRED:
        return                                  # 这一段已经冒过
    try:
        rows, _ = L.transcript(db, room, acct, 20)
    except Exception:
        return
    if not rows:
        return
    spoke = [r for r in rows if r[3] != acct and r[4].timestamp() > start]
    if not spoke:
        MUTE_FIRED.add((room, int(until)))       # 期间没人说话就不吭声
        print("  %s 静音解除，期间没人说话 → 不冒" % name, flush=True)
        return
    lines = L.gen_mute_end(rows, not room.endswith("@chatroom"))
    if not lines:
        return
    MUTE_FIRED.add((room, int(until)))
    print("  %s 静音解除（期间 %d 条新消息）→ 冒一句：%s"
          % (name, len(spoke), " / ".join(lines)), flush=True)
    send_lines(room, acct, lines[:1], False, "mute_end")


def admin_private_bypass(db, room, acct):
    """静音期间，管理员在**自己的私聊**里说话照样处理。"""
    if room.endswith("@chatroom"):
        return False
    try:
        rows, _ = L.transcript(db, room, acct, 3)
    except Exception:
        return False
    if not rows:
        return False
    last = rows[-1]
    if last[3] == acct:
        return False
    return last[3] == room and last[3] in L.load_admins()


def curfew_beat(rooms, dbs, state, i, now=None):
    """宵禁开始给每个白名单群道晚安、宵禁结束问早安，每群每条每天只发一次（用户 m04548）。

    - 只发群聊（`@chatroom`），私聊不打扰
    - 内容由各群最近 14 条上下文生成（`L.gen_curfew`）
    - 发送要穿过 send_guard 的宵禁 + 静音闸（bypass_curfew / bypass_mute）
    """
    now = now or datetime.datetime.now()
    c = L.load_settings().get("curfew") or {}
    start, end = c.get("start", "00:30"), c.get("end", "07:30")
    sm = int(start.split(":")[0]) * 60 + int(start.split(":")[1])
    em = int(end.split(":")[0]) * 60 + int(end.split(":")[1])
    mins = now.hour * 60 + now.minute
    day = now.strftime("%Y-%m-%d")

    night_now = L.in_curfew(now)
    kind = None
    if night_now and state.get("night") != day and 0 <= mins - sm < CURFEW_WINDOW:
        kind = "night"
    elif (not night_now) and state.get("morning") != day and 0 <= mins - em < CURFEW_WINDOW:
        kind = "morning"
    if not kind:
        return 0

    groups = [r for r in rooms if r["room"].endswith("@chatroom")]
    ok_all = 0
    for r in groups:
        room, acct = r["room"], r["account"]
        if L.mute_left(room) > 0:
            # 群被 /闭嘴 了就不播报晚安/早安（用户 m07608）
            print("宵禁播报跳过 %s（群在静音中）" % r["name"], flush=True)
            continue
        try:
            rows, _raw = L.transcript(dbs[acct], room, acct, n=14)
        except Exception as e:
            print("宵禁播报读失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
            continue
        if not rows:
            continue
        draft = L.gen_curfew(rows, night=(kind == "night"))
        lines = [x.strip() for x in (draft or "").splitlines() if x.strip()][:2]
        if not lines:
            print("宵禁播报出稿失败 %s，跳过" % r["name"], flush=True)
            continue
        print("[%d] %s %s: %s" % (i, r["name"], "道晚安" if kind == "night" else "问早安",
                                  " / ".join(lines)), flush=True)
        try:
            n_ok = send_lines(room, acct, lines, True, "curfew", bypass_curfew=True)
        except RuntimeError:
            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            raise
        if n_ok:
            ok_all += 1
        time.sleep(random.uniform(LINE_GAP_MIN, LINE_GAP_MAX))
    state[kind] = day
    save_curfew_state(state)
    print("宵禁播报（%s）完成 %d/%d 个群" % ("晚安" if kind == "night" else "早安", ok_all, len(groups)),
          flush=True)
    return ok_all


def load_kv(path):
    try:
        return json.load(open(path, encoding="utf-8-sig"))
    except Exception:
        return {}


def save_kv(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def _as_lines(draft, max_n=2):
    if isinstance(draft, (list, tuple)):
        out = [str(x).strip() for x in draft if str(x).strip()]
    else:
        out = [x.strip() for x in (draft or "").splitlines() if x.strip()]
    return out[:max_n]


def last_msg_ts(rows):
    """最近一条消息（含自己）的时间戳。"""
    best = 0.0
    for r in rows:
        try:
            best = max(best, r[4].timestamp())
        except Exception:
            pass
    return best


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def idle_topic_beat(rooms, dbs, state, i, now=None):
    """群聊静 1 小时后，每 10 分钟掷 10% 主动抛个话题（用户 m06545 / m06842，只群聊）。"""
    now = now or datetime.datetime.now()
    if L.in_curfew(now):
        return 0
    now_ts = now.timestamp()
    sent = 0
    for r in rooms:
        room, acct = r["room"], r["account"]
        if not room.endswith("@chatroom"):
            continue
        if L.mute_left(room) > 0:
            continue        # 群被 /闭嘴 了就不主动抛话题（用户 m07583）
        try:
            rows, _raw = L.transcript(dbs[acct], room, acct, n=6)
        except Exception:
            continue
        last = last_msg_ts(rows)
        if not last:
            continue
        if now_ts - last < IDLE_TOPIC_AFTER:
            if state.pop(room, None) is not None:
                save_kv(IDLE_FILE, state)
            continue
        if now_ts - float(state.get(room, 0) or 0) < IDLE_TOPIC_EVERY:
            continue
        state[room] = now_ts
        save_kv(IDLE_FILE, state)
        if random.random() >= IDLE_TOPIC_P:
            print("[%d] %s 静了 %d 分钟，没抽中 10%%"
                  % (i, r["name"], int((now_ts - last) // 60)), flush=True)
            continue
        try:
            draft = L.gen_topic(rows)
        except Exception as e:
            print("起话题出稿失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
            continue
        lines = _as_lines(draft)
        if not lines:
            continue
        print("[%d] %s 静了 %d 分钟，抽中 10%% → 抛话题: %s"
              % (i, r["name"], int((now_ts - last) // 60), " / ".join(lines)), flush=True)
        try:
            n_ok = send_lines(room, acct, lines, False, "idle")
        except RuntimeError:
            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            raise
        if n_ok:
            sent += 1
    return sent


def meal_beat(rooms, dbs, state, i, now=None):
    """饭点（11:30-12:30 / 17:00-19:00）随机挑一个时间点说今天吃了什么（用户 m06545）。

    到点了再看这个群最近 20 分钟有没有消息，没有就不发（用户 m07464）。
    """
    now = now or datetime.datetime.now()
    if L.in_curfew(now):
        return 0
    mins = now.hour * 60 + now.minute
    day = now.strftime("%Y-%m-%d")
    now_ts = now.timestamp()
    sent = 0
    for idx, (a, b) in enumerate(MEAL_WINDOWS):
        s, e = _mins(a), _mins(b)
        if not (s <= mins < e):
            continue
        for r in rooms:
            room, acct = r["room"], r["account"]
            if not room.endswith("@chatroom"):
                continue
            if L.mute_left(room) > 0:
                continue    # 群被 /闭嘴 了就不报菜单（用户 m07583）
            key = "%s|%d|%s" % (room, idx, day)
            rec = state.get(key)
            if rec is None:
                base = now.replace(hour=s // 60, minute=s % 60, second=0, microsecond=0).timestamp()
                pick = base + random.randint(0, (e - s) * 60 - 1)
                rec = {"at": day, "pick": pick, "sent": False}
                state[key] = rec
                save_kv(MEAL_FILE, state)
                print("[%d] %s 饭点 %s-%s 挑了 %s 发"
                      % (i, r["name"], a, b, time.strftime("%H:%M", time.localtime(pick))), flush=True)
            if rec.get("sent") or now_ts < float(rec.get("pick") or 0):
                continue
            try:
                rows, _raw = L.transcript(dbs[acct], room, acct, n=20)
            except Exception:
                continue
            if now_ts - last_msg_ts(rows) > MEAL_RECENT:
                rec["sent"] = True
                save_kv(MEAL_FILE, state)
                print("[%d] %s 到饭点了，但最近半小时没消息 → 不发" % (i, r["name"]), flush=True)
                continue
            try:
                draft = L.gen_meal(rows)
            except Exception as e:
                print("饭点出稿失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
                continue
            lines = _as_lines(draft)
            if not lines:
                continue
            print("[%d] %s 饭点报菜单: %s" % (i, r["name"], " / ".join(lines)), flush=True)
            try:
                n_ok = send_lines(room, acct, lines, False, "meal")
            except RuntimeError:
                print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                raise
            if n_ok:
                rec["sent"] = True
                save_kv(MEAL_FILE, state)
                sent += 1
    return sent


def send_sticker(room, hint):
    """按当前话题/关键词从库里挑一张表情包发出去（只走 hook，不动界面）。"""
    path = STICKER.pick(hint or "")
    if not path:
        print("    表情包：库里没挑到（%s）" % (hint or "").strip()[:24], flush=True)
        return False
    time.sleep(random.uniform(1.0, 2.0))     # 文字发完停一下再补图，像人
    ok, info = HS.send_image(room, path)
    if ok:
        STICKER.mark_used(os.path.basename(path))
        print("    表情包: %s 已发（hook）" % os.path.basename(path), flush=True)
        return True
    print("    表情包发送失败: %s" % (info,), flush=True)
    return False


def send_lines(room, acct, lines, bypass_mute, tag, bypass_curfew=False):
    """逐条发；返回发成功的条数。rc=9 直接抛出去（消息发错会话，必须停机）。"""
    env = dict(os.environ)
    if bypass_mute:
        env["WX_BYPASS_MUTE"] = "1"     # 管理员私聊的豁免要一路传到发送器，否则被 rc=8 挡住
    if bypass_curfew:
        env["WX_BYPASS_CURFEW"] = "1"   # 宵禁播报要能穿过 send_guard 的宵禁检查
    ok_any = 0
    for idx, one in enumerate(lines):
        if idx > 0:
            env["WX_TRUST_ROOM"] = room
            env["WX_TRUST_UNTIL"] = str(time.time() + 25)
        p = subprocess.run([sys.executable, SEND, room, acct, one],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", env=env)
        out = (p.stdout or "").strip().replace("\n", " ")[:60]
        print("    %s[%d] rc=%s %s" % (tag, idx, p.returncode, out), flush=True)
        if p.returncode not in (0, 4):
            # 非正常返回时把 stderr 也打出来（rc=1 常见是 send_guard 自己崩了）
            err = (p.stderr or "").strip().replace("\n", " ")[:300]
            if err:
                print("      stderr: %s" % err, flush=True)
        if p.returncode == 9:
            raise RuntimeError("发到别的会话了")
        if p.returncode in (0, 4):
            ok_any += 1
            env["WX_TRUST_ROOM"] = room
            env["WX_TRUST_UNTIL"] = str(time.time() + 25)
        elif p.returncode == 3:
            print("    跳过了：证明不了当前会话就是它", flush=True)
            break
        if idx < len(lines) - 1:
            time.sleep(random.uniform(LINE_GAP_MIN, LINE_GAP_MAX))   # 2~4 秒随机（m04417）
    return ok_any


def sticker_args(rows_ctx, acct, txt):
    """这一条回复要不要配表情包 → (sticker_p, sticker_hot)。

    拍一拍一律不配（用户 m06805）。其余交给模型按语境判断（用户 m07954）：
    sticker_p=None 表示「允许，但由模型决定」；sticker_hot=True 表示活跃窗口里
    别人刚发过表情包，可以顺着接一张。
    """
    if "拍了拍" in str(txt):
        return 0.0, False
    return None, window_has_sticker(rows_ctx, acct)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rooms", default=os.path.join(HERE, "rooms.json"))
    ap.add_argument("--rounds", type=int, default=999999)
    ap.add_argument("--gap", type=float, default=2.0)
    args = ap.parse_args()

    rooms = load_rooms(args.rooms)
    if not rooms:
        print("还没有启用任何会话（rooms.json 是空的）。", flush=True)
        print("→ 打开面板 http://127.0.0.1:8790 的「名单」页添加，或直接编辑 rooms.json", flush=True)
        print("→ 这里每 10 秒重读一次名单，配置好会自动开始；按 Ctrl+C 退出", flush=True)
        while not rooms:
            time.sleep(10)
            rooms = load_rooms(args.rooms)
        print("名单读到了：%d 个会话，开始运行" % len(rooms), flush=True)
    dbs = {}
    names = {}
    for r in rooms:
        if r["account"] not in dbs:
            dbs[r["account"]] = L.open_db(r["account"])
            names[r["account"]] = L.my_name(dbs[r["account"]], r["account"])
            print("本号昵称:", names[r["account"]] or "(未知)", flush=True)
        print("盯:", r["name"], r["room"], flush=True)

    done = load_done()      # room -> {(ts, 发送者)} 已处理（回过 / 判过不回 / 太老）
    _bypass_log = {}        # room -> 上次打「管理员私聊豁免」日志的时间（防刷屏）
    curfew_state = load_curfew_state()   # {"night": 日期, "morning": 日期}
    idle_state = load_kv(IDLE_FILE)      # room -> 上次掷话题的时间（m06545）
    meal_state = load_kv(MEAL_FILE)      # 饭点挑的时间点 / 是否已发（m06545）
    silent_state = load_kv(SILENT_FILE)  # room -> 上次掷「久静开窗」的时间（m06873）

    for i in range(args.rounds):
        # 面板改的参数：写进 tuning.json，这里每轮贴一次（mtime 没变就是空操作）
        TUNE.apply(sys.modules[__name__])
        TUNE.apply(L)
        try:   # 心跳：控制面板靠它判断机器人还活着（m08700）
            with open(os.path.join(HERE, "bot_heartbeat.json"), "w", encoding="utf-8") as _hb:
                json.dump({"ts": time.time(), "pid": os.getpid(), "round": i}, _hb)
        except OSError:
            pass
        # 面板改名单后不用重启：每轮重读一次 rooms.json（m09094）
        try:
            _rooms = load_rooms(args.rooms)
            if _rooms:
                if [r["room"] for r in _rooms] != [r["room"] for r in rooms]:
                    print("名单有变化 → 现在盯 %d 个会话" % len(_rooms), flush=True)
                rooms = _rooms
                for _r in rooms:
                    if _r["account"] not in dbs:
                        dbs[_r["account"]] = L.open_db(_r["account"])
                        names[_r["account"]] = L.my_name(dbs[_r["account"]], _r["account"])
        except Exception as _e:
            print("重读名单失败（沿用上一份）:", _e, flush=True)

        try:
            curfew_beat(rooms, dbs, curfew_state, i)   # 宵禁开始道晚安 / 结束问早安（m04548）
        except RuntimeError:
            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            return 9
        try:
            idle_topic_beat(rooms, dbs, idle_state, i)   # 群里静太久 → 抛话题（m06545）
            meal_beat(rooms, dbs, meal_state, i)         # 饭点报菜单（m06545）
        except RuntimeError:
            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            return 9
        if L.in_curfew():
            if i % 30 == 0:
                print("宵禁中（%s-%s），不读不回，等着" % ("00:30", "07:30"), flush=True)
            time.sleep(60)
            continue
        for r in rooms:
            room, acct = r["room"], r["account"]
            try:
                mute_heads_up(dbs[acct], room, acct, done)   # 每轮都扫最近的 /闭嘴（m04751）
                pat_beat(dbs[acct], room, acct, done)        # 被拍一拍 → 拍回去（m05350）
            except RuntimeError:
                print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                return 9
            left = L.mute_left(room)
            bypass_mute = False
            was_muted = MUTE_PREV.get(room)
            MUTE_PREV[room] = left > 0
            if left > 0:
                # 静音期间把窗口记下来：`/闭嘴 0` 会把 mute.json 清空，到解除时就没地方查了
                win = mute_window(room)
                if win:
                    MUTE_LAST[room] = win
            if was_muted and left <= 0:
                # 静音刚解除：这段时间里有人说话的话，冒一句「憋死我了」（用户 m04866）
                try:
                    mute_end_beat(dbs[acct], room, acct, r["name"])
                except RuntimeError:
                    print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                    return 9
                except Exception as e:
                    print("  解禁提示失败 %s: %s" % (r["name"], str(e)[:60]), flush=True)
            if left > 0:
                if left > 0 and not admin_private_bypass(dbs[acct], room, acct):
                    if i % 30 == 0:
                        print("%s 静音中，还剩 %d 秒" % (r["name"], int(left)), flush=True)
                    continue
                if left > 0:
                    bypass_mute = True
                    if time.time() - _bypass_log.get(room, 0) > 60:
                        _bypass_log[room] = time.time()
                        print("%s 静音中，但管理员私聊 → 照常回" % r["name"], flush=True)
                else:
                    print("%s 静音已解除，继续" % r["name"], flush=True)
            db = dbs[acct]
            try:
                rows, raw = L.transcript(db, room, acct)
            except Exception as e:
                print("  读失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
                continue
            if not rows:
                continue
            raw_by_ts = {}
            for m in (raw or []):
                try:
                    raw_by_ts[int(m.get("create_time") or 0)] = m
                except Exception:
                    pass

            pend = L.pending_after_me(rows, acct)
            if not pend:
                continue                     # 我已经是最后说话的人，没欠账
            rows_ctx = list(rows)
            base = len(rows) - len(pend)
            dset = done.setdefault(room, set())
            todo = []                        # [(tkey, 谁, 内容)]
            acted = False
            is_group = room.endswith("@chatroom")

            for off, row in enumerate(pend):
                j = base + off
                tkey = (int(row[4].timestamp()), row[3])
                if tkey in dset:
                    continue
                if time.time() - row[4].timestamp() > PENDING_AGE:
                    dset.add(tkey)           # 超过 3 分钟的翻篇，不补
                    save_done(done)
                    continue
                if row[3] == acct:
                    dset.add(tkey)
                    continue

                # 活跃期：私聊默认一直活跃；群里要 1 分钟倒计时内（用户 m07728）
                active = (not is_group) or active_left(room) > 0

                # ---- 拍一拍：只有抽中的才会走到这里（没抽中的已被 pat_beat 标记处理掉）----
                if str(row[2]).startswith("<拍"):
                    # 拍一拍消息的 sender 是空的，名字要从 patinfo 里捞（不然日志里全是 wxid）
                    raw_pat = raw_by_ts.get(int(row[4].timestamp())) or {}
                    _raw_pat_body = str(raw_pat.get("message_content") or "")
                    # 只在「拍我」时才算（用户 m09722：别人拍别人不该开窗、也不该回）
                    if not L.pat_info(L.decode_content(_raw_pat_body), acct):
                        dset.add(tkey)
                        save_done(done)
                        print("[%d] %s 这拍一拍不是拍我 → 跳过" % (i, r["name"]), flush=True)
                        continue
                    pat_wxid = L.pat_from(_raw_pat_body)
                    who = (L.contact_name(dbs[acct], pat_wxid) if pat_wxid else "") or row[1] or "有人"
                    rows_ctx[j] = (row[0], row[1], "（%s 拍了拍我）" % who, row[3], row[4])
                    streak = PAT_STREAK.get(room, 1)
                    if streak >= 3:
                        pat_txt = "拍了拍我（连着被拍 %d 下，已经有点急了）" % streak
                    elif streak == 2:
                        pat_txt = "拍了拍我（第二下）"
                    else:
                        pat_txt = "拍了拍我"
                    todo.append((tkey, who, pat_txt))
                    dset.add(tkey)           # 这里就记 done：免得下一轮又被 pat_beat 放回来
                    PAT_OPEN.pop(room, None)
                    save_done(done)
                    # 拍一拍只开 10 秒窗口（用户 m07728：原来 2 分钟，改成 10 秒）
                    was_closed = active_left(room) <= 0
                    active_touch(room, PAT_ACTIVE_SECS)
                    print("[%d] %s 被 %s 拍了，回一句%s"
                          % (i, r["name"], who, "（顺手开活跃窗口）" if was_closed else ""),
                          flush=True)
                    continue

                # ---- 图片 / 表情：识别 → 在活跃期里就按普通消息走（用户 m06759）----
                if L.media_pending(row):
                    desc = L.describe_media(raw_by_ts.get(int(row[4].timestamp())), room, acct)
                    if not desc:
                        print("[%d] %s %s 识别失败（三次都没成）→ 记为无法识别，跳过"
                              % (i, r["name"], L.media_tag(row)), flush=True)
                        dset.add(tkey)
                        save_done(done)
                        continue
                    tag = L.media_tag(row)
                    row = (row[0], row[1], "（%s）%s" % (tag, desc), row[3], row[4])
                    rows_ctx[j] = row
                    print("[%d] %s %s 识别: %s" % (i, r["name"], tag, desc[:40]), flush=True)
                    is_me = L.media_is_self(desc)
                    if tag == "表情" and is_me:
                        # 只存「识别成我」的表情包（用户 m06225）；别的表情一律不入库
                        try:
                            sp = L.VIS.cached_path(raw_by_ts.get(int(row[4].timestamp())), acct)
                            if sp:
                                STICKER.save_sticker(sp, desc, chat_json=L.chat_json)
                        except Exception as _e:
                            print("    存表情包异常: %s" % _e, flush=True)
                    if is_me:
                        if active:
                            # 活跃窗口里图里是我 → 20% 丢一张表情包并续窗（用户 m07824，原 100%）
                            if random.random() < SELF_STICKER_P:
                                if send_sticker(room, SELF_STICKER_HINT + " " + desc[:24]):
                                    print("[%d] %s 图里是我，抽中 20%% → 回一张表情包并续窗"
                                          % (i, r["name"]), flush=True)
                                    acted = True
                                    active_touch(room)
                                else:
                                    print("[%d] %s 图里是我，抽中 20%% 但库里没挑到能发的"
                                          % (i, r["name"]), flush=True)
                            else:
                                print("[%d] %s 图里是我，没抽中 20%% → 不回"
                                      % (i, r["name"]), flush=True)
                        else:
                            # 静默状态里图里是我 → 2% 回一张表情包，但不开活跃窗口（用户 m07548）
                            if random.random() < SELF_STICKER_SILENT_P:
                                if send_sticker(room, SELF_STICKER_HINT + " " + desc[:24]):
                                    print("[%d] %s 静默中图里是我，抽中 2%% → 回一张表情包（不开窗）"
                                          % (i, r["name"]), flush=True)
                                    acted = True
                                else:
                                    print("[%d] %s 静默中图里是我，抽中了但库里没挑到能发的"
                                          % (i, r["name"]), flush=True)
                            else:
                                print("[%d] %s 静默中图里是我，没抽中 2%% → 不回"
                                      % (i, r["name"]), flush=True)
                        dset.add(tkey)
                        save_done(done)
                        continue
                    if not active and silent_gap_ok(rows_ctx, j, acct) and silent_roll(room, silent_state):
                        active_touch(room)
                        active = True
                        print("[%d] %s %s 静默 20 分钟以上，掷中 5%% → 直接开活跃窗口"
                              % (i, r["name"], tag), flush=True)
                    if not active:
                        print("[%d] %s %s 跳过（不在活跃期，不回）"
                              % (i, r["name"], tag), flush=True)
                        dset.add(tkey)
                        save_done(done)
                        continue
                    # 在活跃期里 → 不 continue，落到下面的「普通消息」判定
                    # （should_reply + relates_to_recent，跟文字一样，用户 m06759）

                # ---- /闭嘴 ----
                if L.has_mute_cmd(row[2]):
                    mins = L.find_mute(row[2])
                    is_admin = row[3] in L.load_admins()
                    if is_admin and mins is not None:
                        scope = L.scope_for(room)
                        L.set_mute(mins, scope=scope, by=row[3])
                        if scope == "all":
                            where = "所有会话"
                        elif scope.startswith("all_except:"):
                            where = "除管理员私聊以外的所有会话"
                        else:
                            where = "本群"
                        print("[%d] %s 管理员 /闭嘴 %d，静音%s %d 分钟"
                              % (i, r["name"], mins, where, mins), flush=True)
                        dset.add(tkey)
                        save_done(done)
                        todo = []       # 指令一下，这一轮剩下的欠账也不再回（m04751）
                        break
                    if not is_admin:
                        print("[%d] %s 非管理员提到 /闭嘴 → 回固定台词" % (i, r["name"]), flush=True)
                        try:
                            if send_lines(room, acct, [MUTE_TAUNT], False, "taunt"):
                                acted = True
                                dset.add(tkey)
                                save_done(done)
                        except RuntimeError:
                            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                            return 9
                        continue
                    print("[%d] %s 管理员提到 /闭嘴 但没写分钟数，当普通消息"
                          % (i, r["name"]), flush=True)

                # ---- 普通消息 ----
                ok, why = L.should_reply(rows_ctx[:j + 1], names.get(acct, ""), not is_group)
                if ok and why in TRIGGER_WHY:
                    # 被 @ / 私聊 → 一定开窗；昵称开头 / 被点名 / 聊吃的 → 只有 10%（用户 m07785）
                    if trigger_opens(room, why):
                        was_closed = active_left(room) <= 0
                        active_touch(room)
                        active = True
                        if was_closed:
                            print("[%d] %s 触发了（%s）→ 重新开窗"
                                  % (i, r["name"], why), flush=True)
                    else:
                        print("[%d] %s 触发了（%s）但没抽中 10%% → 不开窗"
                              % (i, r["name"], why), flush=True)
                if why == "被@":
                    # 只有「被 @」重置注意力计数（用户 m06485）
                    count_reset(room)
                targeted = why in ("被@", "点名开头", "被点名", "私聊")
                if why == "聊吃的":
                    # 聊吃的不走"跟前面搭不搭"那道闸：检测到好吃的直接回（用户 m04448）
                    why = "聊吃的（直接回）"
                elif not targeted:
                    mine = [x[2] for x in rows_ctx[:j + 1] if x[3] == acct][-5:]
                    rel, rwhy = L.relates_to_recent(mine, row[2])
                    if rel and ok:
                        why = "%s+接着我说的（%s）" % (why, rwhy)
                    elif rel:
                        ok, why = True, "接着我说的（%s）" % rwhy
                    else:
                        ok, why = False, "跟前面不搭，不接（%s）" % rwhy
                if not ok:
                    print("[%d] %s 跳过（%s）: %s" % (i, r["name"], why, row[2][:26]), flush=True)
                    dset.add(tkey)
                    save_done(done)
                    continue
                if not active and silent_gap_ok(rows_ctx, j, acct) and silent_roll(room, silent_state):
                    active_touch(room)
                    active = True
                    print("[%d] %s 静默 20 分钟以上，掷中 5%% → 直接开活跃窗口"
                          % (i, r["name"]), flush=True)
                if not active:
                    print("[%d] %s 跳过（不在活跃期，静默）: %s"
                          % (i, r["name"], row[2][:26]), flush=True)
                    dset.add(tkey)
                    save_done(done)
                    continue
                if is_group and random.random() < REPLY_LOSS_P:   # 私聊不丢（用户 m09368）
                    print("[%d] %s 活跃期里抽中 10%% 丢失（这条不回，后面照常）: %s"
                          % (i, r["name"], row[2][:26]), flush=True)
                    dset.add(tkey)
                    save_done(done)
                    continue
                if is_group and attention_roll(room):            # 私聊不静默（用户 m09368）
                    # 注意力耗尽：这一条也不回了，直接静默到下次被 @（用户 m06485）
                    dset.add(tkey)
                    save_done(done)
                    continue
                todo.append((tkey, row[1], row[2]))
                print("[%d] %s 待回（%s）: %s" % (i, r["name"], why, row[2][:26]), flush=True)

            if not todo:
                continue
            todo = todo[-MAX_LINES:]         # 欠太多条时先回最近的几条，剩下的下一轮继续
            for n, (tkey, who, txt) in enumerate(todo, 1):
                # 一条欠账一次出稿：允许 1~3 条气泡，但不同欠账分开回、不混（用户 m03439）
                sp, sh = sticker_args(rows_ctx, acct, txt)
                draft = L.gen(rows_ctx, not is_group, todo=[(who, txt, tkey[0])],
                              sticker_p=sp, sticker_hot=sh)
                # gen() 现在返回 list（可能最后一行是 [表情:关键词]）；老版本返回字符串，两种都认
                if isinstance(draft, (list, tuple)):
                    lines = [str(x).strip(" -·") for x in draft if str(x).strip()]
                else:
                    draft = (draft or "").strip()
                    lines = [x.strip(" -·") for x in draft.splitlines() if x.strip()]
                lines = [x for x in lines if x][:MAX_LINES + 1]
                if not lines:
                    continue
                # [表情:关键词] 那一行不是文字：抽出来，文字发完再补一张表情包（m05769）
                sticker_kw = None
                kept = []
                for one in lines:
                    m = L.STICKER_TAG_RE.match(one)
                    if m:
                        if sticker_kw is None:
                            sticker_kw = (m.group(1) or "").strip()
                    else:
                        kept.append(one)
                lines = kept
                if not lines and sticker_kw is None:
                    continue
                print("[%d] %s 回欠账 %d/%d（%s）%d 条: %s"
                      % (i, r["name"], n, len(todo), txt[:20], len(lines),
                         " / ".join(lines)), flush=True)
                n_ok = 0
                if lines:
                    try:
                        n_ok = send_lines(room, acct, lines, bypass_mute, "send")
                    except RuntimeError:
                        print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                        return 9
                if sticker_kw is not None and (n_ok or not lines):
                    try:
                        send_sticker(room, sticker_kw + " " + txt)
                    except Exception as e:
                        print("    表情包发送失败: %s" % e, flush=True)
                if n_ok:
                    dset.add(tkey)
                    save_done(done)
                    acted = True
                    active_touch(room)      # 每次回复都重置活跃倒计时（1 分钟，用户 m07728）
                    # 注意力计数：一次「回复」算一次，不数气泡（m06485）；
                    # 私聊不掷注意力骰，也不累加计数（用户 m09368）
                    cnt = count_inc(room) if is_group else count_get(room)
                    np_ = attention_p(cnt) if is_group else 0.0
                    if np_:
                        print("      %s 本窗口已回 %d 次，下次静默概率 %.0f%%"
                              % (r["name"], cnt, np_ * 100), flush=True)
                time.sleep(0.5)
            if acted:
                continue
        time.sleep(max(args.gap, 0.3))


if __name__ == "__main__":
    main()
