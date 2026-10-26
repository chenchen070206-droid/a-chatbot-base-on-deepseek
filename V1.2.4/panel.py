# -*- coding: utf-8 -*-
"""wx-chatbot 本地控制面板（只监听 127.0.0.1，无登录）。

用法:
    python panel.py            # 默认 http://127.0.0.1:8790
    python panel.py --port 8791

能做的事：
    概览   —— 机器人是否在跑 / hook 通不通 / 微信进程 / 静音与活跃窗口
    参数   —— tuning.json 里的旋钮，保存后主循环下一轮生效（不用重启）
    名单   —— rooms.json / whitelist.json / admins.txt
    人设   —— persona.txt 在线编辑
    表情包 —— 看库、发一张、删除
    工具   —— 给某个会话发测试消息、设置/解除静音、启停机器人
    日志   —— 最近 N 行 run_bot.log
"""
import argparse
import ctypes
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tuning as TUNE  # noqa: E402

LOG_FILE = os.path.join(HERE, "run_bot.log")
HEART = os.path.join(HERE, "bot_heartbeat.json")


# ---------------------------------------------------------------- 小工具
def read_json(name, default):
    p = os.path.join(HERE, name)
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        return default


def write_json(name, obj):
    p = os.path.join(HERE, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def read_text(name, default=""):
    try:
        return open(os.path.join(HERE, name), encoding="utf-8-sig").read()
    except (FileNotFoundError, OSError):
        return default


def write_text(name, text):
    p = os.path.join(HERE, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


def tail(path, n=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 64 * 1024
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        lines = data.decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except (FileNotFoundError, OSError):
        return []


def bot_state():
    """看心跳：主循环每轮写一次 bot_heartbeat.json。"""
    hb = read_json("bot_heartbeat.json", None)
    if not hb:
        return {"running": False, "age": None, "pid": None, "round": None}
    age = time.time() - float(hb.get("ts") or 0)
    return {"running": age < 30, "age": round(age, 1), "pid": hb.get("pid"), "round": hb.get("round")}


def hook_state():
    try:
        import hook_send as HS
        ok = HS.available(timeout=2)
        prof = HS.self_profile(timeout=2) if ok else None
        return {"ok": bool(ok), "wxid": (prof or {}).get("wxid") if isinstance(prof, dict) else None}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def wechat_procs():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=0x08000000,
        ).stdout
        return max(0, out.count("Weixin.exe"))
    except Exception:                                          # noqa: BLE001
        return None


def mutes():
    raw = read_json("mute.json", {})
    scopes = raw.get("scopes") if isinstance(raw, dict) else None
    now = time.time()
    out = []
    for scope, info in (scopes or {}).items():
        try:
            until = float(info.get("until") or 0)
        except (TypeError, ValueError):
            continue
        if until > now:
            out.append({"scope": scope, "left": round(until - now), "by": info.get("by")})
    return out


def active_windows():
    raw = read_json("active_until.json", {})
    now = time.time()
    out = []
    for room, until in (raw or {}).items():
        try:
            left = float(until) - now
        except (TypeError, ValueError):
            continue
        if left > 0:
            out.append({"room": room, "left": round(left, 1)})
    return out


def reply_counts():
    raw = read_json("reply_count.json", {})
    return {k: v for k, v in (raw or {}).items() if isinstance(v, (int, float))}


def room_names():
    out = {}
    for r in read_json("rooms.json", []) or []:
        if isinstance(r, dict) and r.get("room"):
            out[r["room"]] = r.get("name") or r["room"]
    return out


def sticker_list():
    import sticker_lib as S
    rows = []
    for e in S.entries():
        p = os.path.join(S.LIB, e.get("file") or "")
        rows.append({
            "file": e.get("file"),
            "what": e.get("what"),
            "scene": e.get("scene"),
            "keys": e.get("keys"),
            "size": os.path.getsize(p) if os.path.exists(p) else 0,
        })
    return rows


# ---------------------------------------------------------------- API
KEYCAP_LOG = os.path.join(HERE, "key_capture.log")
KEYCAP_STATE = os.path.join(HERE, "key_capture.state")


def keycap_status():
    """重新抓库 key 的进度。"""
    st = {"running": False, "launched": 0, "log": ""}
    try:
        st["launched"] = int(io.open(KEYCAP_STATE, encoding="utf-8").read().strip() or 0)
    except Exception:
        pass
    try:
        txt = io.open(KEYCAP_LOG, encoding="utf-8", errors="replace").read()
    except Exception:
        txt = ""
    st["log"] = "\n".join(txt.strip().splitlines()[-8:])
    if st["launched"] and time.time() - st["launched"] < 180 and "[done]" not in txt:
        st["running"] = True
    return st


def keycap_start():
    """用管理员权限跑 wx4_key.py，结果写 keys.json。"""
    py = os.path.join(HERE, "python", "python.exe")
    if not os.path.isfile(py):
        py = sys.executable
    script = os.path.join(HERE, "wechat-mcp", "scripts", "wx4_key.py")
    if not os.path.isfile(script):
        return {"ok": False, "error": "找不到 wx4_key.py：%s" % script}
    io.open(KEYCAP_STATE, "w", encoding="utf-8").write(str(int(time.time())))
    io.open(KEYCAP_LOG, "w", encoding="utf-8").write(
        "启动抓取…\n请在 UAC 之后、切到微信点开任意聊天。\n")
    cmd = '"%s" "%s" --json --timeout 120 >> "%s" 2>&1 & echo [done] >> "%s"' % (
        py, script, KEYCAP_LOG, KEYCAP_LOG)
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", "/c " + cmd, HERE, 0)
    if r <= 32:
        return {"ok": False, "error": "UAC 被取消或启动失败（代码 %d）" % r}
    return {"ok": True, "msg": "已启动，请看 UAC 弹窗和微信窗口"}


def api_overview():
    return {
        "bot": bot_state(),
        "hook": hook_state(),
        "wechat": wechat_procs(),
        "mutes": mutes(),
        "active": active_windows(),
        "replies": reply_counts(),
        "rooms": room_names(),
        "whitelist": read_json("whitelist.json", []),
        "admins": read_text("admins.txt"),
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def api_send(body):
    room = (body.get("room") or "").strip()
    text = (body.get("text") or "").strip()
    if not room or not text:
        return {"ok": False, "error": "room 和 text 都要填"}
    import hook_send as HS
    ok, data = HS.send_text(room, text)
    return {"ok": bool(ok), "data": data}


def api_mute(body):
    room = (body.get("room") or "").strip()
    minutes = float(body.get("minutes") or 0)
    import wx_chat_loop as L
    if minutes <= 0:
        L.set_mute(0)
        return {"ok": True, "cleared": True, "mutes": mutes()}
    if not room:
        return {"ok": False, "error": "要选一个会话"}
    L.set_mute(minutes, scope=L.scope_for(room), by="panel")
    return {"ok": True, "mutes": mutes()}


def api_bot(body):
    action = (body.get("action") or "").strip()
    if action == "stop":
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", os.path.join(HERE, "stop_bot.ps1")],
                       capture_output=True, text=True, timeout=60)
        time.sleep(1)
        return {"ok": True, "bot": bot_state()}
    if action == "start":
        bat = os.path.join(HERE, "start_bot.bat")
        subprocess.Popen(["cmd", "/c", bat, "run_bot.log"], cwd=HERE,
                         creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        time.sleep(3)
        return {"ok": True, "bot": bot_state()}
    return {"ok": True, "bot": bot_state()}


def _settings_path():
    return os.path.join(HERE, "settings.json")


def allow_dsh_key():
    """是否允许读取本机 ~/.dsh 凭证（settings.json 的 panel.allowDshKey，默认 False）。"""
    try:
        st = json.load(open(_settings_path(), encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return ((((st or {}).get("panel") or {}).get("allowDshKey")) is True)


def apikey_state():
    env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env:
        return {"set": True, "source": "环境变量", "allowDsh": allow_dsh_key(), "masked": env[:7] + "…" + env[-4:] if len(env) > 12 else "***"}
    try:
        st = json.load(open(_settings_path(), encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        st = {}
    key = (((st or {}).get("deepseek") or {}).get("apiKey") or "").strip()
    if key:
        return {"set": True, "source": "settings.json", "allowDsh": allow_dsh_key(),
                "masked": key[:7] + "…" + key[-4:] if len(key) > 12 else "***"}
    if not allow_dsh_key():
        return {"set": False, "source": "", "masked": "", "allowDsh": False}
    cred = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    try:
        for line in open(cred, encoding="utf-8"):
            if line.strip().startswith("DEEPSEEK_API_KEY:"):
                k = line.split(":", 1)[1].strip()
                return {"set": True, "source": "DeepSeek Harness 凭证（本机才行）",
                        "allowDsh": True,
                        "masked": k[:7] + "…" + k[-4:] if len(k) > 12 else "***"}
    except (FileNotFoundError, OSError):
        pass
    return {"set": False, "source": "", "masked": "", "allowDsh": allow_dsh_key()}


def _save_allow_dsh(flag):
    p = _settings_path()
    try:
        st = json.load(open(p, encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        st = {}
    st.setdefault("panel", {})["allowDshKey"] = bool(flag)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def api_apikey_set(body):
    if "allowDsh" in body:
        _save_allow_dsh(bool(body.get("allowDsh")))
        return {"ok": True, "state": apikey_state()}
    key = (body.get("key") or "").strip()
    if key and not key.startswith("sk-"):
        return {"ok": False, "error": "DeepSeek 的 Key 一般以 sk- 开头，检查一下？"}
    path = _settings_path()
    try:
        cfg = json.load(open(path, encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("deepseek", {})["apiKey"] = key
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return {"ok": True, "state": apikey_state()}


def api_apikey_test(body):
    import urllib.error
    import urllib.request
    key = (body.get("key") or "").strip() or apikey_state().get("masked")
    if not key or key.startswith("***") or "…" in key:
        # 用现有配置里的完整 key 再取一次
        key = ""
        try:
            st = json.load(open(_settings_path(), encoding="utf-8-sig"))
            key = (((st or {}).get("deepseek") or {}).get("apiKey") or "").strip()
        except (FileNotFoundError, OSError, ValueError):
            pass
        if not key:
            env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
            key = env
        if not key:   # 最后再试一次 DSH 的凭证文件
            cred = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
            try:
                for line in open(cred, encoding="utf-8"):
                    if line.strip().startswith("DEEPSEEK_API_KEY:"):
                        key = line.split(":", 1)[1].strip()
                        break
            except (FileNotFoundError, OSError):
                pass
    if not key:
        return {"ok": False, "error": "还没有 Key 可测"}
    try:
        req = urllib.request.Request("https://api.deepseek.com/models",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=20) as r:
            body_txt = r.read(300).decode("utf-8", "replace")
        return {"ok": True, "detail": body_txt}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %s（%s）" % (e.code, "Key 不对 / 没余额" if e.code in (401, 402) else e.reason)}
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def api_sticker_send(body):
    name = (body.get("file") or "").strip()
    room = (body.get("room") or "").strip()
    if not name or not room:
        return {"ok": False, "error": "file 和 room 都要填"}
    import hook_send as HS
    import sticker_lib as S
    path = os.path.join(S.LIB, name)
    if not os.path.exists(path):
        return {"ok": False, "error": "库里没有这个文件"}
    ok, data = HS.send_image(room, path)
    if ok:
        try:
            S.mark_used(name)
        except Exception:                                      # noqa: BLE001
            pass
    return {"ok": bool(ok), "data": data}


def api_sticker_delete(body):
    name = (body.get("file") or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return {"ok": False, "error": "文件名不合法"}
    import sticker_lib as S
    path = os.path.join(S.LIB, name)
    if not os.path.exists(path):
        return {"ok": False, "error": "文件不存在"}
    os.remove(path)
    # 从 index.md 里删掉这一行
    idx = os.path.join(S.LIB, "index.md")
    try:
        lines = open(idx, encoding="utf-8").read().splitlines()
        keep = [ln for ln in lines if ("| %s |" % name) not in ln and not ln.strip().startswith("| %s " % name)]
        open(idx, "w", encoding="utf-8").write("\n".join(keep) + "\n")
    except OSError:
        pass
    return {"ok": True}

def _bad_msg(bad):
    parts = []
    for item in bad[:4]:
        cands = item.get("candidates") or []
        if cands:
            parts.append("「%s」有 %d 个候选：%s" % (
                item["input"], len(cands),
                "、".join((c["names"][0] if c.get("names") else c["wxid"]) for c in cands[:5])))
        else:
            parts.append("「%s」没找到" % item["input"])
    return "；".join(parts) + "（可以填 wxid，或换个更准确的名字）"


# ---------------------------------------------------------------- 名字 → wxid
def _fix_names(values):
    """把一串「名字或 id」解析成 wxid，返回 (结果, 认不出来的)。"""
    import names
    return names.resolve_many(values)


def _fix_rooms(rooms):
    import names
    out, bad = [], []
    for r in rooms or []:
        r = dict(r or {})
        raw = str(r.get("room") or "").strip()
        label = str(r.get("name") or "").strip()
        acct = str(r.get("account") or "").strip()
        if not raw:                      # 只填了「名字」列也认（m09094）
            raw = label
        if not raw:
            continue
        wxid, cands = names.resolve(raw, account=acct or None) if not names.looks_like_id(raw) else (raw, [])
        if not wxid:
            bad.append({"input": raw, "candidates": cands})
            continue
        r["room"] = wxid
        if not str(r.get("name") or "").strip():
            r["name"] = names.display(wxid, account=acct or None) or wxid
        if not acct:
            acct = names._account() or ""
            r["account"] = acct
        r.setdefault("enabled", True)
        out.append(r)
    return out, bad


def _fix_admins(text):
    """admins.txt：每行 wxid 或名字，可带 # 注释。"""
    import names
    keep, bad = [], []
    for line in str(text or "").splitlines():
        raw = line.rstrip()
        if not raw.strip():
            keep.append(raw)
            continue
        if raw.lstrip().startswith("#"):
            keep.append(raw)
            continue
        head, sep, tail = raw.partition("#")
        who = head.strip()
        comment = (sep + tail) if sep else ""
        if not who:
            keep.append(raw)
            continue
        wxid, cands = names.resolve(who) if not names.looks_like_id(who) else (who, [])
        if not wxid:
            bad.append({"input": who, "candidates": cands})
            continue
        keep.append("    ".join([wxid, comment.strip()]) if comment else wxid)
    return "\n".join(keep) + "\n", bad


# ---------------------------------------------------------------- HTTP
def make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "wxchatbot-panel/1.0"

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            raw = payload if isinstance(payload, bytes) else \
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):                                       # noqa: N802
            path = self.path.split("?")[0]
            q = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&") if "=" in p) \
                if "?" in self.path else {}
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/api/overview":
                    return self._send(200, {"ok": True, **api_overview()})
                if path == "/api/tuning":
                    return self._send(200, {"ok": True, **TUNE.describe()})
                if path == "/api/rooms":
                    return self._send(200, {"ok": True, "rooms": read_json("rooms.json", [])})
                if path == "/api/whitelist":      # 白名单已与 rooms 合并（m09032）：只返回启用的
                    rooms = read_json("rooms.json", [])
                    return self._send(200, {"ok": True,
                                            "whitelist": [r["room"] for r in rooms if r.get("enabled", True)]})
                if path == "/api/admins":
                    return self._send(200, {"ok": True, "text": read_text("admins.txt")})
                if path == "/api/persona":
                    return self._send(200, {"ok": True, "text": read_text("persona.txt")})
                if path == "/api/stickers":
                    return self._send(200, {"ok": True, "items": sticker_list()})
                if path == "/api/apikey":
                    return self._send(200, {"ok": True, **apikey_state()})
                if path == "/api/keycap":
                    return self._send(200, {"ok": True, **keycap_status()})
                if path == "/api/log":
                    n = int(q.get("n", "200") or 200)
                    return self._send(200, {"ok": True, "lines": tail(LOG_FILE, max(10, min(n, 2000)))})
                if path.startswith("/sticker/"):
                    return self._serve_sticker(path[len("/sticker/"):])
                return self._send(404, {"ok": False, "error": "没有这个接口"})
            except Exception as e:                              # noqa: BLE001
                return self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        def _serve_sticker(self, name):
            import urllib.parse
            name = urllib.parse.unquote(name)
            import sticker_lib as S
            p = os.path.join(S.LIB, os.path.basename(name))
            if not os.path.exists(p):
                return self._send(404, {"ok": False, "error": "没有这个图"})
            ext = os.path.splitext(p)[1].lower()
            ctype = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")
            with open(p, "rb") as f:
                return self._send(200, f.read(), ctype)

        def do_POST(self):                                      # noqa: N802
            path = self.path.split("?")[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._send(400, {"ok": False, "error": "请求体不是 JSON"})
            try:
                if path == "/api/tuning":
                    changed = TUNE.save(body.get("patch") or {})
                    return self._send(200, {"ok": True, "changed": changed, **TUNE.describe()})
                if path == "/api/rooms":
                    rooms = body.get("rooms")
                    if not isinstance(rooms, list):
                        return self._send(400, {"ok": False, "error": "rooms 要是数组"})
                    rooms, bad = _fix_rooms(rooms)
                    if bad:
                        return self._send(400, {"ok": False, "error": _bad_msg(bad), "bad": bad})
                    write_json("rooms.json", rooms)
                    write_json("whitelist.json", [r["room"] for r in rooms if r.get("enabled", True)])
                    return self._send(200, {"ok": True, "rooms": rooms})
                if path == "/api/whitelist":
                    wl = body.get("whitelist")
                    if not isinstance(wl, list):
                        return self._send(400, {"ok": False, "error": "whitelist 要是数组"})
                    wl, bad = _fix_names(wl)
                    if bad:
                        return self._send(400, {"ok": False, "error": _bad_msg(bad), "bad": bad})
                    # 兼容老接口：把名单里的会话在 rooms.json 里置 enabled，其余置 false
                    rooms = read_json("rooms.json", [])
                    have = {r.get("room") for r in rooms}
                    for rid in wl:
                        if rid not in have:
                            rooms.append({"name": names.display(rid) or rid, "room": rid,
                                          "account": names._account() or "", "enabled": True})
                    for r in rooms:
                        r["enabled"] = r.get("room") in set(wl)
                    write_json("rooms.json", rooms)
                    write_json("whitelist.json", [r["room"] for r in rooms if r.get("enabled", True)])
                    return self._send(200, {"ok": True, "whitelist": wl})
                if path == "/api/admins":
                    text, bad = _fix_admins(body.get("text") or "")
                    if bad:
                        return self._send(400, {"ok": False, "error": _bad_msg(bad), "bad": bad})
                    write_text("admins.txt", text)
                    return self._send(200, {"ok": True})
                if path == "/api/persona":
                    write_text("persona.txt", body.get("text") or "")
                    return self._send(200, {"ok": True, "note": "人设要重启机器人才生效"})
                if path == "/api/send":
                    return self._send(200, api_send(body))
                if path == "/api/mute":
                    return self._send(200, api_mute(body))
                if path == "/api/bot":
                    return self._send(200, api_bot(body))
                if path == "/api/stickers/send":
                    return self._send(200, api_sticker_send(body))
                if path == "/api/stickers/delete":
                    return self._send(200, api_sticker_delete(body))
                if path == "/api/apikey":
                    return self._send(200, api_apikey_set(body))
                if path == "/api/keycap":
                    return self._send(200, keycap_start())
                if path == "/api/apikey/test":
                    return self._send(200, api_apikey_test(body))
                return self._send(404, {"ok": False, "error": "没有这个接口"})
            except Exception as e:                              # noqa: BLE001
                return self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        def log_message(self, fmt, *args):                      # 静音访问日志
            pass

    return Handler


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>wx-chatbot 控制面板</title>
<style>
:root{--bg:#0f1419;--panel:#161c24;--line:#243040;--fg:#dbe4ee;--dim:#8797a8;--acc:#4aa8ff;--ok:#4cd18a;--warn:#ffb454;--bad:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;background:var(--panel)}
header h1{font-size:16px;margin:0;font-weight:600}
header .sp{flex:1}
.badge{padding:2px 9px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.badge.ok{color:var(--ok);border-color:#1e4a34;background:#10241a}
.badge.bad{color:var(--bad);border-color:#4a1e1e;background:#241010}
.badge.warn{color:var(--warn);border-color:#4a3a1e;background:#241d10}
main{display:flex;min-height:calc(100vh - 104px)}
.foot{padding:9px 16px;border-top:1px solid var(--line);background:var(--panel);color:var(--dim);font-size:12px;text-align:center;line-height:1.6}
nav{width:150px;border-right:1px solid var(--line);padding:10px 8px;background:var(--panel)}
nav button{display:block;width:100%;text-align:left;padding:9px 12px;margin:2px 0;border:0;border-radius:8px;background:transparent;color:var(--dim);cursor:pointer;font-size:14px}
nav button:hover{background:#1d2632;color:var(--fg)}
nav button.on{background:#1b2dur;background:#1b2b3d;color:#fff}
section{flex:1;padding:18px 22px;max-width:1080px}
h2{font-size:15px;margin:0 0 14px;color:#fff}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px dashed #1e2836}
.kv:last-child{border:0}
.kv b{font-weight:500;color:var(--dim)}
label{display:block;margin:10px 0 4px;color:var(--dim);font-size:13px}
input[type=text],input[type=number],textarea,select{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:#0d1218;color:var(--fg);font:inherit}
textarea{min-height:120px;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:13px}
button.act{padding:8px 16px;border-radius:8px;border:1px solid #2b4b6d;background:#17324d;color:#cfe6ff;cursor:pointer;font:inherit}
button.act:hover{background:#1d3f61}
button.ghost{padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--dim);cursor:pointer}
button.danger{border-color:#5a2626;background:#2a1414;color:#ffb3b3}
.row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1;min-width:180px}
.stickers{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.sticker{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#0d1218}
.sticker img{width:100%;height:120px;object-fit:contain;background:#0a0e13;display:block}
.sticker .cap{padding:8px;font-size:12px;color:var(--dim);height:96px;overflow:auto}
.sticker .ops{display:flex;gap:6px;padding:0 8px 8px}
.sticker .ops button{flex:1;padding:5px 0;font-size:12px}
pre{background:#0a0e13;border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;max-height:60vh;font-size:12.5px;line-height:1.5}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;background:#1b2430;color:var(--dim);font-size:12px;margin:0 6px 6px 0}
.hint{color:var(--dim);font-size:12.5px;margin:6px 0 0}
.toast{position:fixed;right:18px;bottom:18px;padding:10px 16px;border-radius:10px;background:#1d3f61;color:#fff;opacity:0;transition:.2s;pointer-events:none}
.toast.on{opacity:1}
</style></head>
<body>
<header>
  <h1>wx-chatbot 控制面板</h1>
  <span class="badge" id="badgeBot">机器人：查询中</span>
  <span class="badge" id="badgeHook">hook：查询中</span>
  <span class="sp"></span>
  <span class="badge" id="badgeTime"></span>
</header>
<main>
<nav>
  <button data-tab="overview" class="on">概览</button>
  <button data-tab="tuning">参数</button>
  <button data-tab="lists">名单</button>
  <button data-tab="persona">人设</button>
  <button data-tab="stickers">表情包</button>
  <button data-tab="tools">工具</button>
  <button data-tab="log">日志</button>
</nav>
<section id="view"></section>
</main>
<footer class="foot">遇到 bug 请向开发者反馈<br>QQ：1503382706　B站：本人真的穷</footer>
<div class="toast" id="toast"></div>
<script>
const $ = (s) => document.querySelector(s);
let TAB = "overview", OVERVIEW = null, TUNING = null, ROOMS = [], WHITELIST = [],
    ADMIN_TEXT = "";

function toast(msg, bad) {
  const t = $("#toast"); t.textContent = msg; t.style.background = bad ? "#5a2626" : "#1d3f61";
  t.classList.add("on"); setTimeout(() => t.classList.remove("on"), 2200);
}
async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ok: false, error: "返回不是 JSON"}));
  if (!j.ok) toast(j.error || "失败", true);
  return j;
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])); }
function roomOptions(sel) {
  return ROOMS.map((r) => `<option value="${esc(r.room)}"${r.room === sel ? " selected" : ""}>${esc(r.name || r.room)}</option>`).join("");
}

async function loadOverview() {
  const j = await api("/api/overview");
  if (!j.ok) return;
  OVERVIEW = j;
  const b = $("#badgeBot"), h = $("#badgeHook");
  b.className = "badge " + (j.bot.running ? "ok" : "bad");
  b.textContent = j.bot.running ? `机器人：运行中 (#${j.bot.pid})` : "机器人：没在跑";
  h.className = "badge " + (j.hook.ok ? "ok" : "bad");
  h.textContent = "hook：" + (j.hook.ok ? "正常" : "不通");
  $("#badgeTime").textContent = j.now;
  if (TAB === "overview") render();
}

function render() {
  if (TAB === "overview") return renderOverview();
  if (TAB === "tuning") return renderTuning();
  if (TAB === "lists") return renderLists();
  if (TAB === "persona") return renderPersona();
  if (TAB === "stickers") return renderStickers();
  if (TAB === "tools") return renderTools();
  if (TAB === "log") return renderLog();
}

function renderOverview() {
  const o = OVERVIEW || {};
  const mutes = (o.mutes || []).map((m) => `<span class="tag">${esc(o.rooms?.[m.scope] || m.scope)} 剩 ${m.left}s</span>`).join("") || "<span class='hint'>没有静音</span>";
  const act = (o.active || []).map((a) => `<span class="tag">${esc(o.rooms?.[a.room] || a.room)} ${a.left}s</span>`).join("") || "<span class='hint'>没有活跃窗口</span>";
  $("#view").innerHTML = `
    <h2>概览</h2>
    <div class="card"><div class="grid">
      <div><div class="kv"><b>机器人</b><span>${o.bot?.running ? "运行中" : "停止"}</span></div>
           <div class="kv"><b>pid</b><span>${o.bot?.pid ?? "-"}</span></div>
           <div class="kv"><b>心跳</b><span>${o.bot?.age != null ? o.bot.age + " 秒前" : "-"}</span></div>
           <div class="kv"><b>第几轮</b><span>${o.bot?.round ?? "-"}</span></div></div>
      <div><div class="kv"><b>hook 30001</b><span>${o.hook?.ok ? "正常" : "不通"}</span></div>
           <div class="kv"><b>当前登录</b><span>${esc(o.hook?.wxid || "-")}</span></div>
           <div class="kv"><b>微信进程</b><span>${o.wechat ?? "-"}</span></div>
           <div class="kv"><b>会话数</b><span>${(o.rooms ? Object.keys(o.rooms).length : 0)}</span></div></div>
    </div></div>
    <div class="card"><h2 style="margin-bottom:8px">静音</h2>${mutes}</div>
    <div class="card"><h2 style="margin-bottom:8px">活跃窗口</h2>${act}</div>
    <div class="card"><h2 style="margin-bottom:8px">本窗口回复次数</h2>
      ${Object.entries(o.replies || {}).map(([k, v]) => `<span class="tag">${esc(o.rooms?.[k] || k)} ${v}</span>`).join("") || "<span class='hint'>暂无</span>"}
    </div>
    <div class="card" id="keyCard"><h2 style="margin-bottom:6px">DeepSeek Key</h2>
      <div class="hint" id="keyState">读取中…</div>
      <div class="row" style="margin-top:8px"><div><label>填一个（写进 settings.json，立刻生效）</label>
        <input type="text" id="keyInput" placeholder="sk-..."></div></div>
      <div class="row" style="margin-top:10px"><button class="act" id="keySave">保存</button>
      <button class="ghost" id="keyTest">测试能不能用</button></div>
<div class="row" style="margin-top:10px"><button class="act" id="keyCap">重新抓数据库 key</button></div>
<p class="hint" id="keyCapMsg">换过微信号、重新装过微信、或 keys.json 被清空时点这个。点了会弹 UAC，然后切到微信点开任意聊天。</p>
      <label class="hint" style="display:flex;gap:6px;align-items:center;margin-top:8px">
        <input type="checkbox" id="keyAllowDsh"> 允许读取本机 DeepSeek Harness 凭证（默认关；关掉后只认这里填的 Key 或环境变量）</label>
      <p class="hint" id="keyMsg"></p></div>
  `;
  bindKey();
}

async function bindKey() {
  const ks = await api("/api/apikey");
  if (!ks.ok || !$("#keyState")) return;
  $("#keyState").innerHTML = ks.set
      ? `当前：<b>${esc(ks.masked)}</b>　来源：${esc(ks.source)}`
      : "当前：<b>没有 Key</b>　（聊天会直接报错，先填一个）";
  const cb = $("#keyAllowDsh");
  if (cb) {
    cb.checked = !!ks.allowDsh;
    cb.onchange = async () => {
      const r = await api("/api/apikey", {allowDsh: cb.checked});
      if (r.ok) { toast(cb.checked ? "已允许读取本机 DSH 凭证" : "已关闭：只认这里填的 Key / 环境变量"); bindKey(); }
    };
  }
  $("#keySave").onclick = async () => {
    const r = await api("/api/apikey", {key: $("#keyInput").value});
    if (r.ok) { toast("已保存到 settings.json，下一轮就能用"); bindKey(); }
  };
  $("#keyTest").onclick = async () => {
    $("#keyMsg").textContent = "测试中…";
    const r = await api("/api/apikey/test", {key: $("#keyInput").value});
    $("#keyMsg").textContent = r.ok ? "✅ 能用：" + (r.detail || "").slice(0, 120) : "❌ " + (r.error || "失败");
  };
}

async function renderTuning() {
  const j = await api("/api/tuning");
  if (!j.ok) return;
  TUNING = j;
  const groups = j.groups.map((g) => `
    <div class="card"><h2 style="margin-bottom:6px">${esc(g.title)}</h2>
      ${g.keys.map((k) => {
        const s = j.spec[k], v = j.values[k];
        return `<label title="${esc(s.desc)}">${k} <span class="hint">— ${esc(s.desc)}</span></label>
          <input type="number" step="${s.type === "int" ? "1" : "0.01"}" min="${s.min}" max="${s.max}"
                 data-k="${k}" value="${v}">`;
      }).join("")}
    </div>`).join("");
  $("#view").innerHTML = `<h2>参数 <span class="hint">保存后主循环下一轮生效，不用重启</span></h2>${groups}
    <div class="row"><button class="act" id="saveTuning">保存参数</button>
    <button class="ghost" id="resetTuning">恢复默认</button></div>
    <p class="hint">文件：${esc(j.file)}</p>`;
  $("#saveTuning").onclick = async () => {
    const patch = {};
    document.querySelectorAll("[data-k]").forEach((el) => { patch[el.dataset.k] = el.value; });
    const r = await api("/api/tuning", {patch});
    if (r.ok) { toast("已保存：" + r.changed.join("、")); renderTuning(); }
  };
  $("#resetTuning").onclick = async () => {
    const patch = {};
    Object.keys(j.spec).forEach((k) => { patch[k] = j.spec[k].default; });
    const r = await api("/api/tuning", {patch});
    if (r.ok) { toast("已恢复默认"); renderTuning(); }
  };
}

async function renderLists(refresh = true) {
  if (refresh) {
    const [rj, wj, aj] = await Promise.all([api("/api/rooms"), api("/api/whitelist"), api("/api/admins")]);
    ROOMS = rj.rooms || []; WHITELIST = wj.whitelist || []; ADMIN_TEXT = aj.text || "";
  }
  const roomRows = ROOMS.map((r, i) => `<tr>
      <td><input type="text" data-r="${i}" data-f="name" value="${esc(r.name || "")}"></td>
      <td><input type="text" data-r="${i}" data-f="room" value="${esc(r.room || "")}"></td>
      <td><input type="text" data-r="${i}" data-f="account" value="${esc(r.account || "")}"></td>
      <td style="text-align:center"><input type="checkbox" data-r="${i}" data-f="enabled" ${r.enabled ? "checked" : ""}></td>
      <td><button class="ghost danger" data-del="${i}">删</button></td></tr>`).join("");
  $("#view").innerHTML = `
    <h2>白名单</h2>
    <p class="hint">白名单已经和会话合并：这张表就是白名单，<b>勾了「启用」才会听、才会发</b>；取消勾选等于移除。</p>
    <div class="card"><h2 style="margin-bottom:6px">会话 / 白名单（rooms.json）</h2>
      <table style="width:100%;border-collapse:collapse"><thead><tr>
        <th align="left">名字（备注，只填这里也会被当成它来识别）</th><th align="left">room（id 或名字，可留空）</th><th align="left">account</th><th>启用</th><th></th>
      </tr></thead><tbody>${roomRows}</tbody></table>
      <div class="row" style="margin-top:10px"><button class="ghost" id="addRoom">+ 加一行</button>
      <button class="act" id="saveRooms">保存会话</button></div></div>
    <div class="card"><h2 style="margin-bottom:6px">管理员（admins.txt）</h2>
      <textarea id="admins" style="min-height:90px">${esc(ADMIN_TEXT)}</textarea>
      <p class="hint">一行一个：<b>wxid 或名字都行</b>（名字会在保存时自动换成 wxid），# 后面是注释；只有这里的人能用 /闭嘴 让机器人停机。</p>
      <button class="act" id="saveAdmins">保存管理员</button></div>`;
  // 只重绘本地数据，不要重新拉服务端（否则刚加的空行会被覆盖掉）
  $("#addRoom").onclick = () => { ROOMS.push({name: "", room: "", account: "", enabled: true}); renderLists(false); };
  document.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => { ROOMS.splice(+b.dataset.del, 1); renderLists(false); });
  $("#saveRooms").onclick = async () => {
    document.querySelectorAll("[data-r]").forEach((el) => {
      const i = +el.dataset.r, f = el.dataset.f;
      ROOMS[i][f] = f === "enabled" ? el.checked : el.value.trim();
    });
    ROOMS = ROOMS.filter((r) => r.room || r.name);   // 只填了名字也别丢（m09094）
    const r = await api("/api/rooms", {rooms: ROOMS});
    if (r.ok) { toast("会话已保存"); renderLists(); }
  };
  $("#saveAdmins").onclick = async () => {
    const r = await api("/api/admins", {text: $("#admins").value});
    if (r.ok) toast("管理员已保存（下一轮生效）");
  };
}

async function renderPersona() {
  const j = await api("/api/persona");
  $("#view").innerHTML = `<h2>人设（persona.txt）</h2>
    <div class="card"><textarea id="persona" style="min-height:52vh">${esc(j.text || "")}</textarea>
    <div class="row" style="margin-top:10px"><button class="act" id="savePersona">保存</button>
    <span class="hint">保存后要重启机器人才生效（persona 只在启动时读一次）</span></div></div>`;
  $("#savePersona").onclick = async () => {
    const r = await api("/api/persona", {text: $("#persona").value});
    if (r.ok) toast("已保存，重启机器人后生效");
  };
}

async function renderStickers() {
  const j = await api("/api/stickers");
  const items = j.items || [];
  $("#view").innerHTML = `<h2>表情包（${items.length} 张）</h2>
    <div class="row" style="margin-bottom:12px"><div><label>发到哪个会话</label>
      <select id="stRoom">${roomOptions()}</select></div></div>
    <div class="stickers">${items.map((it) => `
      <div class="sticker"><img src="/sticker/${encodeURIComponent(it.file)}" loading="lazy">
        <div class="cap">${esc(it.what || "")}<br><span class="hint">${esc(it.scene || "")}</span></div>
        <div class="ops"><button class="ghost" data-send="${esc(it.file)}">发这张</button>
        <button class="ghost danger" data-del="${esc(it.file)}">删</button></div></div>`).join("") || "<p class='hint'>库是空的</p>"}
    </div>`;
  document.querySelectorAll("[data-send]").forEach((b) => b.onclick = async () => {
    const r = await api("/api/stickers/send", {file: b.dataset.send, room: $("#stRoom").value});
    if (r.ok) toast("发了：" + b.dataset.send);
  });
  document.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("删掉 " + b.dataset.send + " ？")) return;
    const r = await api("/api/stickers/delete", {file: b.dataset.del});
    if (r.ok) { toast("已删"); renderStickers(); }
  });
}

async function renderTools() {
  const j = await api("/api/overview");
  ROOMS = Object.entries(j.rooms || {}).map(([room, name]) => ({room, name}));
  $("#view").innerHTML = `<h2>工具</h2>
    <div class="card"><h2 style="margin-bottom:6px">发一条消息</h2>
      <div class="row"><div><label>会话</label><select id="tRoom">${roomOptions()}</select></div></div>
      <label>内容</label><input type="text" id="tText" placeholder="测一下通不通">
      <div class="row" style="margin-top:10px"><button class="act" id="tSend">发送</button></div>
      <p class="hint">走 hook 直发（127.0.0.1:30001），不动窗口。</p></div>
    <div class="card"><h2 style="margin-bottom:6px">静音</h2>
      <div class="row"><div><label>会话</label><select id="mRoom">${roomOptions()}</select></div>
      <div style="max-width:140px"><label>分钟</label><input type="number" id="mMin" value="10" min="1" max="1440"></div></div>
      <div class="row" style="margin-top:10px"><button class="act" id="mSet">设静音</button>
      <button class="ghost" id="mClear">全部解除</button></div>
      <p class="hint">群聊静音只停该群；私聊（管理员）静音是全体停、留管理员私聊。</p></div>
    <div class="card"><h2 style="margin-bottom:6px">机器人</h2>
      <div class="row"><button class="act" id="bStart">启动</button>
      <button class="ghost danger" id="bStop">停止</button>
      <button class="ghost" id="bState">刷新状态</button></div>
      <p class="hint" id="bInfo"></p></div>`;
  $("#tSend").onclick = async () => {
    const r = await api("/api/send", {room: $("#tRoom").value, text: $("#tText").value});
    if (r.ok) toast("已发送");
  };
  $("#mSet").onclick = async () => {
    const r = await api("/api/mute", {room: $("#mRoom").value, minutes: +$("#mMin").value});
    if (r.ok) toast("已设静音");
  };
  $("#mClear").onclick = async () => {
    const r = await api("/api/mute", {minutes: 0});
    if (r.ok) toast("已全部解除");
  };
  $("#bStart").onclick = async () => { const r = await api("/api/bot", {action: "start"}); if (r.ok) { toast("已尝试启动"); setTimeout(loadOverview, 3000); } };
  $("#bStop").onclick = async () => { const r = await api("/api/bot", {action: "stop"}); if (r.ok) { toast("已停止"); setTimeout(loadOverview, 1500); } };
  $("#bState").onclick = async () => { const r = await api("/api/bot", {action: "state"}); if (r.ok) $("#bInfo").textContent = JSON.stringify(r.bot); };
}

async function renderLog() {
  const j = await api("/api/log?n=400");
  const lines = (j.lines || []).join("\n");
  $("#view").innerHTML = `<h2>日志（最后 400 行）</h2>
    <div class="card"><pre id="logBox">${esc(lines || "（还没有日志）")}</pre>
    <div class="row"><button class="ghost" id="reLog">刷新</button></div></div>`;
  const box = $("#logBox"); box.scrollTop = box.scrollHeight;
  $("#reLog").onclick = renderLog;
}

document.querySelectorAll("nav button").forEach((b) => b.onclick = async () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  TAB = b.dataset.tab;
  if (TAB === "overview" || TAB === "tools") await loadOverview();
  else render();
});

loadOverview();
setInterval(loadOverview, 5000);
</script>

<script>
(function () {
  const cap = document.getElementById('keyCap');
  if (!cap) return;
  async function poll() {
    const r = await fetch('/api/keycap').then(x => x.json()).catch(() => null);
    const box = document.getElementById('keyCapMsg');
    if (!r || !box) return;
    if (r.running) {
      box.textContent = '抓取中…（' + (r.log || '').split('\n').slice(-2).join(' ') + '）';
      setTimeout(poll, 2000);
    } else if (r.log) {
      box.textContent = '上次抓取：' + (r.log || '').split('\n').slice(-3).join(' ');
    }
  }
  cap.onclick = async () => {
    const box = document.getElementById('keyCapMsg');
    box.textContent = '正在启动（同意 UAC 后切到微信点开任意聊天）…';
    const r = await fetch('/api/keycap', {method: 'POST', body: '{}'}).then(x => x.json()).catch(e => null);
    if (!r || !r.ok) { box.textContent = '启动失败：' + ((r && r.error) || '请求出错'); return; }
    poll();
  };
  poll();
})();
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer((args.host, args.port), make_handler())
    url = "http://%s:%d" % (args.host, args.port)
    print("面板已启动：%s" % url, flush=True)
    print("（只监听本机，不需要登录；Ctrl+C 停止）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
