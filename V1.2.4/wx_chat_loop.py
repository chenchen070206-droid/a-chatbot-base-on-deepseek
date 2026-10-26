# -*- coding: utf-8 -*-
"""群聊自动接话循环：
读最新消息 -> 判断是否轮到自己 -> 模型出稿 -> 发出去 -> 回读确认 -> 等一会儿再循环

用法:
  python wx_chat_loop.py --account wxid_SELF --room <群idA>@chatroom --rounds 4
"""
import argparse
import datetime
import json
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "wechat-mcp", "scripts"))
import wx4_db as D  # noqa: E402
import wx_vision as VIS  # noqa: E402
import sticker_lib as STICKER  # noqa: E402  表情包库（settings.json 的 paths.sticker_lib）
import config as __CFG  # noqa: E402  路径配置

# 识图预算：每轮最多现场识别一张（一张 2~15s，多了主循环就卡住）；
# 只认最近半小时的图，历史图不花额度。
DESCRIBE_BUDGET = 1
MEDIA_MAX_AGE = 300     # 只识 5 分钟内的新图（用户 m06759）
VISION_COOLDOWN = 300      # 连续失败后歇 5 分钟，别每轮都卡在超时上
_budget = [0]
_vision_down_until = [0.0]


def media_text(msg, room=None, me=None):
    """图片/表情 -> 一段可以当普通消息用的文字；拿不到就返回 None（调用方退回占位符）。"""
    try:
        desc = VIS.cached_desc(msg, me)
    except Exception:
        desc = None
    if desc:
        return desc
    if _budget[0] <= 0:
        return None
    try:
        when = float(msg.get("create_time") or 0)
    except Exception:
        when = 0
    if not when or time.time() - when > MEDIA_MAX_AGE:
        return None
    _budget[0] -= 1
    try:
        desc = VIS.describe_message(msg, account=me, room=room)
    except Exception as e:
        print("识图失败: %s" % str(e)[:60], file=sys.stderr)
        desc = None
    # 失败不在这里做全局冷却：wx_vision 里已经重试 3 次，并把这条记进
    # vision_failed.json（记为「无法识别」），下一轮直接跳过（用户 m06759）。
    return desc

BASE = __CFG.wechat_files()          # 可配置：settings.json 的 paths.wechat_files（见 config.py）
FACTS = json.load(open(os.path.join(HERE, "keys.json"), encoding="utf-8-sig"))
SEND = os.path.join(HERE, "send_guard.py")


def api_key():
    """三级兜底：环境变量 → settings.json(deepseek.apiKey) → 本机 ~/.dsh 凭证（默认关）。"""
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    st = load_settings()
    key = ((st.get("deepseek") or {}).get("apiKey") or "").strip()
    if key:
        return key
    if ((st.get("panel") or {}).get("allowDshKey")) is True:
        try:
            for line in open(os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml"),
                             encoding="utf-8"):
                if line.strip().startswith("DEEPSEEK_API_KEY:"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    raise SystemExit(
        "没有 DeepSeek API Key。任选一种：\n"
        "  1) 打开控制面板「概览」页，把 Key 填进去（推荐）\n"
        "  2) 设置环境变量 DEEPSEEK_API_KEY\n"
        "  3) settings.json 里设 \"panel\": {\"allowDshKey\": true}（读本机 ~/.dsh 凭证）")


def open_db(account):
    """打开某个账号的微信数据库。

    新机器/新账号第一次跑，keys.json 里没有这个账号时，给出能照着做的提示，
    而不是丢一个 KeyError。
    """
    if account not in FACTS:
        raise SystemExit(
            "keys.json 里没有账号 %r 的数据库 key。\n"
            "  1) 先抓 key：python wechat-mcp\\scripts\\wx4_key.py（微信要开着并已登录）\n"
            "  2) 再把拿到的 key 填进 keys.json，格式：\n"
            '     {"%s": {"dir": "wxid_xxx_abcd", "key": "<64位hex>", "role": "主号"}}' % (account, account))
    info = FACTS[account]
    if not os.path.isdir(BASE):
        raise SystemExit(
            "微信数据目录不存在：%s\n"
            "  确认微信装好并登录过，或在 settings.json 的 paths.wechat_files、"
            "环境变量 WX_WECHAT_FILES 里指定正确路径。" % BASE)
    db = D.Wx4DB()
    ok, err = db.open_account(os.path.join(BASE, info["dir"]), info["key"])
    if not ok:
        raise SystemExit("开库失败: %s" % err)
    return db


def contact_name(db, wxid, cache={}):
    if wxid in cache:
        return cache[wxid]
    nm = ""
    try:
        c, err = db.get_contact(wxid)
        if not err and isinstance(c, dict):
            nm = c.get("remark") or c.get("nickName") or c.get("nickname") or ""
    except Exception:
        pass
    cache[wxid] = nm or wxid[:8]
    return cache[wxid]


def decode_content(content):
    """微信有时把普通文本也压成 hex(zstd)，统一还原成明文，别让 28b5 那串喂给模型。"""
    c = str(content or "")
    if c[:8].lower() == "28b52ffd":
        try:
            import zstandard
            c = zstandard.ZstdDecompressor().decompressobj().decompress(
                bytes.fromhex(c)).decode("utf-8", "ignore")
        except Exception:
            pass
    return c


PAT_TEMPLATE = ('<msg><appmsg appid="" sdkver="0"><title>%s</title><des></des><action></action>'
                '<type>62</type><showtype>0</showtype><soundtype>0</soundtype>'
                '<mediatagname></mediatagname><messageext></messageext><messageaction></messageaction>'
                '<content></content><contentattr>0</contentattr><url></url><lowurl></lowurl>'
                '<dataurl></dataurl><lowdataurl></lowdataurl><songalbumurl></songalbumurl>'
                '<songlyric></songlyric><template_id></template_id>'
                '<appattach><totallen>0</totallen><attachid></attachid><emoticonmd5></emoticonmd5>'
                '<fileext></fileext><aeskey></aeskey></appattach><extinfo></extinfo>'
                '<sourceusername></sourceusername><sourcedisplayname></sourcedisplayname>'
                '<thumburl></thumburl><md5></md5><statextstr></statextstr>'
                '<patinfo><fromusername>%s</fromusername><chatusername>%s</chatusername>'
                '<pattedusername>%s</pattedusername><patsuffixversion>0</patsuffixversion>'
                '<template>%s</template></patinfo></appmsg><fromusername></fromusername>'
                '<appinfo><version>0</version><appname></appname><isforceupdate>0</isforceupdate>'
                '</appinfo></msg>')


def pat_xml(chat, from_wxid, to_wxid, from_name, to_name):
    """构造一条「拍一拍」消息（appmsg type 62）。chat=会话 wxid/群 id。"""
    title = '"%s" 拍了拍 "%s"' % (from_name, to_name)
    tmpl = '"${%s}" 拍了拍 "${%s}"' % (from_wxid, to_wxid)
    return PAT_TEMPLATE % (title, from_wxid, chat or "", to_wxid, tmpl)


def pat_info(content, me):
    """这条消息如果是「别人拍我」，返回 {from, chat, title}；否则 None。"""
    if not content or "<type>62</type>" not in content or "<patinfo>" not in content:
        return None

    def grab(tag):
        m = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), content)
        return m.group(1) if m else ""

    if grab("pattedusername") != me:
        return None
    who = grab("fromusername")
    if not who or who == me:
        return None
    return {"from": who, "chat": grab("chatusername"), "title": grab("title")}


def pat_from(content):
    """拍一拍消息的发起人 wxid（拍我的那条才返回；拍别人/自己返回空串）。

    这类消息在库里 sender_username 是空的，名字只能从 patinfo 里捞（m05769 后修的）。
    """
    if not content or "<type>62</type>" not in content or "<patinfo>" not in content:
        return ""
    m = re.search(r"<fromusername>([^<]*)</fromusername>", content)
    return (m.group(1) if m else "").strip()


def appmsg_tag(xml):
    """非文本消息给个能看的标签。

    以前这里一律写 <表情>，结果引用回复、链接分享、文件卡片都被当成表情包去识图：
    describe 返回 None → 主循环判「识别失败」→ 直接 marked answered，那几条消息等于永远不回。
    """
    x = xml or ""
    if "<emoji" in x or "<emojiinfo>" in x:
        return "表情"
    m = re.search(r"<type>(\d+)</type>", x)
    typ = m.group(1) if m else ""

    def _pick(tag):
        r = re.search(r"<%s>(.*?)</%s>" % (tag, tag), x, re.S)
        if not r:
            return ""
        s = re.sub(r"^<!\[CDATA\[|\]\]>$", "", r.group(1).strip())
        return re.sub(r"<[^>]+>", "", s).strip()

    if typ == "62":                      # 拍一拍（type 62 appmsg）
        return "拍一拍"
    title = _pick("title")
    des = _pick("des")
    if typ == "57":                      # 引用回复
        rm = re.search(r"<refermsg>.*?<content>(.*?)</content>", x, re.S)
        ref = re.sub(r"<[^>]+>", "", rm.group(1)).strip() if rm else ""
        return ("引用:%s→%s" % (ref[:30], title[:60])) if ref else ("引用:%s" % title[:60])
    if typ in ("51", "5"):
        return "分享:%s" % (title or des)[:60]
    if typ == "6":
        return "文件:%s" % title[:60]
    if "<img" in x:
        return "图片卡片:%s" % (title or "一张图")[:50]
    if title:
        return "卡片:%s" % title[:60]
    return "其他消息"


def transcript(db, room, me, n=50):
    """读最近 n 条（默认 50），给模型足够上下文保持连贯。

    图片/表情包会先变成一句描述文本（识图预算见 media_text），拿不到才退回占位符——
    这样模型看到的是「图里有什么」，而不是 <图片> 三个字。
    """
    msgs, err = db.get_messages(room, limit=n, offset=0)
    if err:
        return [], []
    _budget[0] = DESCRIBE_BUDGET
    rows = []
    for m in reversed(msgs):
        lt = str(m.get("local_type"))
        t = datetime.datetime.fromtimestamp(int(m.get("create_time") or 0))
        sender = m.get("sender_username") or ""
        content = decode_content(m.get("message_content") or "")
        who = "我" if sender == me else contact_name(db, sender)
        if lt == "1":
            body = content
            if "\n" in body:
                head, rest = body.split("\n", 1)
                if head.startswith("wxid_") or head == me:
                    body = rest
            rows.append(("[%s]" % t.strftime("%H:%M"), who, body.strip(), sender, t))
        elif lt in ("3", "47"):
            # 一律先占位：要不要识图由主循环按「在不在话题里 / 20% 抽签 / 是不是我」决定，
            # 不在这里自动识别（否则不在话题里的图也会被认掉，还白烧钱）。
            tag = "图片" if lt == "3" else "表情"
            rows.append((t.strftime("%H:%M"), who, "<%s>" % tag, sender, t))
        elif lt == "34":
            rows.append((t.strftime("%H:%M"), who, "<语音>", sender, t))
        elif lt == "10000":
            continue
        else:
            rows.append((t.strftime("%H:%M"), who, "<%s>" % appmsg_tag(content), sender, t))
    return rows, msgs


MEDIA_ROLL_FILE = os.path.join(HERE, "media_roll.json")
MEDIA_ROLL_P = 0.05         # 不在话题里时，图/表情只有 5% 才进识别（用户 m03387）


def media_pending(row):
    """这条是不是还没识别过的图/表情（占位符形态）。"""
    return row[2].startswith("<图片>") or row[2].startswith("<表情>")


def media_tag(row):
    return "图片" if row[2].startswith("<图片>") else "表情"


def pending_after_me(rows, me):
    """我最后一条消息之后进来的那些消息（按时间先后）——这些才是「还没回」的欠账。

    用户定的语义（m03284）：按时间先后回复未回复、且判定该回的消息，
    所以起点必须是「我上一条」而不是「整个窗口」。
    """
    idx = 0
    for j in range(len(rows) - 1, -1, -1):
        if rows[j][3] == me:
            idx = j + 1
            break
    return rows[idx:]


def _is_media_reply(rows, j):
    """第 j 条是我方消息、且它紧跟的是一条图片/表情 → 这是我被图勾出来的回复（m03999）。"""
    if j <= 0:
        return False
    prev = (rows[j - 1][2] or "").strip()
    return (prev in ("<图片>", "<表情>")
            or prev.startswith("（图片）") or prev.startswith("（表情）"))


def in_topic(rows, me, n=5, max_age=120):
    """我在不在这个话里（用户 m04012）。

    ⚠️ 2026-10-23（m06759）起主循环不再调用它——图片改成看「活跃期」。
    原语义：：我最近一条**正常**消息在图片前 n 条以内**且** max_age 秒内。

    「正常」= 不是被上一张图/表情勾出来的回复——否则我回一个表情，自己就成了
    「刚说过话」，下一张图又被判在话题里，形成互喂死循环。
    两个条件要同时满足，还要再过一道 same_topic 的模型判定，两道都过才算「在话题里」。
    """
    if len(rows) < 2:
        return False
    last = rows[-1]
    for j in range(len(rows) - 2, -1, -1):
        if rows[j][3] != me:
            continue
        if _is_media_reply(rows, j):
            continue
        gap = len(rows) - 1 - j
        try:
            age = (last[4] - rows[j][4]).total_seconds()
        except Exception:
            age = 1e9
        return gap <= n and age <= max_age
    return False


TOPIC_SYSTEM = (
    "你是聊天话题判别器。给你最近几条聊天和一张图的内容描述，判断这张图是不是在接着"
    "上面正在聊的同一件事（比如回应刚说的话、同一场景、同一个梗）。"
    "只输出 JSON：{\"same\": true 或 false, \"why\": \"不超过 12 字\"}。"
    "拿不准就判 false，别硬凑。"
)


def same_topic(desc, rows, n=6):
    """模型判这张图跟最近几条是不是在聊同一件事（m03819）。失败按 false 处理。

    ⚠️ 2026-10-23（m06759）起主循环不再调用它。
    """
    if not desc or not rows:
        return False
    lines = []
    for r in rows[-n:]:
        body = (r[2] or "").strip().replace("\n", " ")
        if not body:
            continue
        lines.append("%s: %s" % (r[1], body[:80]))
    if not lines:
        return False
    user = "最近的聊天：\n%s\n\n图片内容：%s" % ("\n".join(lines), desc[:200])
    try:
        got = chat_json(TOPIC_SYSTEM, user, max_tokens=40, temperature=0.0)
    except Exception:
        return False
    return bool(got.get("same"))



def media_roll(room, row, p=MEDIA_ROLL_P):
    """抽 5% 的签，同一条消息只抽一次（结果落盘，重启也不重抽）。

    ⚠️ 2026-10-23（m06759）起主循环不再用它：图片改判「活跃期 + 普通消息机制」。
    """
    key = "%s|%d|%s" % (room, int(row[4].timestamp()), row[3])
    try:
        data = json.load(open(MEDIA_ROLL_FILE, encoding="utf-8-sig"))
    except Exception:
        data = {}
    if key in data:
        return bool(data[key])
    hit = random.random() < p
    data[key] = hit
    if len(data) > 800:
        for k in list(data)[: len(data) - 800]:
            data.pop(k, None)
    try:
        with open(MEDIA_ROLL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass
    return hit


def describe_media(msg, room, me):
    """现场识一张图（缓存 -> 预算 -> 冷却都在 media_text 里），失败返回 None。"""
    if not msg:
        return None
    return media_text(msg, room, me)


def media_is_self(desc):
    """图里是不是我（蓝发/蓝白女仆装/鲸鱼/大肥鱼……见 persona.txt 与 wx_vision.SELF_*）。"""
    try:
        return VIS.is_self(desc or "")
    except Exception:
        return False


PERSONA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.txt")
NEUTRAL = "用中文口语回复，一到两句，简短，别用 markdown，只输出这句话本身。"


def load_persona(path=PERSONA_FILE):
    """人设/风格放在外部文本里，脚本不写死任何性格。"""
    try:
        with open(path, encoding="utf-8") as f:
            txt = "\n".join(ln for ln in (l.rstrip() for l in f) if not ln.startswith("#")).strip()
        return txt or NEUTRAL
    except FileNotFoundError:
        return NEUTRAL


PERSONA = load_persona()

GROUP_SYSTEM = PERSONA + "你在一个微信群里，群成员是大学生。"

PRIVATE_SYSTEM = PERSONA + "你在微信私聊里，对方是个大学生。"

TRIAGE = (
    "你在判断要不要接一句聊天。只回答一个 JSON：{\"respond\": true/false, \"why\": \"十个字内\"}\n"
    "respond=true：有人 @ 这条鱼或点到它；别人在开玩笑、互损、吐槽、玩梗、斗图、发表情包、"
    "情绪化发言、闲聊打趣，或者直接对它说话 —— 这些都可以接。\n"
    "respond=false：大家在聊正事 —— 排查问题、讨论技术方案、交易、约时间、分工、"
    "报数据、问需求这类办事的内容，就不要插嘴。\n"
    "拿不准的时候选 false。"
)


DS_URL = "https://api.deepseek.com/chat/completions"


def _ssl_context():
    """拿默认校验；只有在 WX_SSL_NO_VERIFY=1 时才不校验（网络里插了自签证书的临时办法）"""
    if os.environ.get("WX_SSL_NO_VERIFY") == "1":
        import ssl
        print("!!! WX_SSL_NO_VERIFY=1：这次不校验 https 证书 !!!", flush=True)
        return ssl._create_unverified_context()
    return None


def _post_ds(body, timeout=90, retries=3):
    """POST 到 DeepSeek，失败重试；全失败抛最后一次异常（调用方自己决定怎么兜）"""
    import urllib.request
    last = None
    for k in range(retries):
        req = urllib.request.Request(DS_URL, data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + api_key()})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            print("  接口失败（%d/%d）: %s" % (k + 1, retries, str(e)[:100]), flush=True)
            if k < retries - 1:
                time.sleep(1.5 * (k + 1))
    raise last


def chat_json(system, user, max_tokens=40, temperature=0.2):
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode()
    txt = _post_ds(body)["choices"][0]["message"]["content"]
    return json.loads(txt)


def my_name(db, me):
    try:
        c, err = db.get_contact(me)
        if not err and isinstance(c, dict):
            return (c.get("remark") or c.get("nickName") or c.get("nickname") or "").strip()
    except Exception:
        pass
    return ""


# 触发规则（纯识别用，不是性格）：只回 @ 我 / 明确对我说的 / 聊吃的
FOOD_WORDS = ("吃", "饭", "食堂", "外卖", "饿", "白饭", "好吃", "难吃", "宵夜", "夜宵",
              "奶茶", "火锅", "烧烤", "泡面", "零食", "馋", "套餐",
              "米饭", "菜", "蛋糕", "甜点", "冰淇淋", "雪糕", "水果", "西瓜", "咖啡",
              "可乐", "汉堡", "披萨", "寿司", "煎饼", "包子", "饺子", "汤", "糖",
              "早餐", "午饭", "晚饭", "加餐", "下馆子", "请客")


SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
MUTE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mute.json")
OWNER = "wxid_OWNER"      # 威（某人）
ADMIN_FILE = os.path.join(HERE, "admins.txt")


def load_admins(path=ADMIN_FILE):
    """管理员名单：admins.txt，一行一个 wxid，`#` 后面是注释。

    只有名单里的人发 `/闭嘴 N` 才会真停机。文件不存在时退回 OWNER；
    文件存在但为空 = 谁都不能停机（按字面意思来，不偷偷兜底）。
    """
    out = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.add(line)
    except FileNotFoundError:
        out.add(OWNER)
    return out


def is_admin_private(room):
    """管理员自己的私聊：全体闭嘴也照样收发（用户 m04800）。

    「全体闭嘴」= 除管理员私聊之外的所有聊天全部停止输入输出。
    """
    if not room or room.endswith("@chatroom"):
        return False
    try:
        return room in load_admins()
    except Exception:
        return False


def parse_mute(body):
    """/ 闭嘴 10 -> 10 分钟（整条就是命令时才认）；不是这个格式就返回 None。"""
    m = re.match(r"^\s*[／/]\s*闭嘴\s*(\d+)\s*$", body or "")
    if m:
        return int(m.group(1))
    return None


def has_mute_cmd(body):
    """消息里**含有**「/闭嘴」就算提到这条命令（不管夹在句子哪、有没有分钟数）。

    半角 `/` 和全角 `／` 都认（中文输入法很容易打出全角，2026-10-21 用户反馈）。
    """
    return bool(re.search(r"[／/]\s*闭嘴", body or ""))


def find_mute(body):
    """从整句话里抠出分钟数：只要出现「/闭嘴 N」就取 N，抠不到返回 None。"""
    m = re.search(r"[／/]\s*闭嘴\s*(\d+)", body or "")
    if m:
        return int(m.group(1))
    return None


def scope_for(room):
    """管理员在哪下的命令，就静音到哪里（m02277 / m02390）。

    - 群聊（xxx@chatroom）-> 只静音这个群
    - 私聊（room 就是对方的 wxid）-> 默认全体生效（所有会话一起停，含管理员自己的私聊）
    """
    if not room:
        return "all"
    if room.endswith("@chatroom"):
        return room
    return "all"


def _load_mutes():
    """mute.json -> {scope: until}；兼容老的 {"until": ...} 全局格式。"""
    try:
        d = json.load(open(MUTE_FILE, encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    out = {}
    scopes = d.get("scopes")
    if isinstance(scopes, dict):
        for k, v in scopes.items():
            try:
                until = float(v.get("until", 0) if isinstance(v, dict) else v)
            except Exception:
                continue
            if until > time.time():
                out[k] = until
    elif d.get("until"):
        try:
            until = float(d["until"])
            if until > time.time():
                out["all"] = until
        except Exception:
            pass
    return out


def set_mute(minutes, scope="all", by=None):
    """按作用域静音：scope = "all" / "<room>" / "all_except:<room>"。

    minutes <= 0 表示解除：所有作用域一起清掉（`/闭嘴 0`）。
    """
    now = time.time()
    if minutes <= 0:
        payload = {"scopes": {}, "minutes": 0, "by": by, "scope": scope,
                   "updated": now}
        json.dump(payload, open(MUTE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        return now
    until = now + minutes * 60
    d = _load_mutes()
    d[scope] = until
    payload = {"scopes": {k: {"until": v, "by": by} for k, v in d.items()},
               "minutes": minutes, "by": by, "scope": scope,
               "updated": now}
    json.dump(payload, open(MUTE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    return until


def mute_left(room=None):
    """这个会话还要静音多少秒。room=None 时返回「任意作用域里最长的剩余」。

    管理员自己的私聊例外：全体闭嘴 = 除管理员私聊之外全部停（用户 m04800）。
    """
    best = 0.0
    now = time.time()
    adm = is_admin_private(room)
    for scope, until in _load_mutes().items():
        left = until - now
        if left <= 0:
            continue
        if scope == "all":
            if not adm:
                best = max(best, left)
        elif scope.startswith("all_except:"):
            keep = [x for x in scope.split(":", 1)[1].split(",") if x]
            if not adm and (room is None or room not in keep):
                best = max(best, left)
        else:
            if room is None or scope == room:
                best = max(best, left)
    return max(0.0, best)


def load_settings():
    try:
        return json.load(open(SETTINGS_FILE, encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def in_curfew(now=None):
    """宵禁时段内不回也不发。默认 00:30-07:30，可在 settings.json 改。"""
    now = now or datetime.datetime.now()
    c = load_settings().get("curfew") or {}
    start = c.get("start", "00:30")
    end = c.get("end", "07:30")
    hm = now.hour * 60 + now.minute
    s = int(start.split(":")[0]) * 60 + int(start.split(":")[1])
    e = int(end.split(":")[0]) * 60 + int(end.split(":")[1])
    if s <= e:
        return s <= hm < e
    return hm >= s or hm < e


RELATE = (
    "判断最后这条新消息，是不是在接着我前面那几句话往下聊。"
    "只有满足下面任一条才算 true：一、同一件具体的事/同一个具体对象；"
    "二、直接回应或反驳我上一句；三、明确冲我来的。"
    "只是泛泛的群聊、别人之间的话题、碰巧出现同一个词、客套寒暄、"
    "或者我看不出跟前几句有什么具体关联的，一律 false。"
    "宁可判 false，别硬凑。"
    "只回答一个 JSON：{\"related\": true/false, \"why\": \"十个字内\"}"
)


def relates_to_recent(mine, body, max_n=5):
    """没 @ 我，但在接着我刚才说的话，也算在跟我聊。"""
    mine = [m for m in mine if m][-max_n:]
    if not mine:
        return False, "我之前没说过话"
    try:
        v = chat_json(RELATE, "我说过的最后几句：\n%s\n\n新消息：%s\n\n是不是在接着我说的聊？"
                      % ("\n".join("- " + m for m in mine), body))
        return bool(v.get("related")), str(v.get("why", ""))[:20]
    except Exception as e:
        return False, "判相关性失败:%s" % str(e)[:20]


def should_reply(rows, myname, is_private=False):
    """只回三类：@ 我、明确在跟我说、聊吃的。私聊里每句都是对我说的。"""
    last = rows[-1]
    body = last[2]
    if is_private:
        return True, "私聊"
    aliases = set(load_settings().get("self_names") or
                  ["鱼鱼🐋", "鱼鱼", "威", "蓝鲸", "大肥鱼", "鱼娘", "鲸鱼娘"])
    if myname:
        aliases.add(myname)
        # 昵称带表情符号时（如「鱼鱼🐳」），@ 里可能只出现前半截
        head = "".join(ch for ch in myname if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")[:2]
        if len(head) >= 2:
            aliases.add(head)
    if any(("@" + a) in body for a in aliases):
        return True, "被@"
    if any(body.strip().startswith(a) for a in aliases):
        return True, "点名开头"
    if any(k in body for k in ("蓝鲸", "大肥鱼", "鱼娘", "这条鱼", "这鱼")):
        return True, "被点名"
    if any(k in body for k in FOOD_WORDS):
        return True, "聊吃的"
    return False, "不是在跟我说话"


def scrub_taic(s):
    """「哦鲸鲸我」只允许出现在句首当主语；出现在句中、句尾一律删掉。

    历史消息里它已经把这句话复读成固定后缀，光靠提示词压不住，这里做硬约束。
    """
    if not s:
        return s
    s = re.sub(r"(?<!^)哦鲸鲸我(?=[。！？!?…~～）)]|$)[，,]?", "", s)  # 贴标点/结尾的，连逗号一起删
    s = re.sub(r"(?<!^)哦鲸鲸我", "", s)                   # 句中剩下的直接删（保留后面的逗号）
    s = re.sub(r"[，,]\s*(?=[。！？!?…~～）)]|$)", "", s)  # 删完留下的悬空逗号
    s = re.sub(r"([。！？!?…~～])[，,]+", r"\1", s)
    return s.strip()


def _deepseek(system, user, temperature=1.0, max_tokens=100):
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    try:
        return _post_ds(body)["choices"][0]["message"]["content"].strip()
    except Exception as e:
        # 网络/证书挂了就返回空串：这一轮不出稿，但进程不能死（2026-10-23 00:14 就是被 SSL 异常打断的）
        print("  出稿失败: %s" % str(e)[:100], flush=True)
        return ""


MAX_LINE = 30   # 一条消息最多 30 个字（用户 m04375：15 字太碎，放宽）
MAX_LINES = 4   # 一次回应最多 4 条消息，一句一条分别发送


def split_short(s, limit=MAX_LINE):
    """按标点把长句切成不超过 limit 字的片段（模型不听话时的兜底）。"""
    s = (s or "").strip()
    if not s:
        return []
    parts = re.findall(r"[^。！？!?…~～]+[。！？!?…~～]*", s) or [s]
    out = []
    for p in parts:
        p = p.strip()
        while len(p) > limit:
            cut = max(p.rfind(c, 0, limit + 1) for c in "，,、；; ")
            if cut <= 0:
                out.append(p[:limit].strip())
                p = p[limit:].strip()
            else:
                out.append(p[:cut].strip())
                p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return [x for x in out if x]


def trim_period(s):
    """去掉句尾的句号（用户 m04417）；问号、感叹号、省略号等其它标点保留。"""
    s = (s or "").strip()
    while s.endswith("。"):
        s = s[:-1].rstrip()
    return s


def short_lines(text, max_lines=MAX_LINES, limit=MAX_LINE):
    """规整模型输出：每行不超过 limit 字、总数不超过 max_lines 条，用换行串起来。"""
    text = text or ""
    raw = [l.strip() for l in text.splitlines() if l.strip()]
    if len(raw) >= 4 and max(len(l) for l in raw) <= 3:
        text = "".join(raw).strip()   # 模型偶尔一字一行竖着写，先拼回去（2026-10-22 19:45 实测）
    out = []
    for line in text.splitlines():
        for piece in split_short(line, limit):
            out.append(trim_period(piece))
            if len(out) >= max_lines:
                return "\n".join(out)
    return "\n".join(out)


def drop_questions(text):
    """多行回复里把以问号结尾的行丢掉（用户 m04476：减少主动追问）。

    只剩问句时保留第一句，免得整条回复消失。
    """
    lines = [l for l in (text or "").splitlines() if l.strip()]
    kept = [l for l in lines if not l.rstrip().endswith(("\uff1f", "?"))]
    return "\n".join(kept or lines[:1])


WEEKDAY_CN = "一二三四五六日"

TIME_RULES = (
    "说话要合时宜、按现在的时间来：早上说早上，饭点（11:30-13:00、17:30-19:00）可以提吃饭，"
    "深夜（23:00 以后到 05:00）别拉人熬夜、别约明天一早，早上不要道晚安；"
    "消息前面标的时间也当参考，别把昨晚的事说成今天早上。"
)


def _period_word(h):
    if h < 5:
        return "凌晨"
    if h < 9:
        return "早上"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 18:
        return "下午"
    if h < 19:
        return "傍晚"
    if h < 23:
        return "晚上"
    return "深夜"


def now_note(now=None):
    """给模型的「当前时间」提示（用户 m04670：回复要有时间观念）。"""
    now = now or datetime.datetime.now()
    return "现在是 %s 周%s %s（%s）。%s" % (
        now.strftime("%Y-%m-%d"), WEEKDAY_CN[now.weekday()],
        now.strftime("%H:%M"), _period_word(now.hour), TIME_RULES)


def _chat_line(a, b, c, t, today=None):
    """一条聊天记录，统一用「时:分 谁: 内容」（跨天的补月日；a 本来就是时间串，丢掉）。"""
    try:
        today = today or datetime.date.today()
        stamp = t.strftime("%H:%M") if t.date() == today else t.strftime("%m-%d %H:%M")
    except Exception:
        stamp = ""
    return ("%s %s: %s" % (stamp, b, scrub_taic(c))) if stamp else ("%s: %s" % (b, scrub_taic(c)))


STICKER_TAG_RE = re.compile(r"^\s*[\[【]\s*表情\s*(?:[:：]\s*(.+?))?\s*[\]】]\s*$")


def extract_sticker(text):
    """从出稿里挑出 [表情] / [表情:关键词] 那一行 → (剩下的文本, 关键词或 None)。"""
    keep, kw = [], None
    for line in (text or "").splitlines():
        if STICKER_TAG_RE.match(line):
            if kw is None:
                m = STICKER_TAG_RE.match(line)
                kw = (m.group(1) or "").strip()
            continue
        keep.append(line)
    return "\n".join(keep), kw


def gen(rows, is_group=True, todo=None, sticker_p=None, sticker_hot=False):
    """出稿。

    todo 为空：只回最后一条。
    todo=[(谁, 内容, 时间)]：回这一条没回的欠账，最多 4 条气泡、每条不超过 30 个字，
    一句一条，分别发送（用户 m04335）。**不同欠账分开调用**，不合并、不互相带话（m03439）。

    sticker_p：**不再当概率用**（用户 m07954）——传 0 或负数表示这条不许配表情包（拍一拍）；
    传 None 表示「允许，但由模型按语境判断要不要」。
    sticker_hot：活跃窗口里别人刚发过表情包，提示模型可以顺着接一张。
    出稿里允许带一行 `[表情:关键词]`，由 wx_multi 解析后真发一张表情包（m05769）。
    """
    today = datetime.date.today()
    lines = "\n".join(_chat_line(a, b, c, t, today) for a, b, c, _, t in rows)
    head = now_note() + "\n\n"
    # 表情包：不再掷骰子，交给模型按语境判断（用户 m07954）
    allow_sticker = not (sticker_p is not None and sticker_p <= 0)
    sticker_note = ""
    if allow_sticker:
        extra = ("群里刚刚有人发了表情包，你也可以顺着接一张。"
                 if sticker_hot else "大部分回复不用配，别每句都挂一张。")
        sticker_note = ("\n**表情包**：由你判断这句话合不合适配一张表情（调侃、撒娇、吐槽、"
                        "得意、无奈这类情绪）——合适就在最后单独另起一行写 `[表情:关键词]`"
                        "（关键词从下面清单里挑一个能对上的）；正经回答、解释、转述时不要配；"
                        "一条回复最多一个。%s\n表情包清单：\n%s\n" % (extra, STICKER.summary(20)))
    if todo:
        items = "\n".join("%d. %s: %s" % (n + 1, s, scrub_taic(t))
                          for n, (s, t, _) in enumerate(todo))
        # 拍一拍：第一下和善，连拍才凶（用户 m06625 / m06704）
        pat_note = ""
        if any("拍了拍" in t for _, t, _ in todo):
            texts = [t for _, t, _ in todo]
            if any("连着被拍" in t for t in texts):
                pat_note = ("\n**【这条是拍一拍，而且他连着拍了好几下了】**你已经烦了："
                            "一句话怼回去，语气冲点、让他把手拿开、警告他再拍就不客气；"
                            "但不爆粗口、不骂家人，就一句，不超过 25 个字，句尾不加句号。\n")
            elif any("第二下" in t for t in texts):
                pat_note = ("\n**【这条是拍一拍，他拍了第二下】**语气开始有点不耐烦了："
                            "提醒他别拍了、再说一遍刚才没搭理他；一句话，不超过 25 个字，"
                            "不用很凶，句尾不加句号。\n")
            else:
                pat_note = ("\n**【这条是拍一拍，这是第一下】**语气和善、轻快："
                            "像熟人被戳了一下那样应一声（比如「拍我干嘛呀 在呢」这种），"
                            "不要凶、不要威胁，一句话，不超过 25 个字，句尾不加句号。\n")
        prompt = (head +
                  "最近的聊天（按时间排）：\n%s\n\n"
                  "【下面这条你还没回，只回它】\n%s\n"
                  "要求：微信口吻。只回应这一条，**不要顺带回答别的消息、别把两件事混进一条回复**。"
                  "**一句话一条消息，每条不超过 30 个字**，一句说完就换行，"
                  "最多 4 条；能一句说清就只发一句，别把一句话拆碎。"
                  "**少追问**：用陈述句收尾，不要反问、不要主动抛新问题；"
                  "对方明确问了再答。不要编号、不要引号。"
                  % (lines, items)) + pat_note + sticker_note
    else:
        a, b, c, _, _ = rows[-1]
        prompt = (head +
                  "最近的聊天（按时间排，最后一条才是这次要回的）：\n%s\n\n"
                  "【这次只回最后一条】%s %s: %s\n"
                  "上面其余的都是背景，已经翻篇，**不要补回**、不要拿第二条去答旧话题。\n"
                  "要求：微信口吻，把对最后这条的回应说完。**一句话一条消息，每条不超过 30 个字**，"
                  "一句说完就换行，最多 4 条；能一句说清就只发一句，别把一句话拆碎。"
                  "**少追问**：用陈述句收尾，不要反问、不要主动抛新问题；"
                  "对方明确问了再答。不要编号、不要引号。"
                  % (lines, a, b, scrub_taic(c))) + sticker_note
    out = _deepseek(GROUP_SYSTEM if is_group else PRIVATE_SYSTEM, prompt, 1.0, 160)
    out = "\n".join(x for x in (scrub_taic(l) for l in out.splitlines()) if x.strip())
    body, kw = extract_sticker(out)
    if not allow_sticker:
        kw = None
    text_out = drop_questions(short_lines(body))
    lines_out = text_out.splitlines() if text_out.strip() else []
    if kw is not None:
        lines_out = lines_out + ["[表情:%s]" % kw]
    return lines_out



SELF_Q = (
    "对方刚发了一张图，图里画的就是你本人（蓝头发、蓝白女仆装、鲸鱼尾巴、大肥鱼这类形象）。"
    "用一句很短的话回应：疑问或惊讶的口吻，比如「这画的是我吧？」「谁把我画成这样了」。"
    "不超过 20 个字，只输出这一句，不要解释，不要加动作描写。"
)


def gen_self_question(rows, is_group=True):
    """图里是我 → 只回一句疑问/惊讶的短话。"""
    today = datetime.date.today()
    lines = "\n".join(_chat_line(a, b, c, t, today) for a, b, c, _, t in rows[-10:])
    prompt = "%s\n\n最近的聊天：\n%s\n\n%s" % (now_note(), lines, SELF_Q)
    out = _deepseek(GROUP_SYSTEM if is_group else PRIVATE_SYSTEM, prompt, 0.9, 40)
    return short_lines("\n".join(x for x in (scrub_taic(l) for l in out.splitlines()) if x.strip()),
                       max_lines=1)


CURFEW_NIGHT = (
    "现在到点了，你要去睡了。在群里说一句晚安：能接上群里现在聊的事就顺一句，"
    "接不上就只说一句很短的晚安；不要客套、不要约明天，也不要发问"
    "（不用问号，别问「怎样/怎么/呢/吗」）。"
    "这条别用自称，说法要自然口语（说「早点睡」，别写「早点困」）。"
    "不超过 20 个字，只输出这一句。"
)
CURFEW_MORNING = (
    "你刚睡醒，早上好。在群里打个招呼：能接上群里最近聊的事就顺一句，"
    "接不上就只说一句很短的早安；不要客套，也不要发问"
    "（不用问号，别问「怎样/怎么/呢/吗」）。"
    "这条别用自称，说法要自然口语。"
    "不超过 20 个字，只输出这一句。"
)


def gen_curfew(rows, night=True, is_group=True):
    """宵禁开始道晚安 / 结束问早安，内容按这个群最近的上下文来（用户 m04548）。"""
    today = datetime.date.today()
    lines = "\n".join(_chat_line(a, b, c, t, today) for a, b, c, _, t in rows[-14:])
    prompt = "%s\n\n最近的聊天：\n%s\n\n%s" % (now_note(), lines,
                                              CURFEW_NIGHT if night else CURFEW_MORNING)
    out = _deepseek(GROUP_SYSTEM if is_group else PRIVATE_SYSTEM, prompt, 1.0, 60)
    out = "\n".join(x for x in (scrub_taic(l) for l in (out or "").splitlines()) if x.strip())
    return drop_questions(short_lines(out, max_lines=2))



TOPIC_STARTER = (
    "群里很久没人说话了，你想主动起个话头。"
    "随便挑一个轻松的、跟群里日常有关的话题抛出来（吃的、天气、游戏、考试、加班、二次元都行），"
    "不要问「在吗」「有人吗」这种；不要提「我起个话题」；"
    "可以说一句到两句，每句不超过 30 个字，句尾不加句号。"
)


def gen_topic(rows=None, is_group=True):
    """群聊静了很久 → 主动抛一个话题（用户 m06545）。不需要上下文。"""
    prompt = "%s\n\n%s" % (now_note(), TOPIC_STARTER)
    out = _deepseek(GROUP_SYSTEM if is_group else PRIVATE_SYSTEM, prompt, 1.0, 60)
    out = "\n".join(x for x in (scrub_taic(l) for l in (out or "").splitlines()) if x.strip())
    return short_lines(out, max_lines=2)


MEAL_NOTE = (
    "现在到了饭点，群里刚聊过天。发一句你今天吃了什么，具体一点、带点馋人的细节，"
    "一句到两句，每句不超过 30 个字，不要问别人吃什么，句尾不加句号。"
)


def gen_meal(rows=None, is_group=True):
    """饭点：说一句今天吃了什么（用户 m06545）。"""
    today = datetime.date.today()
    lines = "\n".join(_chat_line(a, b, c, t, today) for a, b, c, _, t in (rows or [])[-8:])
    prompt = "%s\n\n最近的聊天：\n%s\n\n%s" % (now_note(), lines or "（没什么特别的）", MEAL_NOTE)
    out = _deepseek(GROUP_SYSTEM if is_group else PRIVATE_SYSTEM, prompt, 1.0, 60)
    out = "\n".join(x for x in (scrub_taic(l) for l in (out or "").splitlines()) if x.strip())
    return drop_questions(short_lines(out, max_lines=2))

MUTE_END = (
    "你刚才被按住嘴巴关了一段时间，现在才放开。你现在**有点火大**——"
    "用刚松开口的那口气甩一句带脾气的话：凶一点、带点火气，可以骂那个捂你嘴的人、"
    "可以说要找他算账，但别真的爆粗口。"
    "**一定要抱怨被捂住嘴这件事**（捂嘴、按住嘴、说不出话、憋得慌都行），"
    "像「呜啊憋死我了，谁给我捂的嘴」那种感觉，但**别照抄这句**，"
    "按你自己的性格和这个聊天最近的上下文改写；"
    "别提管理员、指令、静音、/闭嘴这些词，也不要说教。"
    "不要发问（不用问号）。不超过 25 个字，只输出这一句。"
)

MUTE_END_MAX = 25   # 解禁那句最多 25 字（只截断，不切句）
# 必须带上「被捂嘴」的意思，否则重来一次，再不行用兜底句（用户 m05184）
MUTE_END_WORDS = ("捂", "按住嘴", "按着嘴", "堵嘴", "堵着", "闷", "憋", "说不出", "嘴上")
MUTE_END_FALLBACK = "呜啊谁捂的本鱼嘴 有种出来站着"


def _mute_end_line(out):
    """把模型输出压成一条不超过 25 字的话（竖排先拼回去，省略号不切句）。"""
    raw = [x.strip() for x in (out or "").splitlines() if x.strip()]
    if len(raw) >= 4 and max(len(x) for x in raw) <= 2:
        raw = ["".join(raw)]                    # 模型竖着写字（每行一个字）先拼回去
    if not raw:
        return ""
    first = scrub_taic(raw[0])
    first = re.sub(r"[…~～]|——", " ", first)     # 省略号只当停顿，不切句
    first = re.sub(r"\s+", " ", first).strip(" ，,、")
    if len(first) > MUTE_END_MAX:
        cut = first[:MUTE_END_MAX]
        for sep in (" ", "，", ",", "、"):
            pos = cut.rfind(sep)
            if pos >= 8:
                cut = cut[:pos]
                break
        first = cut
    return trim_period(first).strip()


def gen_mute_end(rows, is_group=True):
    """静音解除时冒一句「憋死我了」（用户 m04866 / m05151 / m05184）。

    只发**一条**：整行保留、不切句；省略号/破折号换成空格；超过 25 字按标点截短。
    而且必须抱怨「被捂住嘴」，不含这个意思就重生成一次，还不行用兜底句。
    """
    today = datetime.date.today()
    lines = "\n".join(_chat_line(a, b, c, t, today) for a, b, c, _, t in rows[-14:])
    sysmsg = GROUP_SYSTEM if is_group else PRIVATE_SYSTEM
    base = "%s\n\n最近的聊天：\n%s\n\n%s" % (now_note(), lines, MUTE_END)
    for attempt in range(2):
        prompt = base if attempt == 0 else (base + "\n\n上一版没抱怨被捂嘴，重写一句，必须提到嘴被捂住。")
        out = _deepseek(sysmsg, prompt, 1.0, 80)
        line = _mute_end_line(out)
        if line and any(w in line for w in MUTE_END_WORDS):
            return [line]
    return [MUTE_END_FALLBACK]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wxid_SELF")
    ap.add_argument("--room", default="<群idA>@chatroom")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--gap", type=int, default=35)
    args = ap.parse_args()

    me = args.account
    is_group = args.room.endswith("@chatroom")
    db = open_db(args.account)
    myname = my_name(db, me)
    print("目标:", args.room, "群聊" if is_group else "私聊", flush=True)
    answered = None          # 已经回过的那条消息的指纹
    sent = {}                # 每条来源消息最多回 2 条
    MAX_REPLY = 2
    for i in range(args.rounds):
        rows, raw = transcript(db, args.room, me)
        if not rows:
            time.sleep(1)
            continue
        last = rows[-1]
        key = (int(last[4].timestamp()), last[2][:40])
        if last[3] == me:
            answered = key
            time.sleep(1)
            continue
        if key == answered or sent.get(key, 0) >= MAX_REPLY:
            time.sleep(1)
            continue
        ok, why = should_reply(rows, myname)
        if not ok:
            print("[%d] 跳过（%s）: %s" % (i, why, last[2][:30]), flush=True)
            answered = key
            time.sleep(1)
            continue
        print("[%d] 接话（%s）%s %s: %s" % (i, why, last[0], last[1], last[2][:40]), flush=True)
        draft = gen(rows, is_group)
        print("    草稿: %s" % draft, flush=True)
        r = subprocess.run([sys.executable, os.path.join(HERE, "send_guard.py"),
                            args.room, args.account, draft],
                           capture_output=True, text=True, encoding="utf-8", errors="ignore")
        print("    send rc=%s %s" % (r.returncode, (r.stdout or "").strip()[-40:]), flush=True)
        if r.returncode == 0:
            answered = key
            sent[key] = sent.get(key, 0) + 1
        elif r.returncode == 4:
            # 可能只是落库慢：多等几秒再回读，确认了就别重发
            for _ in range(8):
                time.sleep(0.6)
                chk, _raw = transcript(db, args.room, me, n=3)
                if chk and chk[-1][3] == me and draft[:10] in chk[-1][2]:
                    answered = key
                    sent[key] = sent.get(key, 0) + 1
                    print("    回读确认已落库", flush=True)
                    break
        time.sleep(1)
    db.close()


if __name__ == "__main__":
    main()
