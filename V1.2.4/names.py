# -*- coding: utf-8 -*-
"""把「用户填的名字」解析成 wxid。

普通人记不住 `wxid_xxx`，在面板 / 配置文件里直接写会话名（含表情、空格、全角字符）就行，
这里负责把它翻成真正的 id：

    resolve("群A")  -> ("<群idA>@chatroom", [])
    resolve("好友A")        -> ("wxid_FRIEND", [])
    resolve("不存在")      -> (None, [])          # 没找到
    resolve("群")          -> (None, [...候选...])  # 多个都含「群」，交给调用方提示

匹配规则：
  1. 已经是 wxid / xxx@chatroom / filehelper → 直接返回；
  2. 名字先做归一化（NFKC 全角转半角、去掉变体选择符与零宽字符、去掉所有空白、casefold），
     这样「群A」和「群A 🚗」、「ＫＡＩ」和「kai」都能对上；
  3. 先找完全相同的，再找包含关系的；多个候选就返回候选列表让用户挑。
"""
import re
import unicodedata

_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_VS = re.compile("[\ufe0e\ufe0f]")
_SPACE = re.compile(r"\s+")
_ID_RE = re.compile(r"^(wxid_[0-9A-Za-z]+|filehelper|[0-9A-Za-z]+@chatroom)$", re.I)


def norm(s):
    """归一化：全角→半角、去变体符/零宽/空白、大小写折叠。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = _VS.sub("", s).translate(_ZW)
    return _SPACE.sub("", s).casefold()


def looks_like_id(text):
    return bool(_ID_RE.match(str(text or "").strip()))


def _account():
    """当前该用哪个账号：先看 rooms.json 的第一条，再看 keys.json。"""
    import json
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    try:
        rooms = json.load(open(os.path.join(here, "rooms.json"), encoding="utf-8-sig"))
        for r in rooms or []:
            if r.get("account"):
                return r["account"]
    except (FileNotFoundError, OSError, ValueError):
        pass
    try:
        keys = json.load(open(os.path.join(here, "keys.json"), encoding="utf-8-sig"))
        for k in keys or {}:
            if k != "imageKeys":
                return k
    except (FileNotFoundError, OSError, ValueError):
        pass
    return None


def directory(account=None, db=None):
    """返回 [(wxid, 显示名, 归一化名...)]，来源=最近会话 + 联系人表。"""
    import wx_chat_loop as L

    out = {}
    if db is None:
        account = account or _account()
        if not account:
            return []
        db = L.open_db(account)
    try:
        sess, _ = db.get_sessions()
    except Exception:
        sess = []
    for s in sess or []:
        u = s.get("username") or ""
        if not u:
            continue
        names = set()
        try:
            names.add(L.contact_name(db, u) or "")
        except Exception:
            pass
        for key in ("nickname", "nickName", "remark", "alias"):
            try:
                r = db.get_contact(u)
                c = r[0] if isinstance(r, tuple) else r
                v = (c or {}).get(key)
                if v:
                    names.add(str(v))
            except Exception:
                pass
        names.discard("")
        out[u] = names
    return [(u, sorted(n)) for u, n in out.items()]


def display(wxid, account=None):
    """反查显示名（解析不出来就返回空串）。"""
    import wx_chat_loop as L
    try:
        account = account or _account()
        db = L.open_db(account) if account else None
        if db is None:
            return ""
        n = L.contact_name(db, wxid)
        return "" if (not n or n.startswith("wxid_")) else n
    except Exception:
        return ""


def resolve(text, account=None, db=None):
    """名字 → (wxid, 候选列表)。找到唯一匹配返回 (wxid, [])。"""
    raw = str(text or "").strip()
    if not raw:
        return None, []
    if looks_like_id(raw):
        return raw, []

    want = norm(raw)
    if not want:
        return None, []

    items = directory(account=account, db=db)

    exact = []
    partial = []
    for wxid, names in items:
        normed = [norm(n) for n in names]
        normed.append(norm(wxid))
        if want in normed:
            exact.append((wxid, names))
        elif any(want in n for n in normed if n):
            partial.append((wxid, names))

    if len(exact) == 1:
        return exact[0][0], []
    if len(exact) > 1:
        return None, [{"wxid": w, "names": n} for w, n in exact]
    if len(partial) == 1:
        return partial[0][0], []
    if len(partial) > 1:
        return None, [{"wxid": w, "names": n} for w, n in partial]
    return None, []


def resolve_many(values, account=None, db=None):
    """批量解析，返回 (ok_list, errors)。errors 里是 {input, candidates}。"""
    ok, errs = [], []
    for v in values or []:
        v = str(v or "").strip()
        if not v:
            continue
        wxid, cands = resolve(v, account=account, db=db)
        if wxid:
            ok.append(wxid)
        else:
            errs.append({"input": v, "candidates": cands})
    # 去重保序
    seen, uniq = set(), []
    for w in ok:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq, errs


if __name__ == "__main__":
    import sys

    for t in sys.argv[1:] or ["群A", "群A", "好友A", "KAI", "威", "群C", "不存在的人"]:
        w, c = resolve(t)
        print("%-14s -> %-24s %s" % (t, w, "" if not c else ("候选 %d 个: %s" % (len(c), [x["names"][:1] for x in c]))))
