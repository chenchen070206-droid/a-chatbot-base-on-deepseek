# -*- coding: utf-8 -*-
"""wx_vision.py —— 把微信消息里的「图片 / 表情动图」变成一句文字描述。

链路（全程不截屏）：
  图片消息  local_type=3        -> msg\\attach\\...\\Img\\<名字>.dat 解密（wx_image.py）
  表情/动图 local_type=...49    -> XML 里 <emojiinfo> base64 里的 CDN 直链下载
  -> 动图抽帧拼一张联系表（gif_sheet 的算法）
  -> DeepSeek(deepseek-flash) 识别，OVH 兜底（vision_ovh.py）
  -> 结果缓存进 vision_cache.json（同一条消息只花一次额度）

对外主要两个函数：
  describe_message(msg, account) -> 描述文本 / None
  is_self(desc)                  -> 图里是不是「我」（蓝发女仆 / 鲸鱼娘 / 大肥鱼）
"""
import base64
import glob
import hashlib
import html
import json
import os
import re
import sys
import time

import requests
from PIL import Image, ImageSequence
import config as __CFG  # noqa: E402  路径配置

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_image as W  # noqa: E402
import vision_ovh as V  # noqa: E402

BASE = __CFG.wechat_files()
CACHE_FILE = os.path.join(HERE, "vision_cache.json")
OUT_DIR = os.path.join(HERE, "decoded")
UA = {"User-Agent": "MicroMessenger Client"}

# 判定「图里是我」：出现鲸鱼娘/大肥鱼这类标志，或者「蓝发 + 女仆装/鱼尾」同时成立。
# 光有蓝发少女不算（2026-10-22 19:55 把蓝发双马尾的丰川祥子表情认成自己，群里当场问号）
SELF_STRONG = ("大肥鱼", "鲸鱼娘", "蓝鲸", "小肥鱼", "鱼娘", "鲸鱼女孩", "鲸鱼妹子",
               "虎鲸", "鲸鱼尾巴", "鲸尾", "蓝白女仆", "蓝白配色")
SELF_BLUE = ("蓝发", "蓝毛", "蓝色头发", "蓝色的头", "蓝白", "blue hair", "blue-haired", "blue haired")
SELF_MAID = ("女仆", "围裙", "女仆装", "maid")
SELF_FISH = ("鲸", "鱼尾", "鱼鳍", "鲸尾", "鱼鳞", "鱼")
SELF_GIRL = ("女孩", "少女", "女生", "girl")

PROMPT_IMAGE = (
    "这是微信聊天里收到的图片。请用中文描述：画面里是什么/谁；"
    "角色外观（发色、服装、配色、有没有动物元素）；表情和动作；"
    "图上的文字请逐字转写。控制在 120 字以内，不要客套话。"
)
PROMPT_STICKER = (
    "这是微信聊天里的表情动图，我把它的几帧拼在了一张图里（从左到右、从上到下是时间顺序）。"
    "请用中文描述这个动图在做什么：角色、动作变化、情绪，有文字就逐字转写。80 字以内。"
)

_THROTTLE = os.path.join(HERE, ".vision_last_call")
MIN_INTERVAL = 2.0    # 主用 DeepSeek，省着点错开就行（OVH 兜底时才受限流拖慢）


# ---------------------------------------------------------------- 缓存
def _load_cache():
    try:
        return json.load(open(CACHE_FILE, encoding="utf-8-sig"))
    except Exception:
        return {}


def _save_cache(cache):
    try:
        json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        print("缓存写入失败:", e, file=sys.stderr)


# 图本身坏了（截断/过期）或三次识别都失败 → 记一笔，以后直接跳过（用户 m06759）
FAIL_FILE = os.path.join(HERE, "vision_failed.json")
FAIL_TTL = 3600


def _failed_recent(key):
    """这条是不是已经记为「坏图 / 无法识别」了 —— 记过就直接跳过，不再重试（用户 m06759）。"""
    try:
        data = json.load(open(FAIL_FILE, encoding="utf-8-sig"))
    except Exception:
        return False
    return key in data


def _mark_failed(key):
    try:
        data = json.load(open(FAIL_FILE, encoding="utf-8-sig"))
    except Exception:
        data = {}
    data[key] = time.time()
    try:
        json.dump(data, open(FAIL_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


# ---------------------------------------------------------------- 解消息体
def unpack_content(content):
    """数据库里的 message_content -> XML 文本（hex+zstd / 明文都吃）。"""
    s = str(content or "")
    if not s:
        return ""
    if s.lstrip()[:1] == "<":
        return s
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return s
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard
            raw = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
        except Exception:
            return ""
    txt = raw.decode("utf-8", "ignore")
    m = re.match(r"^(wxid_[0-9a-zA-Z]+|[^:]{1,40})\s*:\s*", txt)
    if m and txt[m.end():].lstrip()[:1] == "<":
        return txt[m.end():]
    return txt


def msg_md5(xml):
    for tag in ("md5", "emoticonmd5"):
        m = re.search(tag + r'="([0-9a-fA-F]{16,64})"', xml)
        if m:
            return m.group(1).lower()
    return None


# ---------------------------------------------------------------- 拿图：图片
def chat_attach_dir(room):
    """会话的附件目录名 = md5(会话 id)（实测 wxid_OWNER -> 1c72f54800d6163114ae1378c062dad0）。"""
    return hashlib.md5(str(room).encode()).hexdigest()


def declared_sizes(xml):
    """XML 里写着的字节数：length（原图）、hevc_mid_size（中转图）、cdnthumblength（缩略图）。
    实测 .dat 文件大小 ≈ 这些值 + 31（15 字节容器头 + AES 对齐填充）。"""
    out = []
    for k in ("length", "hevc_mid_size", "cdnthumblength"):
        m = re.search(k + r'="(\d+)"', xml)
        if m:
            out.append(int(m.group(1)))
    return out


def find_dat(xml, when, account=None, room=None, tol=900, claimed=()):
    """找对应的 .dat：优先按 XML 声明的字节数精确匹配（可靠），退路才用 mtime 贴近。

    数据库里的 md5 跟磁盘文件名对不上，所以不能按名字找；
    但 XML 的 length / cdnthumblength 和文件大小能对上，这就是精确钥匙。
    """
    sizes = declared_sizes(xml)
    dirs = []
    if account:
        cfg = json.load(open(os.path.join(HERE, "keys.json"), encoding="utf-8-sig"))
        d = (cfg.get(account) or {}).get("dir")
        if d:
            dirs.append(os.path.join(BASE, d))
    if not dirs:
        dirs = glob.glob(os.path.join(BASE, "wxid_*"))
    cands = []
    for root in dirs:
        pat = os.path.join(root, "msg", "attach", chat_attach_dir(room), "**", "Img", "*.dat") if room \
            else os.path.join(root, "msg", "attach", "**", "Img", "*.dat")
        cands += glob.glob(pat, recursive=True)

    exact, near = [], []
    for p in cands:
        if p in claimed:
            continue
        try:
            size = os.path.getsize(p)
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if sizes:
            diff = min(abs(size - (v + 31)) for v in sizes)
            if diff <= 64:
                exact.append((diff, abs(mt - when), size, p))
            continue
        if abs(mt - when) <= tol:
            near.append((abs(mt - when), size, p))
    if exact:
        exact.sort(key=lambda h: (h[0], h[1], -h[2]))
        return exact[0][3]
    if near:
        near.sort(key=lambda h: (h[0], -h[1]))
        return near[0][2]
    return None


def image_to_file(xml, when, account=None, room=None, claimed=()):
    """拿图：优先完整图，坏了就退到缩略图（同一张图的 _t.dat 往往已经下全）。"""
    tried = set(claimed or ())
    last_err = "本地没有对应的 .dat（没下载/过期清理了）"
    for _ in range(3):
        p = find_dat(xml, when, account, room, claimed=tried)
        if not p:
            return None, last_err, None
        tried.add(p)
        try:
            aes, xor = W.keys_for(p, account)
            buf = W.decrypt(p, aes.encode()[:16], xor)
        except Exception as e:
            last_err = "解密失败:%s" % str(e)[:40]
            continue
        os.makedirs(OUT_DIR, exist_ok=True)
        base = os.path.join(OUT_DIR, os.path.basename(p).replace(".dat", ""))
        out = W.to_image(buf, base)
        if not out:
            last_err = "解密成功但不是认识的图片格式"
            continue
        try:                                     # 文件可能是截断的（边下边存），先验一下
            with Image.open(out) as im:
                im.load()
        except Exception as e:
            last_err = "图片损坏:%s" % str(e)[:40]
            continue
        return out, "", p
    return None, last_err, None


# ---------------------------------------------------------------- 拿图：表情动图
def sticker_urls(xml):
    """表情包的 CDN 直链：两种结构都认。

    appmsg type 8  -> <emojiinfo> 里塞 base64，展开后是一段带 URL 的文本
    local_type 47  -> <msg><emoji ... cdnurl="http://wxapp.tc.qq.com/…" />，带 &amp; 转义
    """
    urls = []
    m = re.search(r"<emojiinfo>(.*?)</emojiinfo>", xml, re.S)
    blob = m.group(1).strip() if m else ""
    if blob:
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4))
            txt = raw.decode("utf-8", "ignore")
            urls += re.findall(r"https?://[^\s\"'<>\\]+", txt)
        except Exception:
            pass
    urls += [html.unescape(u) for u in re.findall(r'cdnurl\s*=\s*"([^"]+)"', xml)]
    out = []
    for u in urls:
        if "stodownload" in u and u not in out:
            out.append(u)
    return out


def sticker_to_file(xml, key):
    urls = sticker_urls(xml)
    if not urls:
        return None, "XML 里没有可用直链", None
    for i, u in enumerate(urls[:3]):
        try:
            r = requests.get(u, headers=UA, timeout=60)
        except Exception as e:
            print("下载失败(%d): %s" % (i, str(e)[:40]), file=sys.stderr)
            continue
        if r.status_code != 200 or len(r.content) < 512:
            continue
        buf = r.content
        if (buf[:4] in (b"GIF8", b"wxgf", b"\x89PNG") or buf[:2] == b"\xff\xd8"
                or buf[:4] == b"RIFF"):
            os.makedirs(OUT_DIR, exist_ok=True)
            path = os.path.join(OUT_DIR, "emoji_%s.bin" % key[:16])
            open(path, "wb").write(buf)
            try:
                with Image.open(path) as im:
                    im.load()
                    frames = sum(1 for _ in ImageSequence.Iterator(im))
            except Exception as e:
                print("表情文件打不开，换下一个直链: %s" % str(e)[:40], file=sys.stderr)
                continue
            if frames <= 1:
                if buf[:4] == b"GIF8":
                    ext = ".gif"
                elif buf[:4] == b"\x89PNG":
                    ext = ".png"
                else:
                    ext = ".jpg"
                final = path.replace(".bin", ext)
                os.replace(path, final)
                return final, "", None
            sheet = path.replace(".bin", "_sheet.png")
            try:
                _sheet(path, sheet, min(6, frames))
            except Exception as e:
                return path, "拼帧失败:%s" % str(e)[:30], None
            return sheet, "", path
    return None, "直链都下不下来（可能过期）", None


def _sheet(src, out, n=6):
    im = Image.open(src)
    frames = [f.convert("RGB").copy() for f in ImageSequence.Iterator(im)]
    picks = [frames[min(len(frames) - 1, round(i * (len(frames) - 1) / max(1, n - 1)))] for i in range(n)]
    w, h = picks[0].size
    if w > 400:
        s = 400.0 / w
        w, h = int(w * s), int(h * s)
    picks = [p.resize((w, h)) for p in picks]
    cols = 3
    rows = (n + cols - 1) // cols
    canvas = Image.new("RGB", (cols * w, rows * h), "white")
    for i, p in enumerate(picks):
        canvas.paste(p, ((i % cols) * w, (i // cols) * h))
    canvas.save(out)


# ---------------------------------------------------------------- 识别
def _throttle():
    try:
        last = float(open(_THROTTLE, encoding="utf-8").read().strip())
    except Exception:
        last = 0.0
    wait = MIN_INTERVAL - (time.time() - last)
    if wait > 0:
        print("视觉限流中，等 %.0fs" % wait, file=sys.stderr)
        time.sleep(wait)
    try:
        open(_THROTTLE, "w", encoding="utf-8").write(str(time.time()))
    except Exception:
        pass


def _vision(path, prompt):
    _throttle()
    try:
        out = V.describe(path, prompt)
    except Exception as e:
        print("识图调用异常: %s" % str(e)[:60], file=sys.stderr)
        return None
    if not out or out.startswith("HTTP ") or "限流重试仍失败" in out:
        return None
    return out.strip()


KIND_IMAGE, KIND_STICKER = "image", "sticker"

PROMPT_STICKER_STATIC = (
    "这是微信聊天里收到的一张表情包（静态图）。用中文说清：画面里是什么、谁在做什么、"
    "情绪是什么；有文字就逐字转写。60 字以内，不要客套话。"
)


def kind_of(lt, xml):
    """这条消息能不能识别，能的话是图片还是表情。"""
    if lt == "3":
        return KIND_IMAGE
    if lt == "47" or re.search(r"<emoji[\s>]", xml) or "<emojiinfo>" in xml:
        return KIND_STICKER
    if "<appmsg" in xml and re.search(r"<type>\s*8\s*</type>", xml):
        return KIND_STICKER
    if "<img" in xml and lt != "1":
        return KIND_IMAGE
    return None


def cache_key(msg, xml=None, account=None):
    lt = str(msg.get("local_type"))
    if xml is None:
        xml = unpack_content(str(msg.get("message_content") or ""))
    kind = kind_of(lt, xml)
    if not kind:
        return None
    when = float(msg.get("create_time") or 0)
    return "%s:%s:%s" % (kind, msg_md5(xml) or int(when), account or "default")


def cached_desc(msg, account=None):
    """只查缓存不花钱：命中返回描述，没命中返回 None。"""
    xml = unpack_content(str(msg.get("message_content") or ""))
    key = cache_key(msg, xml, account)
    if not key:
        return None
    v = _load_cache().get(key)
    if isinstance(v, dict):
        return v.get("desc")
    if isinstance(v, str):
        return v
    return None


def cached_path(msg, account=None):
    """只查缓存：这条消息解密/下载下来的本地文件（没有就 None）。"""
    xml = unpack_content(str(msg.get("message_content") or ""))
    key = cache_key(msg, xml, account)
    if not key:
        return None
    v = _load_cache().get(key)
    if isinstance(v, dict):
        p = v.get("path")
        if p and os.path.exists(p):
            return p
    return None


def describe_message(msg, account=None, room=None):
    """消息 dict -> 一句描述（缓存）。不是图片/表情、或拿不到图就返回 None。"""
    xml = unpack_content(str(msg.get("message_content") or ""))
    kind = kind_of(str(msg.get("local_type")), xml)
    if not kind:
        return None

    when = float(msg.get("create_time") or time.time())
    key = cache_key(msg, xml, account)
    cache = _load_cache()
    old = cache.get(key)
    if isinstance(old, dict):
        return old.get("desc")
    if isinstance(old, str):
        return old
    if _failed_recent(key):
        return None

    # 已经被称为别的消息占用的文件不再用（同一会话连发两张图时容易撞）
    claimed = set()
    for v in cache.values():
        if isinstance(v, dict) and v.get("path"):
            claimed.add(v["path"])

    if kind == KIND_IMAGE:
        path, err, raw_path = image_to_file(xml, when, account, room, claimed=claimed)
        prompt = PROMPT_IMAGE
    else:
        path, err, raw_path = sticker_to_file(xml, msg_md5(xml) or str(int(when)))
        is_sheet = bool(path and path.endswith("_sheet.png"))
        prompt = PROMPT_STICKER if is_sheet else PROMPT_STICKER_STATIC
    if not path:
        print("识图跳过: %s" % err, file=sys.stderr)
        _mark_failed(key)              # 图本身不行 → 记为坏图，以后直接跳过
        return None

    desc = None
    for attempt in range(1, 4):        # 失败重试，最多三次（用户 m06759）
        desc = _vision(path, prompt)
        if desc:
            break
        print("识图第 %d 次失败，重试…" % attempt, file=sys.stderr)
        time.sleep(1.0 * attempt)
    if not desc:
        _mark_failed(key)              # 三次都没成 → 记为「无法识别」，跳过
        return None
    desc = re.sub(r"\s+", " ", desc).strip()[:300]
    cache[key] = {"desc": desc, "path": raw_path or path, "kind": kind, "at": int(when)}
    _save_cache(cache)
    return desc


def is_self(desc):
    """图里是不是「我」：蓝发女仆、鲸鱼娘、大肥鱼那一挂。"""
    if not desc:
        return False
    d = desc.lower() if desc.isascii() else desc
    if any(w in d for w in SELF_STRONG):
        return True
    blue = any(w in d for w in SELF_BLUE)
    if not blue:
        return False
    return any(w in d for w in SELF_MAID) or any(w in d for w in SELF_FISH)


if __name__ == "__main__":
    # 手动试：python wx_vision.py <account> [最近第几条消息，默认0=最新]
    import wx_chat_loop as L
    acct = sys.argv[1] if len(sys.argv) > 1 else "wxid_OWNER"
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    db = L.open_db("wxid_SELF")
    msgs, err = db.get_messages(acct, limit=idx + 5, offset=0)
    msg = msgs[idx]
    print("local_type:", msg.get("local_type"), "time:", msg.get("create_time"))
    d = describe_message(msg, "wxid_SELF", acct)
    print("描述:", d)
    print("是我吗:", is_self(d))
