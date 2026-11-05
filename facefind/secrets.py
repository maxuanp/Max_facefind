from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import Config

FILES = {"xiaohongshu": "xhs.txt", "weibo": "weibo.txt"}
# Cookie 文件是“可选补充”：浏览器持久会话已登录时不需要任何 cookie 文件。
# 因此不再把 SUB / web_session 之类的名字当作硬性必需字段（cookie 名不等于登录态）。
REQUIRED = {"xiaohongshu": [], "weibo": []}
EXPECTED = {
    "xiaohongshu": ["a1", "web_session", "webId"],
    "weibo": ["SUB", "SUBP", "SSOLoginState"],
}


def file_path(site: str, cfg: Config) -> Path:
    return cfg.secrets_path / FILES.get(site, f"{site}.txt")


def parse_cookie(text: str) -> dict[str, str]:
    """解析一行 `name=value; name=value`。"""
    out: dict[str, str] = {}
    for part in text.strip().split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"')
        if name:
            out[name] = value
    return out


def read_raw(site: str, cfg: Config) -> str:
    """只读取 cookie 文本；调用方不得把内容写入日志/索引。"""
    p = file_path(site, cfg)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def read_cookie(site: str, cfg: Config) -> dict[str, str]:
    return parse_cookie(read_raw(site, cfg))


def cookie_header(site: str, cfg: Config) -> str:
    cookies = read_cookie(site, cfg)
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def status(site: str, cfg: Config) -> dict:
    """返回可安全展示的状态（不包含任何 cookie 值）。"""
    p = file_path(site, cfg)
    info = {
        "site": site,
        "file": str(p),
        "exists": p.exists(),
        "readable": False,
        "keys": [],
        "required": REQUIRED.get(site, []),
        "expected": EXPECTED.get(site, []),
        "optional": True,
        "note": "可选：浏览器会话已登录时不需要 cookie 文件",
        "missing": [],
        "mtime": "",
    }
    if not p.exists():
        info["missing"] = list(REQUIRED.get(site, []))
        return info
    try:
        text = p.read_text(encoding="utf-8")
        info["readable"] = True
        info["mtime"] = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    except (OSError, UnicodeDecodeError):
        return info
    keys = parse_cookie(text).keys()
    info["keys"] = sorted(keys)
    info["missing"] = [k for k in REQUIRED.get(site, []) if k not in keys]
    return info
