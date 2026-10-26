# -*- coding: utf-8 -*-
"""wx_image.py —— 直接读微信 4.x 的图片文件（不走截屏）。

原理：
  msg\\attach\\<会话hash>\\YYYY-MM\\Img\\<名字>.dat 是加密容器
    [6B magic 07 08 56 32 08 07][4B aes_size LE][4B xor_size LE][1B 0x01]
    [aes_size 字节 AES-128-ECB][中间明文][末尾 xor_size 字节 单字节 XOR]
  缩略图 `<名字>_t.dat` 解出来直接是 JPEG；
  原图 `<名字>.dat` 解出来常是 `wxgf` 外壳（内部 HEVC），要用 ffmpeg 转一帧。

密钥：
  AES key 是 16 字节，XOR key 是 1 字节，都只存在于**微信进程内存**里。
  `find_image_key.py`（wechat-mcp 里那份）扫描 Weixin.exe 内存，拿一个 .dat 的
  首块密文去试解、认出 JPEG/PNG 魔数就算命中。
  本机 2026-10-21 实测：AES=`78b3bbcf3e769a0d`，XOR=0xd6（记在 keys.json 的 imageKeys 里）。

用法：
    python wx_image.py <消息md5或dat文件名> [...]        # 解到 decoded/
    python wx_image.py --room wxid_OWNER --last 2   # 最近两张
    python wx_image.py --find-key                       # 重新扫内存找 key
"""
import argparse
import glob
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

from Crypto.Cipher import AES

import config as __CFG

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "decoded")
KEYS_FILE = os.path.join(HERE, "keys.json")


def _default_attach():
    """从 keys.json 里第一个带 dir 的账号推出 msg/attach 目录；推不出来就用占位名。"""
    try:
        with open(KEYS_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in cfg.items():
            if k != "imageKeys" and isinstance(v, dict) and v.get("dir"):
                return os.path.join(__CFG.wechat_files(), v["dir"], "msg", "attach")
    except Exception:
        pass
    return os.path.join(__CFG.wechat_files(), "wxid_xxxxxxxxxxxxxxxx_0000", "msg", "attach")


ATTACH = os.environ.get("WX_ATTACH") or _default_attach()   # 可配置：settings.json 的 paths.wechat_files
MAGIC_V2 = b"\x07\x08V2\x08\x07"
MAGIC_V1 = b"\x07\x08V1\x08\x07"
SIGS = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"GIF8", ".gif"), (b"RIFF", ".webp"))
FFMPEG = __CFG.ffmpeg()              # 可配置：settings.json 的 paths.ffmpeg（见 config.py），找不到则为空串


def load_image_keys(account=None):
    cfg = json.load(open(KEYS_FILE, encoding="utf-8-sig"))
    keys = cfg.get("imageKeys") or {}
    if account and account in keys:
        return keys[account]["aes"], keys[account]["xor"]
    if "default" in keys:
        return keys["default"]["aes"], keys["default"]["xor"]
    raise SystemExit("keys.json 里没有 imageKeys，先跑 --find-key")


def save_image_keys(aes, xor, account="default"):
    cfg = json.load(open(KEYS_FILE, encoding="utf-8-sig"))
    cfg.setdefault("imageKeys", {})[account] = {"aes": aes, "xor": xor}
    json.dump(cfg, open(KEYS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def account_of_path(path):
    """从 ...\\xwechat_files\\wxid_xxx_b266\\... 里挑出 wxid（去掉 _b266 这类目录后缀）。"""
    import re
    for part in os.path.normpath(path).split(os.sep):
        m = re.match(r"^(wxid_[0-9a-zA-Z]+)(?:_[0-9a-zA-Z]{4})?$", part)
        if m:
            return m.group(1)
    return None


def dll_image_keys(timeout=10):
    """用项目自带的 wx_key.dll 取图片密钥（GetImageKey 直接返回 JSON）。

    返回 {wxid: {"aes": str, "xor": int}}。
    这是 wechat-mcp 的正路：wx_key.dll 导出 InitializeHook / GetImageKey，
    注入微信进程后 GetImageKey(buf, len) 输出：
      {"accounts":[{"wxid":"...","keys":[{"code":..,"xorKey":214,"aesKey":"..."}]}]}
    """
    dll_path = os.path.join(HERE, "wechat-mcp", "bin", "wx_key.dll")
    if not os.path.isfile(dll_path):
        raise RuntimeError("找不到 %s" % dll_path)
    import ctypes
    sys.path.insert(0, os.path.join(HERE, "wechat-mcp", "scripts"))
    import wx4_common as C

    dll = ctypes.WinDLL(dll_path)
    dll.InitializeHook.argtypes = [ctypes.c_uint32]
    dll.InitializeHook.restype = ctypes.c_bool
    dll.GetImageKey.argtypes = [ctypes.c_char_p, ctypes.c_int]
    dll.GetImageKey.restype = ctypes.c_bool
    dll.CleanupHook.argtypes = []
    dll.CleanupHook.restype = ctypes.c_bool

    pid = C.find_wechat_pid()
    if not pid:
        raise RuntimeError("没找到运行中的微信进程")
    if not dll.InitializeHook(pid):
        raise RuntimeError("InitializeHook 失败(pid=%s)" % pid)
    buf = ctypes.create_string_buffer(4096)
    try:
        for _ in range(int(timeout * 2)):
            if dll.GetImageKey(buf, 4096):
                data = json.loads(buf.value.decode("utf-8", "ignore"))
                out = {}
                for acc in data.get("accounts", []):
                    for k in acc.get("keys", []):
                        out[acc["wxid"]] = {"aes": k["aesKey"], "xor": int(k["xorKey"])}
                return out
            time.sleep(0.5)
    finally:
        dll.CleanupHook()
    raise RuntimeError("GetImageKey 超时（%ss）" % timeout)


def sig_of(buf):
    for sig, ext in SIGS:
        if buf[:len(sig)] == sig:
            return ext
    return None


def decrypt(path, aes_key, xor_key):
    data = open(path, "rb").read()
    if data[:6] not in (MAGIC_V2, MAGIC_V1):
        return data  # 没有容器头，按明文处理
    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    aligned = aes_size + (-aes_size % 16)
    body = data[15:]
    dec = b""
    if aligned:
        dec = AES.new(aes_key, AES.MODE_ECB).decrypt(body[:aligned])
    rest = body[aligned:]
    tail_len = max(0, min(xor_size, len(rest)))
    raw = rest[:len(rest) - tail_len]
    tail = bytes(b ^ xor_key for b in rest[len(rest) - tail_len:]) if tail_len else b""
    return dec + raw + tail


def to_image(buf, out_base):
    """把解密结果写成图片文件；wxgf 走 ffmpeg 取一帧。返回路径或 None。"""
    ext = sig_of(buf)
    if ext:
        if ext == ".jpg":
            eoi = buf.rfind(b"\xff\xd9")
            if eoi > 0:
                buf = buf[:eoi + 2]
        out = out_base + ext
        open(out, "wb").write(buf)
        return out
    if buf[:4] == b"wxgf":
        if not os.path.isfile(FFMPEG):
            return None
        tmp = out_base + ".hevc"
        open(tmp, "wb").write(buf)
        out = out_base + ".jpg"
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", tmp,
                            "-frames:v", "1", "-f", "image2", out],
                           capture_output=True, timeout=120)
        os.remove(tmp) if os.path.exists(tmp) else None
        if r.returncode == 0 and os.path.isfile(out):
            return out
        print("ffmpeg 失败:", r.stderr.decode("utf-8", "ignore")[:200], file=sys.stderr)
        return None
    return None


def find_dats(md5_or_name):
    pats = [os.path.join(ATTACH, "**", md5_or_name + "*"), os.path.join(ATTACH, "**", "*" + md5_or_name + "*")]
    hits = []
    for p in pats:
        hits += glob.glob(p, recursive=True)
    return sorted(set(h for h in hits if h.endswith(".dat")), key=os.path.getsize, reverse=True)


def latest_dats(n=1):
    files = glob.glob(os.path.join(ATTACH, "**", "Img", "*.dat"), recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    seen, out = set(), []
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        key = base[:-2] if base.endswith("_t") else base
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
        if len(out) >= n:
            break
    return out


def find_key(account=None):
    """取图片密钥并写进 keys.json。

    首选项目自带的 wx_key.dll（`GetImageKey`，一次拿全部账号）；
    失败再退回扫微信进程内存（wechat-mcp/find_image_key.py）。
    """
    try:
        keys = dll_image_keys()
        for wxid, k in keys.items():
            save_image_keys(k["aes"], k["xor"], wxid)
        print("dll 取到 %d 个账号的图片密钥: %s" % (len(keys), ", ".join(sorted(keys))))
        return keys
    except Exception as e:
        print("wx_key.dll 取密钥失败（%s），改走内存扫描" % e, file=sys.stderr)

    sys.path.insert(0, os.path.join(HERE, "wechat-mcp"))
    import find_image_key as F
    dats = [d for d in glob.glob(os.path.join(ATTACH, "**", "Img", "*_t.dat"), recursive=True)]
    dats.sort(key=os.path.getmtime, reverse=True)
    ct = None
    for f in dats:
        head = open(f, "rb").read(64)
        if head[:6] == MAGIC_V2:
            ct = head[15:31]
            break
    if ct is None:
        raise SystemExit("没有 V2 .dat 可用作已知密文")
    xor_key = F.find_xor_key(ATTACH)
    for pid in F.get_wechat_pids():
        aes = F.scan_memory_for_aes_key(pid, ct)
        if aes:
            save_image_keys(aes, xor_key or 0, account or "default")
            print("内存扫描命中: aes=%s xor=%s (pid=%s)" % (aes, hex(xor_key or 0), pid))
            return {account or "default": {"aes": aes, "xor": xor_key or 0}}
    raise SystemExit("两种方式都没拿到 key（微信需运行且已点开过图片）")


def keys_for(path, fallback_acct=None):
    """按 .dat 所在账号目录挑密钥，挑不到用 fallback/default。"""
    acct = account_of_path(path) or fallback_acct
    return load_image_keys(acct)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*", help="消息 md5 或 dat 文件名")
    ap.add_argument("--last", type=int, help="最近 N 张")
    ap.add_argument("--find-key", action="store_true")
    ap.add_argument("--account", default=None)
    ap.add_argument("--outdir", default=OUT_DIR)
    args = ap.parse_args()

    if args.find_key:
        find_key(args.account)
        return

    os.makedirs(args.outdir, exist_ok=True)

    dats = []
    if args.last:
        dats = latest_dats(args.last)
    for t in args.targets:
        dats += find_dats(t)
    if not dats:
        ap.error("没找到 .dat，给个 md5 / 文件名，或 --last N")

    for p in dats:
        try:
            aes_str, xor = keys_for(p, args.account)
            buf = decrypt(p, aes_str.encode()[:16], xor)
        except Exception as e:
            print(os.path.basename(p), "ERR", e)
            continue
        base = os.path.basename(p).replace(".dat", "")
        out = to_image(buf, os.path.join(args.outdir, base))
        print(os.path.basename(p), len(buf), "->", out or "无法识别（未知格式）")


if __name__ == "__main__":
    main()
