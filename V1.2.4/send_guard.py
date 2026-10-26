# -*- coding: utf-8 -*-
"""发送（带来源窗口校验）。

发送前逐行点会话列表，用"聊天标题条截图比对"或"目标群未读清零"证明
当前打开的确实是目标群；证明不了就不发（宁可不发，也不发错窗口）。

用法: python send_guard.py <room_wxid> <account> <text>
退出码: 0 成功 / 2 没窗口 / 3 无法确认窗口（拒绝发送）/ 4 发了但没回读确认
        5 粘贴后会话被切走 / 6 不在白名单 / 7 宵禁 / 8 静音 / 9 消息发到了别的会话（必须停机）
环境变量 WX_DRY=1 -> 只验证窗口，不发送
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import warnings
import ctypes.wintypes as wt

warnings.filterwarnings("ignore", category=DeprecationWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "wechat-mcp", "scripts"))
import wx4_db as D  # noqa: E402
import config as __CFG  # noqa: E402  路径配置

# 微信数据目录：settings.json 的 paths.wechat_files / 环境变量 WX_WECHAT_FILES / 默认 ~/Documents/xwechat_files
BASE = __CFG.wechat_files()
FACTS = json.load(open(os.path.join(HERE, "keys.json"), encoding="utf-8-sig"))

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.SetProcessDPIAware()

VK_V, VK_RETURN, VK_CONTROL = 0x56, 0x0D, 0x11
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0042

user32.OpenClipboard.argtypes = [wt.HWND]
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.restype = wt.BOOL
user32.mouse_event.argtypes = [wt.DWORD, ctypes.c_int, ctypes.c_int, wt.DWORD, ctypes.c_size_t]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]

ROW_TOP = 0.125     # 会话列表第一行中心（相对窗口高度）
ROW_PITCH = 0.09    # 行距
# 输入框按「距窗口底部多少像素」定位。别用 0.93H —— 那个点在工具条上，
# 按下去弹的是「选择文件」对话框，文字粘进文件名、回车发不出去，回读也没有，
# 最后一路 rc=4（2026-10-21 踩过，一条都没发出去还以为发出去了）
INPUT_X = 0.55
INPUT_BOTTOM = 125
LIST_X = 0.14

# 标题条裁剪和上面这些比例都假定窗口是下面这个尺寸；窗口被拉过大小就全都偏了
WIN_X, WIN_Y, WIN_W, WIN_H = 20, 20, 1300, 830


def open_db(account):
    info = FACTS[account]
    db = D.Wx4DB()
    ok, err = db.open_account(os.path.join(BASE, info["dir"]), info["key"])
    if not ok:
        raise SystemExit("开库失败: %s" % err)
    return db


def room_unread(db, room):
    s, err = db.get_sessions()
    if err or not s:
        return None
    for r in s:
        if r.get("username") == room:
            return int(r.get("unread_count") or 0)
    return None


def wechat_windows():
    pids = set()
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, encoding="gbk",
                         errors="ignore").stdout
    for line in out.splitlines():
        p = [x.strip('"') for x in line.split('","')]
        if len(p) >= 2 and p[1].isdigit():
            pids.add(int(p[1]))
    res = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, l):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, cls, 256)
            txt = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, txt, 256)
            res.append((h, cls.value, txt.value))
        return True

    user32.EnumWindows(cb, 0)
    return res


def find_main():
    for h, cls, txt in wechat_windows():
        if "TrayIcon" in cls:
            continue
        if "QWindowIcon" in cls and txt.strip() == "微信":
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            if (r.right - r.left) * (r.bottom - r.top) > 400000:
                return h, r
    # 被缩成小条了：恢复一次再找
    for h, cls, txt in wechat_windows():
        if "QWindowIcon" in cls and txt.strip() == "微信":
            user32.ShowWindow(h, 9)
            user32.SetWindowPos(h, wt.HWND(-2), 20, 20, 1300, 830, 0x0040)
            time.sleep(0.4)
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            if (r.right - r.left) * (r.bottom - r.top) > 400000:
                return h, r
            break
    return None, None


def ensure_geometry(h, r):
    """窗口被拉过大小/挪过位置时，先把坐标体系拉回参考值。

    refs/*.png 是 320x32 的标题条裁剪，「窗口比例 + 距底部像素」那套坐标也都是
    按 1300x830 算的。窗口一改比例，标题条就会裁到空白（比对恒不过）、输入框
    会点到工具条上（弹文件选择框），所以先规整再干活。
    """
    w, ht = r.right - r.left, r.bottom - r.top
    if abs(w - WIN_W) <= 20 and abs(ht - WIN_H) <= 20:
        return h, r
    print("窗口 %dx%d 不是参考尺寸 %dx%d，先规整" % (w, ht, WIN_W, WIN_H))
    user32.SetWindowPos(h, wt.HWND(0), WIN_X, WIN_Y, WIN_W, WIN_H, 0x0004 | 0x0040)
    time.sleep(0.7)
    r = wt.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    print("规整后 rect=(%d,%d,%dx%d)" % (r.left, r.top, r.right - r.left, r.bottom - r.top))
    return h, r


def activate(h):
    """把微信真正抢到前台，并且拿到键盘焦点。

    2026-10-21 踩坑：`GetForegroundWindow()` 被一个**不可见**的
    `XamlExplorerHostIslandWindow`（explorer.exe 的 Windows 外壳 XAML 岛，
    rect 0,0,2560x1600、visible=0）占着，微信线程的 hwndActive/hwndFocus
    都是 None —— 于是 keybd_event 的按键全落到别处，粘贴/回车都不生效，
    send_guard 报 rc=4 "sent-but-unverified"（其实一个字都没进输入框）。
    单独 SetForegroundWindow 赢不了它，必须挂线程输入队列。
    """
    me = kernel32.GetCurrentThreadId()
    target = user32.GetWindowThreadProcessId(h, None)
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    try:
        user32.AttachThreadInput(me, target, True)
        if fg_tid and fg_tid != target:
            user32.AttachThreadInput(fg_tid, target, True)
        user32.ShowWindow(h, 9)
        user32.BringWindowToTop(h)
        user32.SetForegroundWindow(h)
        user32.SetFocus(h)
        time.sleep(0.15)
        return user32.GetForegroundWindow() == h
    finally:
        if fg_tid and fg_tid != target:
            user32.AttachThreadInput(fg_tid, target, False)
        user32.AttachThreadInput(me, target, False)


def set_clip(text):
    user32.OpenClipboard(None)
    user32.EmptyClipboard()
    n = (len(text) + 1) * 2
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, n)
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, text.encode("utf-16-le") + b"\x00\x00", n)
    kernel32.GlobalUnlock(h)
    user32.SetClipboardData(CF_UNICODETEXT, h)
    user32.CloseClipboard()


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def header_crop(r):
    from PIL import ImageGrab
    w, ht = r.right - r.left, r.bottom - r.top
    # 只裁标题那一行：再往下就会带进第一条消息气泡，气泡随滚动变，比对全是噪声
    box = (r.left + int(w * 0.22), r.top + int(ht * 0.060),
           r.left + int(w * 0.48), r.top + int(ht * 0.100))
    return ImageGrab.grab(bbox=box).convert("L").resize((320, 32))


def _binary(img):
    """亮字暗底 -> 二值。先二值再比，才不会因为整块背景把差异稀释成 7 分。"""
    return img.point(lambda v: 255 if v > 150 else 0)


def crop_is_blank(img):
    """标题条如果是空白（没字），比对会全部 0 分，等于假阳性，必须挡掉。"""
    from PIL import ImageStat
    st = ImageStat.Stat(img)
    return st.stddev[0] < 8


def ref_path(room):
    d = os.path.join(HERE, "refs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, room.replace("@", "_") + ".png")


def header_diff(r, room):
    """返回"归一化不匹配度"：0 = 完全一样，越大越不像。

    用 mismatch_pixels / ink_ref，而不是平均灰度差——单字标题（如「威」）
    墨点很少，平均差会被背景稀释到 3 分，跟别的会话拉不开（差点又发错）。
    """
    from PIL import Image, ImageChops
    p = ref_path(room)
    if not os.path.exists(p):
        return None
    ref = _binary(Image.open(p).convert("L"))
    if crop_is_blank(ref):
        print("REF_BLANK 参考图是空白，忽略它")
        return None
    cur = header_crop(r)
    if crop_is_blank(cur):
        return None
    if cur.size != ref.size:      # 参考图是旧尺寸就作废，别拿不一样的图硬比
        print("REF_SIZE 参考图尺寸 %s 与当前 %s 不符，忽略" % (ref.size, cur.size))
        return None
    cur = _binary(cur)
    ink_ref = sum(1 for v in ref.getdata() if v)
    if ink_ref <= 0:
        return None
    mismatch = sum(1 for v in ImageChops.difference(ref, cur).getdata() if v)
    return mismatch / float(ink_ref)


def open_room_verified(db, room, r, w, ht):
    """证明当前打开的会话就是目标群，证不出来就不发。

    两种证据，任一成立即可：
      A. 目标群未读数从 >=1 变成 0（点开了它，微信才会清未读）
      B. 聊天标题条截图与参考图 refs/<room>.png 一致（参考图是在人工核对过
         "确实是这个群"时保存的）
      C. 刚在同一个会话里成功发过一两条（WX_TRUST_ROOM / WX_TRUST_UNTIL），
         25 秒内续发第二条不用再证明一次
    """
    if (os.environ.get("WX_TRUST_ROOM") == room
            and time.time() < float(os.environ.get("WX_TRUST_UNTIL", "0") or 0)):
        print("verified 刚在这个会话发过，续发第二条")
        return True

    before = room_unread(db, room)
    has_ref = os.path.exists(ref_path(room))

    # A. 未读清零法（快）：目标会话有未读时，点开它就会清零
    if before is not None and before > 0:
        for row in range(10):
            y = r.top + int(ht * (0.11 + row * 0.018))
            click(r.left + int(w * LIST_X), y)
            time.sleep(0.4)
            if room_unread(db, room) == 0:
                if not has_ref:
                    header_crop(r).save(ref_path(room))
                print("verified 第 %d 次点击 = 目标会话（未读 %d -> 0）" % (row, before))
                return True
        print("ABORT 逐格点都没能让目标未读清零")
        return False

    # B. 标题比对法：未读为 0 时，逐格点会话列表，直到标题条跟参考图对上
    #    行高不固定（有两行预览的会话更高），所以不能按固定行距算，必须扫
    if not has_ref:
        print("ABORT 未读为 0 且没有标题参考图，无法证明当前会话")
        return False
    try:
        known = json.load(open(os.path.join(HERE, "whitelist.json"), encoding="utf-8-sig"))
    except Exception:
        known = []
    others = [x for x in known if x != room and os.path.exists(ref_path(x))]
    for k in range(43):
        y = r.top + int(ht * (0.100 + k * 0.016))
        click(r.left + int(w * LIST_X), y)
        time.sleep(0.32)
        me = header_diff(r, room)
        if me is None or me >= 0.30:
            continue
        # 还要确认它比"别的会话"明显更像——单字标题（威）天然容易糊，靠这个兜住
        rival = 9.9
        for other in others:
            d2 = header_diff(r, other)
            if d2 is not None and d2 < rival:
                rival = d2
        if rival >= 9.9 or rival - me > 0.5:
            print("verified 第 %d 格 = 目标会话（不匹配度 %.2f，次像 %.2f）" % (k, me, rival))
            return True
    print("ABORT 扫了一遍会话列表都没对上目标会话（需 不匹配度<0.30 且比次像低 0.5）")
    print("提示：微信偶尔会排版卡死——窗口外面一圈空白/白底，界面不跟着重排。"
          "这时候手动把窗口右下角拖一下就能恢复。")
    return False


WHITELIST_FILE = os.path.join(HERE, "whitelist.json")


def in_whitelist(room):
    """白名单 = rooms.json 里 enabled=true 的会话（2026-10-25 m09032 合并）。

    whitelist.json 现在只是镜像，读它作兜底（老部署没 rooms.json 时也能用）。
    """
    rooms_file = os.path.join(HERE, "rooms.json")
    try:
        with open(rooms_file, encoding="utf-8-sig") as f:
            rooms = json.load(f)
        return any(r.get("room") == room and r.get("enabled", True) for r in rooms)
    except FileNotFoundError:
        try:
            with open(WHITELIST_FILE, encoding="utf-8") as f:
                return room in json.load(f)
        except FileNotFoundError:
            return False


def _plain(content):
    """微信有时把普通文本也压成 hex(zstd)，比对前必须先还原，否则长文本永远 rc=4。"""
    c = str(content or "")
    if c[:8].lower() == "28b52ffd":
        try:
            import zstandard
            c = zstandard.ZstdDecompressor().decompressobj().decompress(
                bytes.fromhex(c)).decode("utf-8", "ignore")
        except Exception:
            pass
    return c


def main():
    room, account, text = sys.argv[1], sys.argv[2], sys.argv[3]

    if not in_whitelist(room):
        print("ABORT %s 不在白名单 whitelist.json 里" % room)
        return 6

    sys.path.insert(0, HERE)
    import wx_chat_loop as _L
    if _L.in_curfew() and os.environ.get("WX_BYPASS_CURFEW") != "1":
        print("ABORT 宵禁时段，不发任何消息")
        return 7
    if os.environ.get("WX_BYPASS_MUTE") != "1" and _L.mute_left(room) > 0:
        print("ABORT 被 /闭嘴 静音中（这个会话），还剩 %d 秒" % int(_L.mute_left(room)))
        return 8

    # 优先走 DLL hook 的 HTTP 通道：不动窗口、不动鼠标、不抢前台
    # （WX_SEND_BACKEND=hook 强制只走 hook；=gui 强制走界面）
    backend = os.environ.get("WX_SEND_BACKEND", "auto")
    if backend in ("auto", "hook"):
        sys.path.insert(0, HERE)
        import hook_send
        if hook_send.available():
            ok, info = hook_send.send_text(room, text)
            if ok:
                dbh = open_db(account)
                for _ in range(10):
                    time.sleep(0.3)
                    msgs, _ = dbh.get_messages(room, limit=1, offset=0)
                    if msgs:
                        m = msgs[0]
                        if (m.get("sender_username") or account) == account and \
                                text[:12] in _plain(m.get("message_content")):
                            dbh.close()
                            print("sent (hook)")
                            return 0
                dbh.close()
                print("sent-but-unverified (hook)")
                return 4
            print("hook 发送失败（%r），回落到界面发送" % (info,))
            if backend == "hook":
                return 3
        elif backend == "hook":
            print("ABORT hook API 没响应（30001 不通）")
            return 2

    db = open_db(account)
    h, r = find_main()
    if not h:
        print("no window")
        return 2
    h, r = ensure_geometry(h, r)
    w, ht = r.right - r.left, r.bottom - r.top

    if not user32.IsWindowVisible(h):
        user32.ShowWindow(h, 9)
        time.sleep(0.2)
    user32.SetWindowPos(h, wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002)  # 临时置顶
    if not activate(h):
        print("WARN 前台抢不到，重试一次")
        activate(h)
    time.sleep(0.25)
    if user32.GetForegroundWindow() != h:
        db.close()
        print("ABORT 微信拿不到前台（键盘事件会打到别处），不发")
        return 3

    if not open_room_verified(db, room, r, w, ht):
        db.close()
        return 3

    if os.environ.get("WX_DRY") == "1":
        print("dry-run 只验证窗口，不发送")
        db.close()
        return 0

    click(r.left + int(w * INPUT_X), r.bottom - INPUT_BOTTOM)
    time.sleep(0.12)
    # 贴之前先清空输入框：微信按会话保留草稿，不清就会把旧草稿和新消息拼成一条
    # （2026-10-21 群里出过「定位测试CCC我在这儿呢…」这种拼接）
    for vk, up in ((VK_CONTROL, 0), (0x41, 0), (0x41, 2), (VK_CONTROL, 2)):
        user32.keybd_event(vk, 0, up, 0)
    time.sleep(0.1)
    user32.keybd_event(0x2E, 0, 0, 0)
    user32.keybd_event(0x2E, 0, 2, 0)
    time.sleep(0.1)
    set_clip(text)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 2, 0)
    user32.keybd_event(VK_CONTROL, 0, 2, 0)
    time.sleep(0.25)

    # 回车前再确认一次：这期间如果有人切了会话，就不按回车，并清空输入框
    if user32.GetForegroundWindow() != h and not activate(h):
        db.close()
        print("ABORT 粘贴后丢了前台焦点，不按回车")
        return 5
    d2 = header_diff(r, room)
    if d2 is not None and d2 >= 0.30:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(0x41, 0, 0, 0)
        user32.keybd_event(0x41, 0, 2, 0)
        user32.keybd_event(VK_CONTROL, 0, 2, 0)
        user32.keybd_event(0x2E, 0, 0, 0)
        user32.keybd_event(0x2E, 0, 2, 0)
        db.close()
        print("ABORT 粘贴后会话被切换（不匹配度 %.2f），已清空输入框" % d2)
        return 5

    user32.keybd_event(VK_RETURN, 0, 0, 0)
    user32.keybd_event(VK_RETURN, 0, 2, 0)

    for _ in range(12):
        time.sleep(0.4)
        # 先看目标会话有没有新消息
        msgs, err = db.get_messages(room, limit=1, offset=0)
        if msgs:
            m = msgs[0]
            if (m.get("sender_username") or account) == account and \
                    text[:12] in _plain(m.get("message_content")):
                db.close()
                print("sent")
                return 0
        # 再看别的会话——文字跑到别处 = 发错窗口，必须停机
        s, _ = db.get_sessions()
        for row in (s or [])[:5]:
            u = row.get("username")
            if u == room or not u:
                continue
            other, _ = db.get_messages(u, limit=1, offset=0)
            if other and text[:12] in _plain(other[0].get("message_content")):
                db.close()
                print("LEAK 文字落到 %s，不是目标 %s —— 立刻停机" % (u, room))
                return 9
    db.close()
    print("sent-but-unverified")
    return 4


if __name__ == "__main__":
    sys.exit(main())
