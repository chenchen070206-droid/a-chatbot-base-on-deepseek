#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""broadcast.py —— 向白名单内所有会话群发同一段文字。

用法：
    python broadcast.py --text "图像识别已接入"
    python broadcast.py -f 通知.txt --gap 8
    python broadcast.py -t "测试" --dry                  # 只看会发给谁
    python broadcast.py -t "测试" --only 好友A,威          # 只发指定会话
    python broadcast.py -t "测试" --no-pause              # 不停主循环直接发

默认行为：
  1. 读 rooms.json（enabled=true）∩ whitelist.json，得到目标会话
  2. 有 wx_multi.py 在跑就先停掉（它会在同一窗口点来点去，抢焦点）
  3. 逐个调 send_guard.py（发完回读数据库确认，rc=0 才算真发出去）
  4. 全部发完再把主循环拉起来，日志写 run_bb_<时分秒>.log

退出码：0 = 全部成功；1 = 有失败；2 = 参数/环境问题。
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SEND = os.path.join(HERE, "send_guard.py")
BOT = os.path.join(HERE, "wx_multi.py")
ROOMS = os.path.join(HERE, "rooms.json")
WHITELIST = os.path.join(HERE, "whitelist.json")

RC_DESC = {
    0: "sent",
    2: "目标会话没找到",
    3: "微信拿不到前台",
    4: "发出去了但没核实",
    5: "回车前前台丢了",
    6: "不在白名单",
    7: "宵禁时段",
    8: "被 /闭嘴 静音",
    9: "排版卡死/窗口异常",
}
RC_MARKS = {0: "OK", 2: "MISS", 3: "FOCUS", 4: "UNVERIFIED",
            5: "FOCUS", 6: "WL", 7: "CURFEW", 8: "MUTED", 9: "STUCK"}


def load_targets(only=None):
    """rooms.json 里 enabled=true 的会话（白名单已与 rooms 合并，2026-10-25 m09032）。"""
    try:
        rooms = json.load(open(ROOMS, encoding="utf-8-sig"))
    except Exception as e:
        print("读 rooms.json 失败: %s" % e)
        return []
    out = []
    for r in rooms:
        name, room = r.get("name") or r["room"], r["room"]
        if not r.get("enabled", True):
            print("跳过 %s：rooms.json 里 enabled=false" % name)
            continue
        if only and (name not in only and room not in only):
            continue
        out.append({"name": name, "room": room, "account": r["account"]})
    return out


def bot_pids():
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*wx_multi.py*' } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    return [int(x) for x in (p.stdout or "").split() if x.strip().isdigit()]


def stop_bot():
    pids = bot_pids()
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, text=True)
    if pids:
        time.sleep(2)
    return pids


def start_bot():
    stamp = datetime.datetime.now().strftime("%H%M%S")
    log = os.path.join(HERE, "run_bb_%s.log" % stamp)
    fh = open(log, "a", encoding="utf-8", errors="ignore")
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | NEW_PROCESS_GROUP
    p = subprocess.Popen(
        [sys.executable, BOT, "--rooms", "rooms.json",
         "--rounds", "999999", "--gap", "2"],
        cwd=HERE, stdout=fh, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=flags)
    return p.pid, log


def send_one(room, account, text, timeout=240, force_mute=False):
    env = dict(os.environ)
    if force_mute:
        env["WX_BYPASS_MUTE"] = "1"
    try:
        p = subprocess.run([sys.executable, SEND, room, account, text],
                           capture_output=True, text=True, env=env,
                           encoding="utf-8", errors="ignore", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 99, "超时（>%ds）" % timeout
    lines = [(p.stdout or "").strip(), (p.stderr or "").strip()]
    body = "\n".join(x for x in lines if x)
    # 发送器真正的结论行才是有用的（PIL/DeprecationWarning 会混在最后一行）
    marks = [ln.strip() for ln in body.splitlines()
             if ("verified" in ln or "sent" in ln or ln.strip().startswith("ABORT"))]
    tail = marks[-1] if marks else (body.splitlines()[-1] if body else "")
    return p.returncode, tail[:90]


def main():
    ap = argparse.ArgumentParser(description="向白名单内所有会话群发同一段文字")
    ap.add_argument("-t", "--text", help="要发的内容")
    ap.add_argument("-f", "--file", help="从文件读内容（UTF-8）")
    ap.add_argument("--only", help="只发这些（逗号分隔，名字或 room id）")
    ap.add_argument("--gap", type=float, default=6.0, help="每条之间等几秒（默认 6）")
    ap.add_argument("--dry", action="store_true", help="只打印目标，不发")
    ap.add_argument("--no-pause", action="store_true", help="不停主循环")
    ap.add_argument("--no-restart", action="store_true", help="发完不重启主循环")
    ap.add_argument("--timeout", type=int, default=240, help="单条超时秒数")
    ap.add_argument("--force-mute", action="store_true",
                    help="无视 /闭嘴 静音强行发（手动群发时用）")
    ap.add_argument("--no-stamp", action="store_true",
                    help="不加时间戳前缀（默认会在开头加【MM-DD HH:MM】）")
    args = ap.parse_args()

    if args.file:
        text = open(args.file, encoding="utf-8").read().strip()
    else:
        text = (args.text or "").strip()
    if not text and not args.dry:
        print("没有内容：用 -t \"文字\" 或 -f 文件")
        return 2
    if text and not args.no_stamp:
        text = "【%s】%s" % (time.strftime("%m-%d %H:%M"), text)

    only = set(x.strip() for x in args.only.split(",")) if args.only else None
    targets = load_targets(only)
    if not targets:
        print("没有目标会话")
        return 2

    print("内容：%s" % (text if text else "(空)"))
    print("目标（%d 个）：" % len(targets))
    for t in targets:
        print("  - %s  %s" % (t["name"], t["room"]))
    if args.dry:
        print("\n--dry：到此为止，没有发送。")
        return 0

    pids = []
    if not args.no_pause:
        pids = stop_bot()
        if pids:
            print("\n已暂停主循环：pid %s" % ", ".join(map(str, pids)))
        else:
            print("\n主循环没在跑，直接发")

    results = []
    for idx, t in enumerate(targets, 1):
        print("\n[%d/%d] %s ..." % (idx, len(targets), t["name"]), flush=True)
        t0 = time.time()
        rc, tail = send_one(t["room"], t["account"], text, args.timeout,
                            force_mute=args.force_mute)
        dt = time.time() - t0
        mark = RC_MARKS.get(rc, "rc%s" % rc)
        print("    rc=%s %s (%.1fs) %s" % (rc, mark, dt, tail), flush=True)
        results.append({"name": t["name"], "room": t["room"], "rc": rc,
                        "mark": mark, "tail": tail, "sec": round(dt, 1)})
        if idx < len(targets) and args.gap > 0:
            time.sleep(args.gap)

    ok = sum(1 for r in results if r["rc"] == 0)
    print("\n===== 结果 %d/%d 成功 =====" % (ok, len(results)))
    for r in results:
        print("  %-22s rc=%-2s %-11s %5.1fs" % (r["name"], r["rc"], r["mark"], r["sec"]))

    if pids and not args.no_restart:
        pid, log = start_bot()
        print("\n主循环已重启：pid %s，日志 %s" % (pid, os.path.basename(log)))

    if ok == len(results):
        return 0
    failed = [r for r in results if r["rc"] != 0]
    print("\n失败明细：")
    for r in failed:
        print("  %s rc=%s %s — %s" % (r["name"], r["rc"],
                                      RC_DESC.get(r["rc"], "未知"), r["tail"]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
