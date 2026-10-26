# -*- coding: utf-8 -*-
r"""走 aixed version.dll 的 HTTP API 发消息（不占屏幕、不动鼠标）。

前提：微信 4.1.10.27 + `D:\Weixin\version.dll`（606208 B 的注入器），
启动后本机 30001 端口由 Weixin.exe 监听。

API: POST http://127.0.0.1:30001/SendTextMsg
     {"wxidorgid": <会话 wxid 或 xxx@chatroom>, "msg": <文本>}
     返回 {"ret": 0, "retmsg": "success"}

其它可用端点（备查）：
     /SendImgMsg {wxidorgid, path}   /send_file_msg {wxid, filepath, stage}
     /ForwardXMLMsg {to_wxid, content}   /GetSelfProfile   /QueryDB/status
"""
import os
import time

import requests

BASE = "http://127.0.0.1:30001"
STATUS_TIMEOUT = 1.5
SEND_TIMEOUT = 20


def available(timeout=STATUS_TIMEOUT):
    """30001 上有没有活的 hook 服务。"""
    try:
        r = requests.get(BASE + "/QueryDB/status", timeout=timeout)
        return r.status_code == 200 and "hWeixin" in r.text
    except Exception:
        return False


def self_profile(timeout=5):
    try:
        r = requests.post(BASE + "/GetSelfProfile", json={}, timeout=timeout)
        return r.json()
    except Exception:
        return None


def send_text(room, text, timeout=SEND_TIMEOUT):
    """返回 (ok, info)；ok 只代表 DLL 收下了，不代表已经落库。"""
    try:
        r = requests.post(BASE + "/SendTextMsg",
                          json={"wxidorgid": room, "msg": text},
                          timeout=timeout)
        data = r.json()
        return data.get("ret") == 0, data
    except Exception as e:
        return False, {"err": "%s: %s" % (type(e).__name__, e)}


def send_image(room, path, timeout=SEND_TIMEOUT):
    """发一张本机图片（表情包）。

    ⚠️ 2026-10-23 实测（用户 m06922「为什么发不出gif动图」）：
      - `/SendImgMsg` 走的是**图片**通道，落库 `local_type=3`，对方看到的是静态图（点开也不动）；
      - `/send_file_msg` 走**文件**通道，微信会把 .gif 认成表情，落库 `local_type=47`
        + `<emoji type="2" md5=…>`，对方看到的是会动的表情包。
    所以：GIF 走 send_file_msg（字段 wxid + filepath），PNG/JPG 仍走 SendImgMsg（字段 wxidorgid + path）。
    """
    try:
        if not path or not os.path.exists(path):
            return False, {"err": "文件不存在: %s" % path}
        if os.path.getsize(path) == 0:
            return False, {"err": "空文件: %s" % path}
        full = os.path.abspath(path)
        if os.path.splitext(full)[1].lower() == ".gif":
            r = requests.post(BASE + "/send_file_msg",
                              json={"wxid": room, "filepath": full},
                              timeout=max(timeout, 60))
        else:
            r = requests.post(BASE + "/SendImgMsg",
                              json={"wxidorgid": room, "path": full},
                              timeout=timeout)
        data = r.json()
        return data.get("ret") == 0, data
    except Exception as e:
        return False, {"err": "%s: %s" % (type(e).__name__, e)}


def send_xml(room, xml, timeout=SEND_TIMEOUT):
    """发一条 appmsg XML（拍一拍等）。返回 (ok, info)。"""
    try:
        r = requests.post(BASE + "/send_xml",
                          json={"wxidorgid": room, "content": xml},
                          timeout=timeout)
        data = r.json()
        return data.get("ret") == 0, data
    except Exception as e:
        return False, {"err": "%s: %s" % (type(e).__name__, e)}


def wait(seconds):
    time.sleep(seconds)


if __name__ == "__main__":
    import sys
    print("available:", available())
    print("self:", self_profile())
    if len(sys.argv) >= 3:
        print("send:", send_text(sys.argv[1], sys.argv[2]))
