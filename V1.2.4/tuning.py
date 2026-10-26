# -*- coding: utf-8 -*-
"""wx-chatbot 的可调参数（面板改这里，主循环下一轮就生效，不用重启）。

优先级：tuning.json > 代码里的默认值。
只放「随时可以改、改了立刻见效」的旋钮；会改结构的（比如换账号、换库）不放这。
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TUNING_FILE = os.path.join(HERE, "tuning.json")

# 字段 → (默认值, 类型, 说明, 最小, 最大)
SPEC = {
    # ---- 活跃窗口 ----
    "ACTIVE_SECS": (60, float, "对话触发的活跃窗口（秒）", 5, 3600),
    "PAT_ACTIVE_SECS": (10, float, "拍一拍触发的活跃窗口（秒）", 0, 600),
    "PENDING_AGE": (180, float, "只追最近多少秒还没回的欠账", 30, 86400),
    "TRIGGER_OPEN_P": (0.10, float, "昵称开头/被点名/聊吃的 开窗概率（被@和私聊恒开）", 0, 1),
    # ---- 静默 ----
    "SILENT_ROLL_GAP": (1800, float, "我自己多久没说话才掷「久静开窗」（秒）", 60, 86400),
    "SILENT_ROLL_P": (0.05, float, "久静开窗概率", 0, 1),
    "SILENT_ROLL_COOL": (600, float, "久静开窗冷却（秒）", 0, 86400),
    "REPLY_LOSS_P": (0.10, float, "活跃期内正常回复的丢失概率（只对群聊，私聊不丢）", 0, 1),
    # ---- 注意力 ----
    "ATTENTION_FROM": (5, float, "一个窗口里回过这么多条之后开始加静默概率（只对群聊）", 1, 100),
    "ATTENTION_STEP": (0.05, float, "每多回一条，静默概率加多少", 0, 1),
    # ---- 主动说话 ----
    "IDLE_TOPIC_AFTER": (3600, float, "群静多少秒后才考虑主动抛话题", 60, 86400),
    "IDLE_TOPIC_EVERY": (1800, float, "之后每隔多少秒掷一次", 60, 86400),
    "IDLE_TOPIC_P": (0.10, float, "抛话题概率", 0, 1),
    "MEAL_RECENT": (1200, float, "饭点前多少秒内有消息才报菜单", 0, 86400),
    "CURFEW_WINDOW": (90, float, "宵禁开始/结束后多少分钟内播报（分钟）", 0, 1440),
    # ---- 回复形态 ----
    "MAX_LINES": (4, int, "一次回应最多几条消息", 1, 10),
    "LINE_GAP_MIN": (2.0, float, "同一轮里两条消息的最小间隔（秒）", 0, 60),
    "LINE_GAP_MAX": (4.0, float, "最大间隔（秒）", 0, 120),
    "MAX_LINE": (30, int, "单条消息最多几个字（wx_chat_loop）", 5, 200),
    # ---- 图片 / 表情包 ----
    "MEDIA_MAX_AGE": (300, float, "只识别多少秒内的新图", 0, 86400),
    "DESCRIBE_BUDGET": (1, int, "每轮最多现场识别几张", 0, 5),
    "SELF_STICKER_P": (0.40, float, "活跃窗口里「图里是我」→ 回表情包的概率", 0, 1),
    "SELF_STICKER_SILENT_P": (0.02, float, "静默状态下「图里是我」→ 回表情包的概率（不开窗）", 0, 1),
    "PAT_REPLY_P": (1.0, float, "被拍一拍回一句的概率", 0, 1),
}

GROUPS = [
    ("活跃窗口", ["ACTIVE_SECS", "PAT_ACTIVE_SECS", "PENDING_AGE", "TRIGGER_OPEN_P"]),
    ("静默与丢失", ["SILENT_ROLL_GAP", "SILENT_ROLL_P", "SILENT_ROLL_COOL", "REPLY_LOSS_P"]),
    ("注意力", ["ATTENTION_FROM", "ATTENTION_STEP"]),
    ("主动说话", ["IDLE_TOPIC_AFTER", "IDLE_TOPIC_EVERY", "IDLE_TOPIC_P", "MEAL_RECENT", "CURFEW_WINDOW"]),
    ("回复形态", ["MAX_LINES", "LINE_GAP_MIN", "LINE_GAP_MAX", "MAX_LINE"]),
    ("图片 / 表情包", ["MEDIA_MAX_AGE", "DESCRIBE_BUDGET", "SELF_STICKER_P", "SELF_STICKER_SILENT_P", "PAT_REPLY_P"]),
]

_cache = {"mtime": 0, "data": None}


def load():
    """读 tuning.json；只保留认识的字段，类型按 SPEC 校验。"""
    try:
        st = os.stat(TUNING_FILE)
        if st.st_mtime == _cache["mtime"] and _cache["data"] is not None:
            return _cache["data"]
        raw = json.load(open(TUNING_FILE, encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raw = {}
        out = {}
        for k, (default, typ, *_rest) in SPEC.items():
            if k in raw:
                try:
                    out[k] = typ(raw[k])
                except (TypeError, ValueError):
                    out[k] = default
            else:
                out[k] = default
        _cache["mtime"] = st.st_mtime
        _cache["data"] = out
        return out
    except (FileNotFoundError, OSError, ValueError):
        if _cache["data"] is None:
            _cache["data"] = {k: v[0] for k, v in SPEC.items()}
        return _cache["data"]


def save(patch):
    """把 patch 合并进 tuning.json，返回实际写入的字段。"""
    cur = {}
    try:
        cur = json.load(open(TUNING_FILE, encoding="utf-8-sig"))
        if not isinstance(cur, dict):
            cur = {}
    except (FileNotFoundError, OSError, ValueError):
        cur = {}
    changed = []
    for k, v in (patch or {}).items():
        if k not in SPEC:
            continue
        default, typ, _desc, lo, hi = SPEC[k]
        try:
            val = typ(v)
        except (TypeError, ValueError):
            continue
        if val < lo:
            val = lo
        if val > hi:
            val = hi
        cur[k] = val
        changed.append(k)
    tmp = TUNING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, TUNING_FILE)
    _cache["mtime"] = 0
    return changed


def apply(module):
    """把 tuning 的值贴到 wx_multi / wx_chat_loop 这些模块的同名全局上（每轮调一次，带 mtime 缓存）。"""
    vals = load()
    touched = []
    for k, v in vals.items():
        if hasattr(module, k) and getattr(module, k) != v:
            setattr(module, k, v)
            touched.append(k)
    return touched


def describe():
    """给面板用的元信息。"""
    vals = load()
    return {
        "values": vals,
        "spec": {k: {"default": v[0], "type": v[1].__name__, "desc": v[2], "min": v[3], "max": v[4]}
                 for k, v in SPEC.items()},
        "groups": [{"title": t, "keys": ks} for t, ks in GROUPS],
        "file": TUNING_FILE,
        "now": time.time(),
    }
