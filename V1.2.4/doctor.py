# -*- coding: utf-8 -*-
"""自检：接手的人（或下一条鱼）先跑这个，不要一上来就发消息。

    python doctor.py

全部只读：不开窗口、不点鼠标、不发消息。
"""
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "wechat-mcp", "scripts"))

OK, WARN, BAD = "[ OK ]", "[WARN]", "[FAIL]"
problems = []


def say(tag, msg):
    print("%s %s" % (tag, msg))
    if tag == BAD:
        problems.append(msg)


def check_python():
    v = sys.version_info
    say(OK if v >= (3, 10) else BAD, "Python %d.%d.%d (%s)" % (v[0], v[1], v[2], sys.executable))
    try:
        import PIL  # noqa: F401
        say(OK, "Pillow 已安装")
    except Exception:
        say(BAD, "Pillow 缺失：pip install pillow")


def check_files():
    need = ["wx_chat_loop.py", "wx_multi.py", "send_guard.py", "persona.txt",
            "settings.json", "rooms.json", "whitelist.json", "keys.json"]
    for f in need:
        say(OK if os.path.exists(os.path.join(HERE, f)) else BAD, "文件 %s" % f)
    dlls = ["wx_key.dll", "wcdb_api.dll", "WCDB.dll", "SDL2.dll"]
    for d in dlls:
        p = os.path.join(HERE, "wechat-mcp", "bin", d)
        say(OK if os.path.exists(p) else BAD, "DLL %s" % d)


def check_wechat():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe"],
                             capture_output=True, text=True, encoding="gbk", errors="ignore").stdout
        n = out.count("Weixin.exe")
        say(OK if n else BAD, "微信进程数 %d（要 ≥1）" % n)
    except Exception as e:
        say(WARN, "列进程失败：%s" % e)
    try:
        import send_guard as G
        h, r = G.find_main()
        if h:
            say(OK, "微信主窗口 hwnd=%s，位置 (%d,%d) 尺寸 %dx%d"
                % (h, r.left, r.top, r.right - r.left, r.bottom - r.top))
            if not G.user32.IsWindowVisible(h):
                say(WARN, "主窗口当前不可见（可能在托盘），发送时脚本会自动 ShowWindow")
        else:
            say(BAD, "找不到微信主窗口（class 含 QWindowIcon、标题「微信」、面积>40万）")
    except Exception as e:
        say(BAD, "窗口探测失败：%s" % e)


def check_db():
    try:
        import wx_chat_loop as L
    except Exception as e:
        say(BAD, "import wx_chat_loop 失败：%s" % e)
        return
    facts = json.load(open(os.path.join(HERE, "keys.json"), encoding="utf-8-sig"))
    rooms = json.load(open(os.path.join(HERE, "rooms.json"), encoding="utf-8-sig"))
    accounts = sorted({r["account"] for r in rooms if r.get("enabled", True)})
    for acct in accounts:
        try:
            db = L.open_db(acct)
            s, _ = db.get_sessions()
            say(OK, "%s 开库成功，会话 %d 个" % (acct, len(s or [])))
            names = {x.get("username") for x in (s or [])}
            for r in rooms:
                if r.get("account") != acct:
                    continue
                if r["room"] in names:
                    rows, _ = L.transcript(db, r["room"], acct, 1)
                    tail = (rows[-1][2][:30] if rows else "(空)")
                    say(OK, "会话在列表里：%s → %s" % (r["name"], tail))
                else:
                    say(WARN, "会话 %s(%s) 不在列表里，可能被删/改名" % (r["name"], r["room"]))
            db.close()
        except SystemExit as e:
            say(BAD, "%s 开库失败：%s（密钥过期，见 README 第六节）" % (acct, e))
        except Exception as e:
            say(BAD, "%s 开库异常：%s" % (acct, e))


def check_refs():
    try:
        from PIL import Image, ImageStat
    except Exception:
        return
    rooms = [r["room"] for r in json.load(open(os.path.join(HERE, "rooms.json"), encoding="utf-8-sig"))
             if r.get("enabled", True)]
    for room in rooms:
        p = os.path.join(HERE, "refs", room.replace("@", "_") + ".png")
        if not os.path.exists(p):
            say(WARN, "%s 没有标题参考图（未读为 0 时会拒绝发送；先人工点开该会话存一张）" % room)
            continue
        try:
            img = Image.open(p).convert("L")
            std = ImageStat.Stat(img).stddev[0]
            size_ok = img.size == (320, 32)   # send_guard.header_crop() 就是 resize 到 320x32
            tag = OK if (std >= 8 and size_ok) else WARN
            say(tag, "%s 参考图 %s 尺寸%s 对比度%.1f"
                % (room, img.size, "正确" if size_ok else "应为(320,32)", std))
        except Exception as e:
            say(WARN, "%s 参考图读不出：%s" % (room, e))


def check_state():
    try:
        import wx_chat_loop as L
    except Exception:
        return
    say(OK if not L.in_curfew() else WARN, "宵禁状态：%s"
        % ("正在宵禁（不读不回）" if L.in_curfew() else "不在宵禁"))
    left = int(L.mute_left())
    say(OK if left == 0 else WARN, "静音状态：%s" % ("无" if left == 0 else "还剩 %d 秒" % left))
    try:
        wl = set(json.load(open(os.path.join(HERE, "whitelist.json"), encoding="utf-8-sig")))
        rooms = {r["room"] for r in json.load(open(os.path.join(HERE, "rooms.json"), encoding="utf-8-sig"))
                 if r.get("enabled", True)}
        missing = rooms - wl
        say(OK if not missing else BAD, "白名单覆盖：%s" % ("全部覆盖" if not missing else "缺 %s" % missing))
    except Exception as e:
        say(WARN, "白名单检查失败：%s" % e)


def check_running():
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -like '*wx_multi.py*' } | "
              "Select-Object -ExpandProperty ProcessId")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, encoding="utf-8", errors="ignore").stdout
        pids = [x.strip() for x in out.splitlines() if x.strip().isdigit()]
        say(OK if pids else WARN,
            "wx_multi 正在运行 pid=%s" % ",".join(pids) if pids else "wx_multi 没在跑（需要就手动启动）")
    except Exception:
        say(WARN, "查不到运行状态")


def main():
    print("=" * 60)
    print("wc-chatbot 自检  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("工作目录:", HERE)
    print("=" * 60)
    print("\n-- 环境 --")
    check_python()
    print("\n-- 文件 --")
    check_files()
    print("\n-- 微信 --")
    check_wechat()
    print("\n-- 数据库 --")
    check_db()
    print("\n-- 参考图 --")
    check_refs()
    print("\n-- 运行状态 --")
    check_state()
    check_running()
    print("\n" + "=" * 60)
    if problems:
        print("有 %d 个必须先解决的问题：" % len(problems))
        for p in problems:
            print("  -", p)
    else:
        print("自检通过，可以跑 wx_multi.py 了（建议先 WX_DRY=1 干跑一次）")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
