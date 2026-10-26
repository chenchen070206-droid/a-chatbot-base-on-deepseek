# -*- coding: utf-8 -*-
"""vision_watch.py —— 只识图，不回复。

盯 rooms.json 里的会话，把**新收到**的图片 / 表情动图识成一句文字，
打进日志（默认 vision.log）。不判断要不要接、不发消息，纯粹用来看识图效果。

用法:
  python vision_watch.py                 # 只看新消息
  python vision_watch.py --backfill 3    # 启动时额外把最近 3 条图片消息也识一遍
  python vision_watch.py --rooms rooms.json --gap 4
"""
import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_chat_loop as L  # noqa: E402
import wx_vision as V  # noqa: E402


def visual_kind(msg):
    lt = str(msg.get("local_type"))
    if lt == "3":
        return "图片"
    xml = V.unpack_content(msg.get("message_content"))
    if "<appmsg" in xml and "<type>8</type>" in xml.replace(" ", ""):
        return "表情"
    return None


def stamp(ts):
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rooms", default=os.path.join(HERE, "rooms.json"))
    ap.add_argument("--gap", type=float, default=4.0)
    ap.add_argument("--backfill", type=int, default=0, help="启动时补识最近 N 条图片/表情")
    args = ap.parse_args()

    rooms = [r for r in json.load(open(args.rooms, encoding="utf-8-sig")) if r.get("enabled", True)]
    dbs, names = {}, {}
    seen = set()

    for r in rooms:
        acct = r["account"]
        if acct not in dbs:
            dbs[acct] = L.open_db(acct)
            names[acct] = L.my_name(dbs[acct], acct) or ""
        try:
            _, msgs = L.transcript(dbs[acct], r["room"], acct)
        except Exception as e:
            print("读失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
            continue
        todo = []
        for m in msgs:
            k = (r["room"], m.get("create_time"), str(m.get("local_type")))
            if k in seen:
                continue
            seen.add(k)
            if visual_kind(m):
                todo.append(m)
        if args.backfill:
            for m in todo[:args.backfill]:
                _describe(r, acct, m)
        print("就位: %s（%d 条历史图片已登记，只处理新消息）" % (r["name"], len(todo)), flush=True)

    print("=== 开始等新图片（不回复）%s ===" % datetime.datetime.now().strftime("%H:%M:%S"), flush=True)
    while True:
        for r in rooms:
            acct = r["account"]
            db = dbs.get(acct)
            if not db:
                continue
            try:
                _, msgs = L.transcript(db, r["room"], acct)
            except Exception as e:
                print("  读失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
                continue
            for m in reversed(msgs):          # 旧 -> 新
                k = (r["room"], m.get("create_time"), str(m.get("local_type")))
                if k in seen:
                    continue
                seen.add(k)
                if visual_kind(m):
                    _describe(r, acct, m)
        time.sleep(max(args.gap, 1.0))


def _describe(room_cfg, acct, msg):
    kind = visual_kind(msg)
    when = msg.get("create_time")
    print("[%s] %s 收到%s → 识图中…" % (stamp(when), room_cfg["name"], kind), flush=True)
    t0 = time.time()
    try:
        desc = V.describe_message(msg, acct, room_cfg["room"])
    except Exception as e:
        print("    失败: %s" % str(e)[:120], flush=True)
        return
    dt = time.time() - t0
    if desc:
        print("    描述(%.1fs): %s" % (dt, desc), flush=True)
        print("    像我=%s" % V.is_self(desc), flush=True)
    else:
        print("    没识别出内容(%.1fs)" % dt, flush=True)


if __name__ == "__main__":
    main()
