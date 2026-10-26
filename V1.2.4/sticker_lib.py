# -*- coding: utf-8 -*-
"""表情包库的读写与挑选。

库目录：<程序目录>\\stickers\\library（可用 settings.json 的 paths.sticker_lib 改）
索引：同目录 index.md，表格列「文件 / 画面内容 / 适用场景 / 关键词」。

对外：
  entries()                  -> [{"file","what","scene","keys"}]
  summary(n=24)              -> 「文件名｜适用场景｜关键词」多行，喂给模型挑
  pick(text)                 -> 按关键词挑一个文件（绝对路径），挑不到 None
  mark_used(name)            -> 记一下刚发过，短时间内不再发同一张
  save_sticker(src, desc)    -> 把收到的图存进库并补一行索引（模型判值不值得留）
"""
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time

import config as __CFG

LIB = __CFG.sticker_lib()            # 可配置：settings.json 的 paths.sticker_lib（见 config.py）
INDEX = os.path.join(LIB, "index.md")
HERE = os.path.dirname(os.path.abspath(__file__))
USED = os.path.join(HERE, "sticker_used.json")      # 文件名 -> 上次发出时间
SAVED = os.path.join(HERE, "sticker_saved.json")    # 内容 hash -> 存进库的时间

REUSE_GAP = 900         # 同一张表情包 15 分钟内不再发
DAILY_CAP = None        # 已停用：2026-10-23 用户 m06978 取消每日上限（save_sticker 不再看它）
SAVE_GAP = 0            # 已停用：2026-10-23 用户 m06978 取消保存间隔（save_sticker 不再看它）
HASHES = os.path.join(HERE, "sticker_hashes.json")   # 库内文件 -> 感知哈希，用于查重

# 跟「我」的形象相关的一律必存（用户 m06053）：命中就跳过模型判断，也不受每日上限/间隔限制。
# 2026-10-23 m06108「收紧」：光有「蓝发」不够，必须是身份词，或者 蓝发 + （女仆／鲸鱼鱼尾类）组合。
SELF_STRONG = (
    "大肥鱼", "小肥鱼", "鲸鱼娘", "蓝鲸", "虎鲸", "鱼娘", "鲸鱼尾", "鲸尾",
    "本鱼", "呆呆鱼", "鱼鱼", "鲸鱼女仆",
)
SELF_BLUE = ("蓝发", "蓝毛", "蓝白", "蓝色头发")
SELF_MARK = ("女仆", "围裙", "maid", "鲸", "鱼尾", "鱼鳍", "尾巴", "鱼娘", "大肥鱼", "鱼耳")
# 别的动物娘（猫/狐/兔…）不算我，避免「蓝发女仆猫娘」被当成自己
SELF_NOT = ("猫娘", "猫耳", "犬娘", "狗娘", "兔娘", "狐娘", "狐狸娘", "鸟娘", "龙娘", "蛇娘", "羊娘")
EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _log(msg):
    print(msg, flush=True)


# ---------- 索引读写 ----------

def _split_cols(line):
    """'| a | b |' -> ['a','b']"""
    s = line.strip()
    if not s.startswith("|"):
        return []
    return [c.strip() for c in s.strip("|").split("|")]


def entries():
    """读 index.md 表格 → [{"file","what","scene","keys"}]"""
    if not os.path.exists(INDEX):
        return []
    rows = []
    try:
        with open(INDEX, encoding="utf-8") as f:
            for line in f:
                cols = _split_cols(line)
                if len(cols) < 4:
                    continue
                if cols[0] in ("文件",) or set(cols[0]) <= set("-: "):
                    continue
                fname = cols[0].strip("` ")
                if not fname.lower().endswith(EXTS):
                    continue
                if not os.path.exists(os.path.join(LIB, fname)):
                    continue
                rows.append({"file": fname, "what": cols[1], "scene": cols[2], "keys": cols[3]})
    except Exception as e:
        _log("读 index.md 失败: %s" % e)
    return rows


def summary(n=24):
    rows = entries()[:n]
    if not rows:
        return "（库里现在没有表情包）"
    return "\n".join("- %s｜%s｜%s" % (r["file"], r["scene"], r["keys"]) for r in rows)


def _append_row(fname, what, scene, keys):
    """把一行加进 index.md 的表格末尾（没有表格就新建）。"""
    os.makedirs(LIB, exist_ok=True)
    row = "| %s | %s | %s | %s |\n" % (fname, what, scene, keys)
    if not os.path.exists(INDEX):
        with open(INDEX, "w", encoding="utf-8") as f:
            f.write("# 表情包索引\n\n| 文件 | 画面内容 | 适用场景 | 关键词 |\n|---|---|---|---|\n" + row)
        return
    with open(INDEX, encoding="utf-8") as f:
        lines = f.readlines()
    last = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("|"):
            last = i
    if last < 0:
        lines.append("\n| 文件 | 画面内容 | 适用场景 | 关键词 |\n|---|---|---|---|\n")
        lines.append(row)
    else:
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines.insert(last + 1, row)
    with open(INDEX, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ---------- 挑一张 ----------

def _load(path, default):
    try:
        return json.load(open(path, encoding="utf-8-sig"))
    except Exception:
        return default


def _save(path, obj):
    """写注册表。Windows 上 os.replace 偶尔会被别的进程占着（WinError 5/32），失败重试几次。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception as e:
        _log("写 %s 失败: %s" % (os.path.basename(path), e))
        return
    for k in range(4):
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            if k == 3:
                _log("写 %s 失败: %s" % (os.path.basename(path), e))
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            else:
                time.sleep(0.15 * (k + 1))


def mark_used(name):
    used = _load(USED, {})
    used[name] = time.time()
    if len(used) > 200:
        cut = time.time() - 86400 * 3
        used = {k: v for k, v in used.items() if v > cut}
    _save(USED, used)


def _score(entry, text):
    """关键词命中数；场景里的词也算。"""
    text = (text or "").lower()
    score = 0
    for kw in re.split(r"[\s,，、/]+", (entry["keys"] or "") + " " + (entry["scene"] or "")):
        kw = kw.strip().lower()
        if len(kw) < 2:
            continue
        if kw in text:
            score += 2 if kw in (entry["keys"] or "").lower() else 1
    return score


def pick(text):
    """按当前聊天内容挑一张没刚发过的；挑不到返回 None。"""
    rows = entries()
    if not rows:
        return None
    used = _load(USED, {})
    now = time.time()
    hint = (text or "").lower()
    # 模型直接写了文件名就照办
    for r in rows:
        stem = os.path.splitext(r["file"])[0].lower()
        if r["file"].lower() in hint or (len(stem) >= 3 and stem in hint):
            p = os.path.join(LIB, r["file"])
            if os.path.exists(p):
                return p
    fresh = [r for r in rows if now - float(used.get(r["file"], 0)) > REUSE_GAP]
    if not fresh:
        # 库里就一两张、又刚发过：退一步用最久没发的那张，间隔至少 60 秒
        lru = min(rows, key=lambda r: float(used.get(r["file"], 0)))
        if now - float(used.get(lru["file"], 0)) < 60:
            return None
        fresh = [lru]
    if not fresh:
        return None
    scored = sorted(((_score(r, text), r) for r in fresh), key=lambda x: -x[0])
    best = scored[0][0]
    top = [r for sc, r in scored if sc == best]
    chosen = random.choice(top)
    p = os.path.join(LIB, chosen["file"])
    return p if os.path.exists(p) else None


# ---------- 收进来的图要不要存 ----------

def _sha(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dhash(path, size=8):
    """64 位感知哈希，用来认「同一张表情包换了压缩/格式」。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("L").resize((size + 1, size), Image.LANCZOS)
            px = list(im.getdata())
        bits = 0
        for row in range(size):
            base = row * (size + 1)
            for col in range(size):
                bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
        return "%016x" % bits
    except Exception:
        return ""


def _hamming(a, b):
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 99


def _lib_hashes():
    """库内文件 -> 感知哈希（增量补齐并落盘；顺手清掉已不在库里的旧记录）。"""
    reg = _load(HASHES, {})
    changed = False
    rows = entries()
    alive = {e["file"] for e in rows}
    for f in list(reg.keys()):
        if f not in alive:
            reg.pop(f, None)
            changed = True
    for e in rows:
        f = e["file"]
        if f not in reg:
            reg[f] = _dhash(os.path.join(LIB, f)) or ""
            changed = True
    if changed:
        _save(HASHES, reg)
    return reg


def _bigrams(text):
    t = re.sub(r"\s+", "", str(text or ""))
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else ({t} if t else set())


def _similar(a, b):
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / float(len(x | y))


def _ext_by_magic(path):
    """按文件头定后缀（微信缓存里的表情常没有后缀，或者被存成 .bin）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return ""
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ""


def _valid_image(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:
        return False


def _is_self(desc):
    d = str(desc or "")
    if any(k in d for k in SELF_NOT) and not any(k in d for k in ("鲸", "大肥鱼", "本鱼")):
        return False
    if any(k in d for k in SELF_STRONG):
        return True
    return any(k in d for k in SELF_BLUE) and any(k in d for k in SELF_MARK)


def find_dup(path, what=None):
    """入库前查重：文件内容相同 / 画面几乎一样 / 注释说法几乎一样（≥0.88），都算重复。"""
    h = _sha(path)
    saved = _load(SAVED, {})
    if h in saved:
        return "同文件"
    dh = _dhash(path)
    if dh:
        for fname, hh in _lib_hashes().items():
            if hh and _hamming(dh, hh) <= 6:
                return "画面几乎一样（%s）" % fname
    if what:
        for e in entries():
            if _similar(what, e.get("what")) >= 0.88:   # m06978 从 0.72 放宽：说法相近的不同图不该被当重复
                return "注释几乎一样（%s）" % e["file"]
    return None


KEEP_PROMPT = (
    "这是一个微信聊天里收到的表情包/图片，描述如下：\n%s\n\n"
    "判断它值不值得存进「发出去用来配合聊天」的表情包库，并给注释。"
    "只有本身是个完整表情包（有画面、情绪/梗清楚、别人看了能会心一笑）才值得；"
    "纯文字截图、聊天记录截图、别人家的私事/丑照、低清糊图、风景美食照都不值得。\n"
    "只输出 JSON：{\"keep\": true/false, \"what\": \"画面内容(15字内)\", "
    "\"scene\": \"什么场合用(12字内)\", \"keys\": \"关键词 空格分隔 3-5个\"}"
)


def save_sticker(src, desc, chat_json=None):
    """把一张收到的表情包存进库 + 补索引。返回文件名或 None。"""
    try:
        if not src or not os.path.exists(src):
            return None
        if os.path.getsize(src) < 1024:
            return None
        h = _sha(src)
        saved = _load(SAVED, {})
        if h in saved:
            return None
        now = time.time()
        selfy = _is_self(desc)

        # ① 入库前查重：同文件 / 画面几乎一样 / （后面）注释几乎一样
        dup = find_dup(src)
        if dup:
            _log("重复，不存：%s（%s）" % (os.path.basename(src), dup))
            saved[h] = now
            _save(SAVED, saved)
            return None

        if chat_json is None:
            import wx_chat_loop as L
            chat_json = L.chat_json

        if selfy:
            # ② 跟我的形象相关：必存，不问模型、不受每日上限和间隔限制
            what = str(desc or "我自己").strip()[:40].replace("|", "／") or "我自己"
            scene = "群里有人发我的图时"
            keys = " ".join([k for k in (SELF_STRONG + SELF_BLUE + SELF_MARK) if k in str(desc)]) or "大肥鱼 本鱼"
            keys = (keys + " 表情包")[:60]
        else:
            # 2026-10-23 用户 m07039：只存「跟我形象相关」的表情，别的表情一律不收
            _log("不是我（设定形象）的表情，不存：%s" % os.path.basename(src))
            saved[h] = now
            _save(SAVED, saved)
            return None

        ext = _ext_by_magic(src)
        if not ext:
            _log("不是可识别的图片，不存：%s" % os.path.basename(src))
            saved[h] = now
            _save(SAVED, saved)
            return None
        if not _valid_image(src):
            _log("图片打不开（可能没下全），不存：%s" % os.path.basename(src))
            saved[h] = now
            _save(SAVED, saved)
            return None
        name = "got_%s_%s%s" % (time.strftime("%m%d_%H%M"), h[:6], ext)
        dst = os.path.join(LIB, name)
        shutil.copyfile(src, dst)
        _append_row(name, what, scene, keys)
        saved[h] = now
        _save(SAVED, saved)
        dh = _dhash(dst)
        if dh:
            reg = _load(HASHES, {})
            reg[name] = dh
            _save(HASHES, reg)
        _log("表情库存了一张: %s（%s）%s" % (name, what, "  ← 我自己的形象，必存" if selfy else ""))
        return name
    except Exception as e:
        _log("存表情包失败: %s" % e)
        return None


if __name__ == "__main__":
    for r in entries():
        print(r)
    print("--- summary ---")
    print(summary())
    if len(sys.argv) > 1:
        print("pick:", pick(sys.argv[1]))
