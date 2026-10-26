# -*- coding: utf-8 -*-
"""config.py —— 统一的路径配置（换机器只需要改 settings.json 的 paths 段）

优先级：环境变量 > settings.json 的 "paths" 段 > 内置默认值。

settings.json 里这样写（给的都是示例，按自己机器改）：

    "paths": {
      "wechat_files": "C:\\\\Users\\\\你的用户名\\\\Documents\\\\xwechat_files",
      "ffmpeg": "C:\\\\ffmpeg\\\\bin\\\\ffmpeg.exe",
      "sticker_lib": "D:\\\\Deepseek\\\\stickers\\\\library"
    }

对应的环境变量：WX_WECHAT_FILES / WX_FFMPEG / WX_STICKER_LIB。
"""
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "settings.json")
_cache = None


def settings():
    global _cache
    if _cache is None:
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                _cache = json.load(f) or {}
        except Exception:
            _cache = {}
    return _cache


def reload():
    """改完 settings.json 想立刻生效时调用（进程里一般不用）。"""
    global _cache
    _cache = None
    return settings()


def paths():
    return settings().get("paths") or {}


def _expand(p):
    """展开环境变量和 ~；相对路径按「程序目录」解析（不是当前工作目录）。"""
    if not p:
        return ""
    p = os.path.expandvars(os.path.expanduser(p))
    if not os.path.isabs(p):
        p = os.path.join(HERE, p)
    return os.path.normpath(p)


def _first_dir(*cands):
    for c in cands:
        p = _expand(c)
        if p and os.path.isdir(p):
            return p
    for c in cands:                      # 目录都不存在时，仍返回配置值，方便报错定位
        p = _expand(c)
        if p:
            return p
    return ""


def _first_file(*cands):
    for c in cands:
        p = _expand(c)
        if p and os.path.isfile(p):
            return p
    return ""


def home_documents(*parts):
    return os.path.join(os.path.expanduser("~"), "Documents", *parts)


def wechat_files():
    """微信数据根目录（下面有 wxid_xxx_xxxx 账号目录）。

    显式配置优先（环境变量 → settings.json 的 paths.wechat_files），
    **即使那个目录不存在也按配置返回**，这样报错信息能直接指出配错的位置。
    """
    explicit = os.environ.get("WX_WECHAT_FILES") or paths().get("wechat_files")
    if explicit:
        return _expand(explicit)
    return _first_dir(home_documents("xwechat_files"))


def ffmpeg():
    """转 wxgf（HEVC 外壳）用的 ffmpeg。找不到返回空串，此时高清原图会退回缩略图。"""
    return _first_file(os.environ.get("WX_FFMPEG"),
                       paths().get("ffmpeg"),
                       shutil.which("ffmpeg"),
                       os.path.expandvars(
                           r"%LOCALAPPDATA%\Programs\WeFlow\resources\app.asar.unpacked"
                           r"\node_modules\ffmpeg-static\ffmpeg.exe"))


def sticker_lib():
    """表情包库目录（里面有 index.md 和一堆图片）。"""
    return _first_dir(os.environ.get("WX_STICKER_LIB"),
                      paths().get("sticker_lib"),
                      home_documents("stickers", "library"))
