# -*- coding: utf-8 -*-
"""识别本地图片：主用 DeepSeek（deepseek-flash，自带 key），OVH 免费端点做兜底。

对外只有 describe(path, prompt) -> 描述文本 / None。
失败不抛异常：超时、连接被掐、429 都内部重试，最后返回 None，让调用方退回占位符。

文件名保留 vision_ovh.py 是为了不牵动 wx_vision.py 的 import。
"""
import base64
import io
import os
import sys
import time

import requests
from PIL import Image

# ---------------------------------------------------------------- DeepSeek
DS_URL = "https://api.deepseek.com/chat/completions"
DS_MODEL = "deepseek-flash"      # 支持图形输入；deepseek-v4-pro 不行
DS_TIMEOUT = 90
DS_MAX_TOKENS = 600
DS_RETRIES = 1

# ---------------------------------------------------------------- OVH 兜底
OVH_URL = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions"
OVH_MODEL = "Qwen2.5-VL-72B-Instruct"
OVH_TIMEOUT = 30
OVH_NET_RETRIES = 1
OVH_RATE_RETRIES = 3

MAX_SIDE = 768          # 太大的图 base64 到几百 KB，免费端点会直接掐连接

_LAST = {"kind": None}   # "local"=图本身坏了/没下全；"net"=后端不通


def last_fail_kind():
    return _LAST["kind"]


def deepseek_key():
    """环境变量优先，其次 ~/.dsh/.credentials.yaml 里的 DEEPSEEK_API_KEY。"""
    k = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if k.startswith("sk-"):
        return k
    path = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "DEEPSEEK_API_KEY" in line and ":" in line:
                    v = line.split(":", 1)[1].strip().strip("'\"")
                    if v.startswith("sk-"):
                        return v
    except Exception:
        pass
    return None


def _payload(path):
    """读图 -> 缩到 MAX_SIDE -> jpeg base64；返回 (b64, mime) 或抛异常。"""
    im = Image.open(path)
    im = im.convert("RGB")
    if max(im.size) > MAX_SIDE:
        scale = MAX_SIDE / float(max(im.size))
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def _body(model, b64, mime, prompt, no_think):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
        ]}],
        "max_tokens": DS_MAX_TOKENS,
    }
    if no_think:
        body["thinking"] = {"type": "disabled"}   # 实测 714 token / 3.4s -> 225 token / 1.4s
    return body


def describe_deepseek(b64, mime, prompt):
    key = deepseek_key()
    if not key:
        print("DeepSeek 没有 key，跳过", file=sys.stderr)
        return None
    body = _body(DS_MODEL, b64, mime, prompt, True)
    fail = 0
    while True:
        try:
            r = requests.post(DS_URL, json=body, timeout=DS_TIMEOUT,
                              headers={"Authorization": "Bearer " + key})
        except Exception as e:
            fail += 1
            print("DeepSeek 网络错误(%d/%d): %s" % (fail, DS_RETRIES, str(e)[:60]), file=sys.stderr)
            if fail > DS_RETRIES:
                return None
            time.sleep(2)
            continue
        if r.status_code == 200:
            try:
                msg = r.json()["choices"][0]["message"]
                return (msg.get("content") or "").strip() or None
            except Exception as e:
                print("DeepSeek 返回解析失败: %s" % str(e)[:60], file=sys.stderr)
                return None
        print("DeepSeek HTTP %s: %s" % (r.status_code, r.text[:200]), file=sys.stderr)
        return None


def describe_ovh(b64, mime, prompt):
    body = _body(OVH_MODEL, b64, mime, prompt, False)
    net_fail = 0
    rate_fail = 0
    while True:
        try:
            r = requests.post(OVH_URL, json=body, timeout=OVH_TIMEOUT)
        except Exception as e:
            net_fail += 1
            print("OVH 网络错误(%d/%d): %s" % (net_fail, OVH_NET_RETRIES, str(e)[:60]), file=sys.stderr)
            if net_fail > OVH_NET_RETRIES:
                return None
            time.sleep(3)
            continue
        if r.status_code == 200:
            try:
                return (r.json()["choices"][0]["message"]["content"] or "").strip() or None
            except Exception as e:
                print("OVH 返回解析失败: %s" % str(e)[:60], file=sys.stderr)
                return None
        if r.status_code == 429:
            rate_fail += 1
            if rate_fail > OVH_RATE_RETRIES:
                print("OVH 429 重试用尽", file=sys.stderr)
                return None
            print("[429] OVH 限流，等 30s", file=sys.stderr)
            time.sleep(30)
            continue
        print("OVH HTTP %s: %s" % (r.status_code, r.text[:200]), file=sys.stderr)
        return None


def describe(path, prompt):
    try:
        b64, mime = _payload(path)
    except Exception as e:
        print("图片打不开: %s" % str(e)[:60], file=sys.stderr)
        _LAST["kind"] = "local"
        return None

    _LAST["kind"] = None
    out = describe_deepseek(b64, mime, prompt)
    if out:
        return out
    out = describe_ovh(b64, mime, prompt)
    if not out:
        _LAST["kind"] = "net"
    return out


if __name__ == "__main__":
    p = sys.argv[1]
    q = sys.argv[2] if len(sys.argv) > 2 else (
        "详细描述这张图：画面内容、角色外观、配色、表情动作；"
        "图上的所有文字请逐字转写，并说明每行文字的位置和颜色。")
    print(describe(p, q))
