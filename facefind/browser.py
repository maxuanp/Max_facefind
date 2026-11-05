"""浏览器自动化（Playwright）。

会话隔离（重要）：
  - 所有站点共用一个 **项目私有** 的持久化 user_data_dir：`<项目>/data/browser_profile`；
  - 绝不使用系统 Chrome/Edge 的默认 User Data 目录（`%LOCALAPPDATA%\\Google\\Chrome\\User Data`
    / `%LOCALAPPDATA%\\Microsoft\\Edge\\User Data`），因此不会读取或改写你日常使用的账号；
  - 启动前会校验 profile 路径，如果被配置成系统默认目录会直接拒绝启动；
  - `channel="chrome"` 只是复用本机 Chrome 的可执行文件，profile 仍然是上面的独立目录。
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .models import SearchItem
from .urlkey import canonical_url, note_key as _note_key
from . import secrets


class BrowserError(RuntimeError):
    """Playwright 不可用 / 浏览器启动失败。"""


class RiskControlError(BrowserError):
    """检测到验证码、滑块或风控拦截。"""


class LoginRequiredError(BrowserError):
    """该站点需要先登录。"""


_LOCK = threading.Lock()          # 同一 profile 只能有一个浏览器实例
_CACHE: dict[str, dict] = {}       # {site: 最近一次登录检查结果}
_CACHE_LOCK = threading.Lock()

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
EDGE_CANDIDATES = [
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

LOGIN_URLS = {
    "xiaohongshu": "https://www.xiaohongshu.com/explore",
    "weibo": "https://weibo.com/login.php",
}
HOME_URLS = {
    "xiaohongshu": "https://www.xiaohongshu.com/explore",
    "weibo": "https://m.weibo.cn/",
}
COOKIE_DOMAINS = {
    "xiaohongshu": ".xiaohongshu.com",
    "weibo": ".weibo.com",
}
SESSION_COOKIES = {
    "xiaohongshu": ["web_session"],
    "weibo": ["SUB"],
}
# 登出时只清理该站点所属域名的 cookie / 本地存储
SITE_COOKIE_ROOTS = {
    "xiaohongshu": ("xiaohongshu.com",),
    "weibo": ("weibo.com", "weibo.cn", "sina.com.cn"),
}
SITE_ORIGINS = {
    "xiaohongshu": ("https://www.xiaohongshu.com", "https://xiaohongshu.com"),
    "weibo": ("https://weibo.com", "https://s.weibo.com", "https://m.weibo.cn", "https://passport.weibo.com"),
}
# 「验证码」这类词会出现在正常登录表单（如「获取验证码」按钮），不能作为风控依据；
# 这里只保留站点拦截页/验证页的真实文案与结构标记（见 _risk_hits）。
RISK_WORDS = ("滑块", "安全验证", "访问频繁", "异常访问", "访问验证", "安全限制",
              "当前网络存在风险", "captcha", "verify")


def detect_channel(preferred: str = "") -> str:
    if preferred:
        return preferred
    if any(Path(p).exists() for p in CHROME_CANDIDATES):
        return "chrome"
    if any(Path(p).exists() for p in EDGE_CANDIDATES):
        return "msedge"
    return ""


def profile_dir(cfg: Config) -> Path:
    p = cfg.browser_profile_path
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_profile_dirs() -> dict[str, Path]:
    """系统 Chrome / Edge 的默认 User Data 目录（本工具绝不使用）。"""
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return {
        "chrome": base / "Google" / "Chrome" / "User Data",
        "msedge": base / "Microsoft" / "Edge" / "User Data",
    }


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def isolation_status(cfg: Config) -> dict[str, Any]:
    """返回持久会话路径与系统默认 profile 的隔离关系（不启动浏览器）。"""
    prof = profile_dir(cfg).resolve()
    defaults = []
    inside_any = False
    for name, path in default_profile_dirs().items():
        rp = path.resolve()
        contains = _inside(prof, rp)
        inside_any = inside_any or contains
        defaults.append({
            "name": name,
            "path": str(rp),
            "exists": rp.exists(),
            "project_profile_inside": contains,
            "in_use": False,   # 本工具从不打开这个目录
        })
    return {
        "profile_dir": str(prof),
        "profile_exists": prof.exists(),
        "isolated": not inside_any,
        "default_profiles": defaults,
        "note": (
            "Playwright 使用独立 user_data_dir；系统 Chrome/Edge 默认配置目录只读不写。"
            if not inside_any else
            "危险：持久会话目录位于系统浏览器默认配置目录内，已拒绝启动。"
        ),
    }


def _snapshot_default_profiles() -> list[dict[str, Any]]:
    """记录系统默认 profile 关键文件的 mtime/size，用于证明未被改动。"""
    rels = (
        "Local State",
        "Default/Preferences",
        "Default/Network/Cookies",
        "Default/Cookies",
        "Default/Login Data",
    )
    out: list[dict[str, Any]] = []
    for name, base in default_profile_dirs().items():
        for rel in rels:
            p = base / rel
            item: dict[str, Any] = {"key": f"{name}/{rel}", "path": str(p), "exists": False}
            try:
                st = p.stat()
                item.update({"exists": True, "mtime_ns": st.st_mtime_ns, "size": st.st_size})
            except OSError:
                pass
            out.append(item)
    return out


def isolation_evidence(cfg: Config, *, run_session: bool = True) -> dict[str, Any]:
    """实际启动一次无头会话，与前后快照对比，证明默认 profile 未被读写。"""
    status = isolation_status(cfg)
    prof = Path(status["profile_dir"])
    before = _snapshot_default_profiles()
    reported = ""
    launch_error = ""
    session_ok = False
    if run_session:
        try:
            with _session(cfg, headless=True, timeout_ms=45000) as context:
                session_ok = True
                page = _first_page(context)
                try:
                    page.goto("chrome://version", wait_until="domcontentloaded", timeout=15000)
                    text = page.evaluate("() => document.body.innerText") or ""
                    m = re.search(r"(?:Profile Path|配置文件路径)[\s:：]+(.+)", text)
                    if m:
                        reported = m.group(1).strip()
                except Exception as exc:
                    launch_error = f"chrome://version 读取失败：{exc.__class__.__name__}"
        except Exception as exc:
            launch_error = f"{exc.__class__.__name__}: {exc}"
    after = _snapshot_default_profiles()
    changed = [
        x["key"] for x, y in zip(before, after)
        if x.get("exists") and (x.get("mtime_ns") != y.get("mtime_ns") or x.get("size") != y.get("size"))
    ]
    files = 0
    try:
        files = sum(1 for _ in prof.rglob("*") if _.is_file())
    except OSError:
        pass
    matches = bool(reported) and os.path.normcase(os.path.normpath(reported)) == os.path.normcase(os.path.normpath(str(prof)))
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "profile_dir": str(prof),
        "isolated": status["isolated"],
        "default_profiles": status["default_profiles"],
        "session_started": session_ok,
        "browser_reported_profile_path": reported,
        "browser_profile_matches": matches,
        "default_files_checked": len(before),
        "default_files_changed": changed,
        "default_profile_untouched": not changed,
        "project_profile_files": files,
        "launch_error": launch_error,
    }


def playwright_installed() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def _inject_cookies(context, site: str, cfg: Config) -> None:
    """把 secrets 里的 cookie 注入持久会话（不打印、不落库）。"""
    cookies = secrets.read_cookie(site, cfg)
    if not cookies:
        return
    payload = [
        {"name": k, "value": v, "domain": COOKIE_DOMAINS.get(site, ""), "path": "/"}
        for k, v in cookies.items()
        if COOKIE_DOMAINS.get(site)
    ]
    try:
        context.add_cookies(payload)
    except Exception:
        pass


@contextmanager
def _session(cfg: Config, *, headless: bool, timeout_ms: int = 30000):
    if not playwright_installed():
        raise BrowserError(
            "Playwright 未安装：请执行 `python -m pip install playwright`。"
            "本工具优先复用本机 Chrome/Edge，无需下载浏览器。"
        )
    from playwright.sync_api import sync_playwright

    channel = detect_channel(cfg.browser.channel)
    profile = profile_dir(cfg)
    status = isolation_status(cfg)
    if not status["isolated"]:
        raise BrowserError(
            "持久会话目录位于系统浏览器默认配置目录内，已拒绝启动："
            f"{status['profile_dir']}。请把 browser.profile_dir 改回项目内路径（如 data/browser_profile）。"
        )
    _LOCK.acquire()
    pw = None
    context = None
    try:
        pw = sync_playwright().start()
        attempts: list[tuple[str | None, str]] = []
        if channel:
            attempts.append((channel, channel))
        attempts.append((None, "bundled-chromium"))
        if channel == "chrome":
            attempts.append(("msedge", "msedge"))
        errors: list[str] = []
        for chan, label in attempts:
            try:
                # 关键：user_data_dir 固定为项目私有目录，与系统默认 profile 完全隔离
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    channel=chan,
                    headless=headless,
                    viewport={"width": 1440, "height": 900},
                    locale="zh-CN",
                    timeout=timeout_ms,
                    args=["--no-first-run", "--no-default-browser-check"]
                    + (["--start-maximized"] if not headless else []),
                )
                break
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        if context is None:
            lock = profile / "SingletonLock"
            lock_hint = (
                f"。检测到 profile 锁文件 {lock}：可能仍有残留浏览器占用该 profile，"
                "请先关闭残留的 Chrome/Edge 窗口，再重试"
                if lock.exists() else ""
            )
            raise BrowserError(
                "无法启动浏览器（已尝试本机 Chrome/Edge）：" + " | ".join(errors[:2]) +
                "。如本机没有 Chrome/Edge，请执行 `python -m playwright install chromium`。"
                + lock_hint
            )
        yield context
    finally:
        # 用户手动关窗时 context 已失效，close() 会抛错；不能让异常覆盖真正的返回结果
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        _LOCK.release()


def _first_page(context):
    return context.pages[0] if context.pages else context.new_page()


def _risk_text(page) -> str:
    try:
        return page.evaluate("() => (document.body ? document.body.innerText.slice(0, 3000) : '')") or ""
    except Exception:
        return ""


def _risk_hits(page) -> list[str]:
    text = _risk_text(page)
    low = text.lower()
    hits = [w for w in RISK_WORDS if w.lower() in low]
    try:
        markers = page.evaluate(
            """
            () => ['.fe-verify-box', '.captcha', '#captcha', 'iframe[src*="captcha"]']
              .filter((s) => document.querySelector(s))
            """
        ) or []
    except Exception:
        markers = []
    hits.extend(f"dom:{m}" for m in markers)
    url = page.url.lower()
    if "captcha" in url:
        hits.append("url:captcha")
    if "/verify" in url:
        hits.append("url:/verify")
    return hits


def _risk_detected(page) -> bool:
    return bool(_risk_hits(page))


def _cookie_names(context) -> list[str]:
    """仅用于展示：当前会话里的 cookie 名，不作为登录依据。"""
    return sorted({c.get("name") for c in context.cookies() if c.get("name")})


# 未登录访问时站点下发的访客类 cookie（只清理这些，不动会话 cookie）
VISITOR_COOKIES = {
    # 只列真正的访客标识；SUB/SUBP/SSOLoginState 属于登录会话 cookie，不能删（会把人踢下线）
    "weibo": ("_T_WM", "MWEIBOID", "XSRF-TOKEN", "WEIBOCN_FROM"),
    "xiaohongshu": ("webId", "gid", "xsecappid", "customerClientId"),
}
WEIBO_CONFIG_API = "https://m.weibo.cn/api/config"


def _clear_visitor_cookies(context, page, site: str) -> list[str]:
    """登录前清理该站点域内的旧访客 cookie，避免历史残留造成误判；不影响其它站点。"""
    names = VISITOR_COOKIES.get(site, ())
    if not names:
        return []
    try:
        cdp = context.new_cdp_session(page)
    except Exception:
        return []
    removed: list[str] = []
    for c in context.cookies():
        if not _domain_match(c.get("domain", ""), site):
            continue
        name = c.get("name") or ""
        if not (name in names or name.startswith("_T_")):
            continue
        try:
            cdp.send("Network.deleteCookies", {
                "name": name, "domain": c.get("domain"), "path": c.get("path") or "/",
            })
            removed.append(name)
        except Exception:
            pass
    return removed


def _weibo_api_state(context) -> dict[str, Any]:
    """以 m.weibo.cn/api/config 的 data.login 为准判定微博登录态。"""
    state: dict[str, Any] = {
        "confirmed": False, "logged_in": False, "uid": "", "nickname": "",
        "source": "", "detail": "",
    }
    try:
        resp = context.request.get(
            WEIBO_CONFIG_API, headers={"Referer": "https://m.weibo.cn/"}, timeout=15000
        )
    except Exception as exc:
        state["detail"] = f"登录态接口不可用：{exc.__class__.__name__}"
        return state
    try:
        data = resp.json()
    except Exception:
        state["detail"] = f"登录态接口返回非 JSON（HTTP {resp.status}）"
        return state
    body = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        state["detail"] = "登录态接口返回结构异常"
        return state
    state["confirmed"] = True
    state["logged_in"] = bool(body.get("login"))
    # 注意：m.weibo.cn/api/config 登录后常只返回顶层 uid，不带 user 对象，
    # 所以 UID 必须同时从 data.uid 读取，不能只依赖 data.user.id。
    uid = str(body.get("uid") or "")
    user = body.get("user") or {}
    if isinstance(user, dict):
        uid = uid or str(user.get("id") or user.get("uid") or "")
        state["nickname"] = str(
            user.get("screen_name") or user.get("nick") or ""
        ).strip()
    state["uid"] = uid
    state["source"] = "api_config"
    if state["logged_in"] and uid and not state["nickname"]:
        # config 接口不带昵称时补一次用户资料接口；取不到昵称不影响登录判定
        state["nickname"] = _weibo_nickname(context, uid)
    if state["logged_in"] and not uid:
        # login=true 但拿不到本人 UID：无法确认是谁，按未登录处理，避免误判
        state["logged_in"] = False
        state["detail"] = "接口 login=true 但未返回本人 UID，按未登录处理"
    return state


def _weibo_nickname(context, uid: str) -> str:
    """按接口返回的本人 UID 取 screen_name（失败返回空，不阻断登录判定）。"""
    try:
        resp = context.request.get(
            f"https://m.weibo.cn/api/users/show?id={uid}",
            headers={"Referer": f"https://m.weibo.cn/u/{uid}"},
            timeout=15000,
        )
        data = (resp.json() or {}).get("data") or {}
        if isinstance(data, dict) and str(data.get("id") or "") == str(uid):
            return str(data.get("screen_name") or "").strip()
    except Exception:
        return ""
    return ""


_XHS_STATE_JS = """
() => {
  const out = {ok: false, logged_in: false, nickname: '', uid: '', source: '', reason: ''};
  // 小红书 PC 端 __INITIAL_STATE__ 已是 Vue ref（{__v_isRef:true, _value/_rawValue}）；
  // 直接读 ref 对象永远是真值，旧实现因此把访客当成已登录（登录窗口 4s 就被关掉）。
  const unwrap = (v) => {
    if (v && typeof v === 'object' && ('__v_isRef' in v)) {
      return v._rawValue !== undefined ? v._rawValue : v._value;
    }
    return v;
  };
  const st = window.__INITIAL_STATE__;
  const u = (st && st.user) || {};
  const info = unwrap(u.userInfo) || {};
  const loggedIn = unwrap(u.loggedIn);
  const uid = String(info.userId || info.user_id || unwrap(u.userId) || '');
  const nick = String(info.nickname || info.nickName || unwrap(u.nickname) || '');
  const guest = info.guest === true || unwrap(u.guest) === true;
  const bodyText = (document.body ? document.body.innerText : '').slice(0, 400);
  const modal = /电脑设备登录超限|请重新登录|登录后查看/.test(bodyText);
  out.ok = !!st;
  out.uid = uid;
  out.nickname = nick;
  out.source = st ? 'web_state' : '';
  if (modal) {
    out.ok = true;
    out.logged_in = false;
    out.reason = 'login_required';
  } else if (loggedIn === true) {
    // 必须同时具备账号 ID 与昵称、且非访客态，才算确认登录
    out.logged_in = !guest && !!uid && !!nick;
    out.reason = out.logged_in ? 'account' : 'incomplete_identity';
  }
  if (!out.ok) {
    const btn = document.querySelector('.login-btn, .side-bar .login-container, .user-side-bar .btn');
    if (btn) { out.ok = true; out.logged_in = false; out.source = 'dom'; }
  }
  return out;
}
"""


def _xhs_state(page) -> dict[str, Any]:
    """小红书：按页面登录态（__INITIAL_STATE__.user）判定，不看 cookie 名。"""
    state: dict[str, Any] = {
        "confirmed": False, "logged_in": False, "uid": "", "nickname": "",
        "source": "", "detail": "",
    }
    try:
        data = page.evaluate(_XHS_STATE_JS) or {}
    except Exception as exc:
        state["detail"] = f"页面登录态读取失败：{exc.__class__.__name__}"
        return state
    state["confirmed"] = bool(data.get("ok"))
    state["logged_in"] = bool(data.get("logged_in"))
    state["uid"] = str(data.get("uid") or "")
    state["nickname"] = str(data.get("nickname") or "").strip()
    state["source"] = str(data.get("source") or "")
    if not state["confirmed"]:
        state["detail"] = "页面未暴露登录态"
    elif state["logged_in"] and not state["nickname"]:
        state["nickname"] = ""
    return state


def _state(site: str, page, context) -> dict[str, Any]:
    if site == "weibo":
        return _weibo_api_state(context)
    return _xhs_state(page)


def _state_status(state: dict) -> str:
    if state.get("logged_in"):
        return "logged_in"
    if state.get("confirmed"):
        return "logged_out"
    return "unknown"


def _page_count(context) -> int:
    try:
        return len(context.pages)
    except Exception:
        return -1


def _page_notes(page) -> int:
    try:
        return int(page.evaluate("() => document.querySelectorAll('section.note-item').length"))
    except Exception:
        return -1


def _state_detail(state: dict, risk: bool) -> str:
    # 页面明确要求重新登录时，优先给出登录结论（此类页面里也可能夹带「安全验证」等字样）
    if state.get("reason") == "login_required":
        return "页面要求重新登录（会话已失效或设备登录超限），请在窗口内扫码登录"
    if risk:
        return "页面出现安全限制/风控拦截（不是未登录），请稍后重试或改用有头模式"
    status = _state_status(state)
    if status == "logged_in":
        return f"接口确认已登录（{state.get('source') or '接口'}）"
    if status == "logged_out":
        return "接口确认未登录，请点击「打开登录窗口」登录"
    return f"无法确认登录态（{state.get('detail') or '接口未返回'}），请重试"


def _state_account(state: dict) -> dict[str, str]:
    if not state.get("logged_in"):
        return {"nickname": "", "uid": "", "source": ""}
    return {
        "nickname": str(state.get("nickname") or ""),
        "uid": str(state.get("uid") or ""),
        "source": str(state.get("source") or ""),
    }


def check_login(site: str, cfg: Config, *, timeout_ms: int = 30000) -> dict:
    """无头检查登录态：以站点接口/页面实际登录态为准，cookie 名只作展示。"""
    with _session(cfg, headless=True, timeout_ms=timeout_ms) as context:
        _inject_cookies(context, site, cfg)
        page = _first_page(context)
        try:
            page.goto(HOME_URLS[site], wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        risk = _risk_detected(page)
        state = _state(site, page, context)
        result = {
            "site": site,
            "browser_logged_in": bool(state.get("logged_in")),
            "confirmed": bool(state.get("confirmed")),
            "status": _state_status(state),
            "cookie_names": _cookie_names(context),
            "account": _state_account(state),
            "risk": risk,
            "detail": _state_detail(state, risk),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "channel": detect_channel(cfg.browser.channel) or "chromium",
        }
        with _CACHE_LOCK:
            _CACHE[site] = result
        return result


def open_login_window(site: str, cfg: Config, on_state=None, log=None) -> dict:
    """打开有头浏览器供用户手动登录。

    硬规则：
      1) 窗口一律强制有头（headless=False）：登录必须人工扫码，无头窗口用户看不见，
         因此这里显式覆盖全局 browser.headless（搜索/检查仍按配置走无头）；
      2) 未确认登录前任何情况都不关窗（browser.login_timeout 只产生提示文案）；
      3) 确认登录后：站点在 browser.login_keep_open 里（默认 xiaohongshu）则保持窗口，
         由用户手动关闭；其余站点自动关闭。
    """

    def note(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    started = datetime.now()
    hint_after = max(60, int(cfg.browser.login_timeout))
    keep_open = site in (cfg.browser.login_keep_open or [])
    hinted = False
    waited = 0
    removed: list[str] = []
    closed_by_user = False
    confirmed = False
    risk = False
    risk_logged = False
    last_detail = ""
    state: dict[str, Any] = {"confirmed": False, "logged_in": False, "detail": ""}
    with _session(cfg, headless=False, timeout_ms=60000) as context:
        page = _first_page(context)
        note(f"会话已启动（headless=False 强制有头，keep_open={keep_open}）")
        # 登录前清掉该站点旧访客 cookie，避免历史残留把访客当成已登录
        removed = _clear_visitor_cookies(context, page, site)
        note(f"已清理访客 cookie：{removed or '无'}")
        _inject_cookies(context, site, cfg)
        try:
            page.goto(LOGIN_URLS[site], wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            note(f"打开登录页失败：{exc.__class__.__name__}: {exc}")
        note(f"登录页已加载：{page.url}")
        if on_state:
            try:
                on_state({
                    "state": "waiting",
                    "status": "unknown",
                    "detail": "已打开登录窗口，等待扫码登录（未确认登录前窗口不会关闭）…",
                    "removed_cookies": removed,
                    "waited_seconds": 0,
                })
            except Exception:
                pass
        while True:
            if page.is_closed():
                closed_by_user = True
                note(f"页面句柄已关闭（用户关窗/渲染进程退出）：context.pages={_page_count(context)}，"
                     f"waited={int((datetime.now() - started).total_seconds())}s")
                break
            try:
                page.wait_for_timeout(2000)
            except Exception as exc:
                closed_by_user = True
                note(f"等待期间异常：{exc.__class__.__name__}: {exc}，"
                     f"context.pages={_page_count(context)}")
                break
            if page.is_closed():
                closed_by_user = True
                note(f"页面句柄已关闭（wait 之后）：context.pages={_page_count(context)}")
                break
            risk = _risk_detected(page)
            state = _state(site, page, context)
            waited = int((datetime.now() - started).total_seconds())
            if risk and not risk_logged:
                risk_logged = True
                note(f"风控命中词：{_risk_hits(page)} · 正文片段={_risk_text(page)[:80]!r}")
            if not risk and waited % 10 < 2:
                note(f"心跳：waited={waited}s url={page.url} notes={_page_notes(page)}")
            detail_now = _state_detail(state, risk)
            if detail_now != last_detail:
                last_detail = detail_now
                note(f"状态变化：{detail_now}")
            if state.get("logged_in"):
                confirmed = True
                detail = (
                    f"已确认登录（{state.get('nickname') or '-'} / {state.get('uid') or '-'}）"
                    + ("；窗口保持打开，请手动关闭（关闭后其它任务才能使用浏览器）"
                       if keep_open else "；窗口将自动关闭")
                )
                with _CACHE_LOCK:
                    _CACHE[site] = {
                        "site": site, "browser_logged_in": True, "confirmed": True,
                        "status": "logged_in", "account": _state_account(state),
                        "risk": risk, "detail": detail, "keep_open": keep_open,
                        "checked_at": datetime.now().isoformat(timespec="seconds"),
                        "channel": detect_channel(cfg.browser.channel) or "chromium",
                    }
                if on_state:
                    try:
                        on_state({
                            "state": "logged_in",
                            "status": "logged_in",
                            "detail": detail,
                            "removed_cookies": removed,
                            "waited_seconds": waited,
                            "keep_open": keep_open,
                        })
                    except Exception:
                        pass
                if not keep_open:
                    break
                continue
            detail = _state_detail(state, risk)
            if not hinted and waited >= hint_after:
                hinted = True
                detail = (
                    f"{detail} · 已等待 {waited}s，窗口会一直保持；"
                    + ("登录成功后保持窗口，由你手动关闭"
                       if keep_open else "登录成功后自动关闭，也可以手动关闭")
                )
            if on_state:
                try:
                    on_state({
                        "state": "waiting",
                        "status": _state_status(state),
                        "detail": detail,
                        "keep_open": keep_open,
                        "removed_cookies": removed,
                        "waited_seconds": waited,
                    })
                except Exception:
                    pass

    logged_in = confirmed or bool(state.get("logged_in"))
    if logged_in:
        detail = (
            f"登录成功（{state.get('nickname') or '-'}），窗口已由你手动关闭" if keep_open else
            f"接口确认登录成功（{state.get('source') or '接口'}），窗口已自动关闭"
        )
    elif closed_by_user:
        detail = f"窗口已被手动关闭（等待 {waited}s，未确认登录）"
    else:
        detail = _state_detail(state, risk)
    result = {
        "site": site,
        "logged_in": logged_in,
        # 与 check_login 的字段保持一致：UI / 服务端统一读 browser_logged_in
        "browser_logged_in": logged_in,
        "confirmed": bool(state.get("confirmed")),
        "status": _state_status(state) if not logged_in else "logged_in",
        "account": _state_account(state),
        "detail": detail,
        "risk": risk,
        "removed_cookies": removed,
        "waited_seconds": waited,
        "keep_open": keep_open,
        "auto_closed": logged_in and not keep_open,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "channel": detect_channel(cfg.browser.channel) or "chromium",
    }
    with _CACHE_LOCK:
        _CACHE[site] = result
    return result


def cached_status(site: str) -> dict | None:
    with _CACHE_LOCK:
        return dict(_CACHE[site]) if site in _CACHE else None


def _domain_match(domain: str, site: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return any(d == root or d.endswith("." + root) for root in SITE_COOKIE_ROOTS.get(site, ()))


def logout(site: str, cfg: Config) -> dict:
    """只退出指定站点：删除该站域名下的 cookie 与本地存储，其他站点不受影响。"""
    if site not in SITE_COOKIE_ROOTS:
        raise BrowserError(f"不支持的站点：{site}")
    with _session(cfg, headless=True, timeout_ms=45000) as context:
        page = _first_page(context)
        cookies = context.cookies()
        site_cookies = [c for c in cookies if _domain_match(c.get("domain", ""), site)]
        other_cookies = [c for c in cookies if not _domain_match(c.get("domain", ""), site)]
        cdp = context.new_cdp_session(page)
        deleted = 0
        for c in site_cookies:
            try:
                cdp.send("Network.deleteCookies", {
                    "name": c["name"], "domain": c["domain"], "path": c.get("path") or "/",
                })
                deleted += 1
            except Exception:
                pass
        remaining = [c for c in context.cookies() if _domain_match(c.get("domain", ""), site)]
        if remaining:
            try:
                context.add_cookies([
                    {"name": c["name"], "value": "", "domain": c["domain"],
                     "path": c.get("path") or "/", "expires": 1}
                    for c in remaining
                ])
            except Exception:
                pass
        for origin in SITE_ORIGINS.get(site, ()):
            try:
                cdp.send("Storage.clearDataForOrigin", {
                    "origin": origin,
                    "storageTypes": "local_storage,indexeddb,cache_storage,service_workers",
                })
            except Exception:
                pass
        left = [c for c in context.cookies() if _domain_match(c.get("domain", ""), site)]
        session_left = [c for c in left if c.get("name") in SESSION_COOKIES.get(site, [])]
        result = {
            "site": site,
            "deleted_cookies": deleted,
            "remaining_cookies": len(left),
            "other_site_cookies_before": len(other_cookies),
            "other_site_cookies_after": len([c for c in context.cookies()
                                             if not _domain_match(c.get("domain", ""), site)]),
            "logged_out": not session_left,
            "detail": (
                "已清理该站点登录会话（其他站点未受影响）" if not session_left else
                "已删除登录 cookie，但仍有残留，请在该站点窗口内手动退出"
            ),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        with _CACHE_LOCK:
            _CACHE[site] = {
                "site": site,
                "browser_logged_in": False,
                "confirmed": True,
                "status": "logged_out",
                "account": {"nickname": "", "uid": "", "source": ""},
                "risk": False,
                "detail": result["detail"],
                "checked_at": result["checked_at"],
                "channel": detect_channel(cfg.browser.channel) or "chromium",
            }
        return result


# ---------------- 搜索 ----------------
def _scroll(page, times: int) -> None:
    for _ in range(max(1, times)):
        try:
            page.mouse.wheel(0, 900)
        except Exception:
            pass
        page.wait_for_timeout(700)


def _xhs_valid_image(url: str) -> bool:
    """排除头像、预览占位等非照片资源（spectrum 是小红书的正常图片路径，不能排除）。"""
    if not url or not url.startswith("http"):
        return False
    if not ("xhscdn.com" in url or "sns-webpic" in url):
        return False
    bad = ("notes_pre_p", "avatar", "fe-static", "picasso-static", "fe-platform")
    return not any(x in url for x in bad)


def _xhs_cards(page) -> list[dict]:
    """解析搜索结果卡片：按笔记 id 去重，优先带 xsec_token 的详情链接。"""
    js = """
    () => {
      const out = [];
      const seen = new Set();
      document.querySelectorAll('section.note-item, .note-item').forEach(node => {
        const id = node.getAttribute('data-note-id') || '';
        const cover = node.querySelector('a.cover') || node.querySelector('a[href*="xsec_token"]')
          || node.querySelector('a[href*="/explore/"]') || node.querySelector('a');
        const img = node.querySelector('img');
        if (!id && !img) return;
        const pageUrl = cover ? cover.href : '';
        const src = img ? (img.currentSrc || img.src || img.getAttribute('data-src') || '') : '';
        const key = id || pageUrl || src;
        if (!key || seen.has(key)) return;
        seen.add(key);
        const title = img ? (img.alt || '') : '';
        out.push({id, url: src, page: pageUrl, title: (title || node.innerText || '').slice(0, 80)});
      });
      return out;
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception:
        return []


def xhs_scroll_cap(max_pages: int) -> int:
    """小红书没有真正的分页，只有无限滚动：把 `max_pages` 线性映射成滚动轮次预算。"""
    return max(3, min(16, int(max_pages) * 2))


# 最近一次小红书搜索的滚动统计（供上层把"第几轮/共几轮"如实展示）
LAST_XHS_SCROLL: dict = {}

# 最近一次详情页展开得到的「笔记 → 图片清单」：用于记录每篇笔记有多少张图、哪些已入库
LAST_XHS_NOTE_MEDIA: dict = {}


def _xhs_collect(page, max_scrolls: int) -> tuple[list[dict], dict]:
    """滚动加载直到连续两轮没有新增笔记（应对虚拟列表：按笔记 id 累积）。

    返回 (笔记列表, 统计)；每张卡片带 `round` = 首次出现的滚动轮次（1 起）。
    """
    notes: dict[str, dict] = {}
    idle = 0
    rounds = 0
    stop = "cap"
    for i in range(max(1, max_scrolls)):
        rounds = i + 1
        cards = _xhs_cards(page)
        before = len(notes)
        for card in cards:
            key = card.get("id") or card.get("page") or card.get("url")
            if key and key not in notes:
                card = dict(card)
                card["round"] = rounds
                notes[key] = card
        idle = idle + 1 if len(notes) == before else 0
        if i >= 3 and idle >= 2:
            stop = "idle"
            break
        try:
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.mouse.wheel(0, 1200)
        except Exception:
            pass
        page.wait_for_timeout(1100 + random.random() * 400)
    stats = {"rounds": rounds, "cap": max(1, max_scrolls), "notes": len(notes), "stop": stop}
    return list(notes.values()), stats


def _https(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _xhs_note_originals(context, url: str) -> tuple[list[dict], dict]:
    """打开笔记详情页，取图集原图（优先 ORIGIN，其次 WB_DFT 最大变体，最后 DOM）。

    返回 (图片列表, 元信息)；元信息里的 image_list = 页面声明的图集张数（解析成功时可靠），
    dom = 是否退回到 DOM 兜底（此时 image_list 可能为 0，不能当作「本篇全部图片」）。
    """
    page = None
    try:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(2000)
        if _risk_detected(page):
            raise RiskControlError("小红书详情页出现验证码/风控拦截，已停止该站点任务。")
        rows = page.evaluate("""
        () => {
          const unwrap = (v) => {
            if (v && typeof v === 'object' && ('__v_isRef' in v)) {
              return v._rawValue !== undefined ? v._rawValue : v._value;
            }
            return v;
          };
          const st = window.__INITIAL_STATE__ || {};
          const note = unwrap(st.note) || {};
          const map = unwrap(note.noteDetailMap) || {};
          let entry = null;
          for (const k of Object.keys(map)) {
            const e = unwrap(map[k]);
            if (e && e.note) { entry = e; break; }
          }
          const n = entry ? (unwrap(entry.note) || {}) : {};
          const imgs = unwrap(n.imageList) || [];
          const out = [];
          if (Array.isArray(imgs)) {
            for (const x of imgs) {
              const u = unwrap(x) || {};
              const infos = (unwrap(u.infoList) || []).map(i => unwrap(i) || {});
              const pick = (scene) => {
                const it = infos.find(i => i.imageScene === scene);
                return it ? String(it.url || '') : '';
              };
              out.push({
                origin: pick('ORIGIN'),
                dft: pick('WB_DFT') || String(u.urlDefault || ''),
                prv: pick('WB_PRV') || String(u.urlPre || ''),
                w: u.width || 0, h: u.height || 0,
                scenes: infos.map(i => i.imageScene).join(','),
              });
            }
          }
          return out;
        }
        """) or []
        out: list[dict] = []
        for r in rows:
            for variant, key in (("origin", "origin"), ("default", "dft"), ("preview", "prv")):
                u = _https(str(r.get(key) or ""))
                if _xhs_valid_image(u):
                    out.append({"url": u, "variant": variant,
                                "width": int(r.get("w") or 0), "height": int(r.get("h") or 0)})
                    break
        dom_used = 0
        if not out:
            dom_used = 1
            dom = page.evaluate("""
            () => {
              const box = document.querySelector('#noteContainer, .note-container, .note-scroller');
              const root = box || document.body;
              const out = [];
              const seen = new Set();
              const ok = (s) => {
                if (!s) return false;
                if (!(s.includes('xhscdn.com') || s.includes('sns-webpic'))) return false;
                return !/avatar|fe-static|picasso-static|fe-platform|profile|notes_pre_p/.test(s);
              };
              root.querySelectorAll('.swiper-slide img, .note-slider img, .media-container img, img').forEach(img => {
                const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                if (!ok(src) || seen.has(src)) return;
                seen.add(src);
                out.push(src);
              });
              root.querySelectorAll('video[poster]').forEach(v => {
                const src = v.getAttribute('poster') || '';
                if (ok(src) && !seen.has(src)) { seen.add(src); out.push(src); }
              });
              return out;
            }
            """) or []
            for u in dom:
                u = _https(u)
                if _xhs_valid_image(u):
                    out.append({"url": u, "variant": "dom", "width": 0, "height": 0})
        meta = {"image_list": len(rows), "parsed": len(out), "dom": dom_used}
        return out, meta
    except RiskControlError:
        raise
    except Exception:
        return [], {"image_list": 0, "parsed": 0, "dom": 0}
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def expand_xhs_notes(cfg: Config, notes: list[SearchItem], budget: int) -> tuple[list[SearchItem], dict[str, int]]:
    """只对准备下载的笔记开详情页取原图；失败回退封面并标记 fallback_cover。

    调用方按「count 缺口」给出 budget，避免展开全部候选；详情页之间保留随机延迟，
    一旦出现验证码/风控立即抛出 RiskControlError，由上层停止该站点。
    """
    stats = {"notes": len(notes), "detail_pages": 0, "images": 0, "fallback_cover": 0,
             "image_list": 0, "dom_fallback": 0}
    out: list[SearchItem] = []
    seen: set[str] = set()
    LAST_XHS_NOTE_MEDIA.clear()
    with _session(cfg, headless=cfg.browser.headless) as context:
        _inject_cookies(context, "xiaohongshu", cfg)
        for note in notes:
            if stats["detail_pages"] >= max(0, int(budget)):
                break
            page_url = note.page_url or ""
            if not page_url or page_url in seen:
                continue
            if "/explore/" not in page_url and "/search_result/" not in page_url:
                continue
            seen.add(page_url)
            stats["detail_pages"] += 1
            media, meta = _xhs_note_originals(context, page_url)
            stats["image_list"] += int(meta.get("image_list") or 0)
            if media:
                if meta.get("dom"):
                    stats["dom_fallback"] += 1
                for m in media:
                    out.append(SearchItem(
                        url=m["url"], source="xiaohongshu", query=note.query,
                        title=note.title or note.query, page_url=page_url,
                        width=int(m.get("width") or 0), height=int(m.get("height") or 0),
                        variant=str(m.get("variant") or ""),
                    ))
                stats["images"] += len(media)
                # 只有 imageList 解析成功时，张数才是「本篇全部图片」，可据此做整篇跳过
                nk = _note_key(page_url)
                declared = int(meta.get("image_list") or 0)
                if nk and declared > 0:
                    LAST_XHS_NOTE_MEDIA[nk] = {
                        "page_url": page_url,
                        "total": declared,
                        "images": [{"url": m["url"], "key": canonical_url(m["url"])} for m in media],
                    }
            elif note.url:
                stats["fallback_cover"] += 1
                out.append(SearchItem(
                    url=note.url, source="xiaohongshu", query=note.query,
                    title=note.title or note.query, page_url=page_url, variant="cover",
                    fallback_cover=True,
                ))
            time.sleep(1.5 + random.random() * 2.0)
    return out, stats


def search_xiaohongshu(cfg: Config, query: str, *, max_pages: int = 3,
                       page_size: int = 30) -> list[SearchItem]:
    # 搜索页只负责「发现笔记」：滚动累积笔记卡片，封面仅用于去重/兜底，不作下载源
    # 候选池要远大于单轮详情页预算：整篇已入库的笔记会被跳过，需要更深处的笔记补位
    max_notes = max(20, min(160, max(page_size, 30) * 4))
    max_scrolls = xhs_scroll_cap(max_pages)
    with _session(cfg, headless=cfg.browser.headless) as context:
        _inject_cookies(context, "xiaohongshu", cfg)
        page = _first_page(context)
        url = "https://www.xiaohongshu.com/search_result?keyword=" + urllib.parse.quote(query)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
        except Exception as exc:
            raise BrowserError(f"打开小红书搜索页失败：{exc}") from exc
        page.wait_for_timeout(2500)
        if _risk_detected(page):
            raise RiskControlError("小红书出现验证码/滑块/风控拦截，已停止该站点任务，请手动完成验证后重试。")
        state = _xhs_state(page)
        if state.get("confirmed") and not state.get("logged_in"):
            raise LoginRequiredError(
                "小红书未登录：请点击“打开登录窗口”扫码登录一次（会复用同一浏览器会话）。"
            )
        notes, scroll_stats = _xhs_collect(page, max_scrolls)
        LAST_XHS_SCROLL.clear()
        LAST_XHS_SCROLL.update(scroll_stats)
        notes = notes[:max_notes]
        if not notes:
            raise BrowserError("小红书搜索页未解析到笔记（页面结构可能变化或需要登录）")

        items: list[SearchItem] = []
        seen: set[str] = set()

        def add(item_url: str, page_url: str, title: str, round_no: int = 1) -> bool:
            if not _xhs_valid_image(item_url) or item_url in seen:
                return False
            seen.add(item_url)
            items.append(SearchItem(
                url=item_url, source="xiaohongshu", query=query,
                title=title or query, page_url=page_url, variant="cover",
                page=max(1, int(round_no or 1)),
            ))
            return True

        for note in notes:
            add(note.get("url") or "", note.get("page") or "", note.get("title") or "",
                note.get("round") or 1)
        return items


def _weibo_cards(page) -> list[dict]:
    js = """
    () => {
      const out = [];
      const seen = new Set();
      document.querySelectorAll('img').forEach(img => {
        let src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
        if (!src.includes('sinaimg.cn')) return;
        if (!(src.includes('/orj360/') || src.includes('/mw690/') || src.includes('/large/') ||
              src.includes('/thumb150/') || src.includes('/bmiddle/'))) return;
        if (seen.has(src)) return;
        seen.add(src);
        const a = img.closest('a');
        const box = img.closest('.card, .card-wrap, article, .m-panel');
        const title = (box ? box.innerText : img.alt || '').slice(0, 80);
        out.push({url: src, page: a ? a.href : '', title});
      });
      return out;
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception:
        return []


def _weibo_large(url: str) -> str:
    for part in ("orj360", "thumb150", "bmiddle", "mw690"):
        if f"/{part}/" in url:
            return url.replace(f"/{part}/", "/large/")
    return url


def _weibo_pics(mblog: dict) -> list[str]:
    out = []
    for pic in mblog.get("pics") or []:
        url = (pic.get("large") or {}).get("url") or pic.get("url") or ""
        if url:
            out.append(_weibo_large(url))
    return out


def search_weibo(cfg: Config, query: str, *, max_pages: int = 3, page_size: int = 30) -> list[SearchItem]:
    # m.weibo.cn 搜索接口每页约 10-16 条博文、可连续翻页；比 s.weibo.com 首屏 DOM 稳定得多。
    max_items = min(200, max(60, page_size) * max(1, min(max_pages, 6)))
    with _session(cfg, headless=cfg.browser.headless) as context:
        _inject_cookies(context, "weibo", cfg)
        page = _first_page(context)
        try:
            page.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=40000)
        except Exception:
            page = None
        if page is not None:
            page.wait_for_timeout(1200)
            state = _weibo_api_state(context)
            if state.get("confirmed") and not state.get("logged_in"):
                raise LoginRequiredError(
                    "微博未登录：请点击“打开登录窗口”登录一次（会复用同一浏览器会话）。"
                )
            if _risk_detected(page):
                raise RiskControlError("微博出现验证码/滑块/风控拦截，已停止该站点任务，请手动完成验证后重试。")

        items: list[SearchItem] = []
        seen: set[str] = set()

        def add(url: str, page_url: str, title: str) -> None:
            if not url or url in seen or len(items) >= max_items:
                return
            seen.add(url)
            items.append(SearchItem(url=url, source="weibo", query=query,
                                    title=title or query, page_url=page_url))

        def fetch_one(q: str, pages: int) -> int:
            """请求一页搜索接口，返回新增图片数；异常时抛出，由外层兜底。"""
            container = "100103type=1&q=" + urllib.parse.quote(q)
            idle = 0
            for p in range(1, max(1, pages) + 1):
                api = ("https://m.weibo.cn/api/container/getIndex?containerid="
                       + urllib.parse.quote(container) + "&page_type=searchall&page=" + str(p))
                resp = context.request.get(
                    api, headers={"Referer": "https://m.weibo.cn/"}, timeout=15000,
                )
                if resp.status in (403, 418, 429):
                    raise RiskControlError(
                        f"微博搜索接口返回 {resp.status}（疑似风控），已停止该站点任务。"
                    )
                try:
                    data = resp.json()
                except Exception:
                    break
                if not data.get("ok"):
                    msg = str(data.get("msg") or "")
                    if "验证" in msg or "频繁" in msg or "登录" in msg:
                        raise RiskControlError(f"微博接口提示：{msg}，已停止该站点任务。")
                    break
                cards = (data.get("data") or {}).get("cards") or []
                mblogs = []
                for card in cards:
                    if card.get("mblog"):
                        mblogs.append(card["mblog"])
                    for sub in card.get("card_group") or []:
                        if sub.get("mblog"):
                            mblogs.append(sub["mblog"])
                before = len(seen)
                for mb in mblogs:
                    for url in _weibo_pics(mb):
                        add(url, "", (mb.get("text") or "").strip()[:80])
                fresh = len(seen) - before
                idle = idle + 1 if fresh == 0 else 0
                if not cards or idle >= 2:
                    break
                time.sleep(0.6 + random.random() * 0.8)
            return len(items)

        try:
            fetch_one(query, max_pages)
            # 关键词覆盖：加一组「人名 图集」，按半额页数，已有结果时仍继续补量
            extra = f"{query} 图集"
            if extra != query and len(query) <= 24 and len(items) < max_items:
                fetch_one(extra, max(1, max_pages // 2))
        except RiskControlError:
            raise
        except Exception:
            pass

        if items:
            return items

        # 兜底：m.weibo.cn 接口不可用时回到 s.weibo.com 首屏 DOM
        if page is None:
            raise BrowserError("微博搜索失败：无法打开 m.weibo.cn，也未取得 DOM 兜底页面。")
        try:
            page.goto("https://s.weibo.com/weibo?q=" + urllib.parse.quote(query),
                      wait_until="domcontentloaded", timeout=40000)
        except Exception as exc:
            raise BrowserError(f"打开微博搜索页失败：{exc}") from exc
        page.wait_for_timeout(2000)
        if "passport.weibo.com" in page.url or "login" in page.url:
            raise LoginRequiredError(
                "微博未登录：请点击“打开登录窗口”登录一次，或在 secrets/weibo.txt 提供 Cookie。"
            )
        if _risk_detected(page):
            raise RiskControlError("微博出现验证码/滑块/风控拦截，已停止该站点任务，请手动完成验证后重试。")
        _scroll(page, min(8, max(2, max_pages)))
        cards = _weibo_cards(page)
        if not cards:
            raise BrowserError("微博搜索页未解析到图片（可能被风控或页面结构变化）")
        for card in cards:
            if len(items) >= max_items:
                break
            raw = card.get("url") or ""
            if not raw:
                continue
            big = _weibo_large(raw)
            add(big, card.get("page") or "", card.get("title") or "")
            if big != raw:
                seen.add(raw)
        return items


def search(site: str, cfg: Config, query: str, *, max_pages: int = 3, page_size: int = 30) -> list[SearchItem]:
    if site == "xiaohongshu":
        return search_xiaohongshu(cfg, query, max_pages=max_pages, page_size=page_size)
    if site == "weibo":
        return search_weibo(cfg, query, max_pages=max_pages, page_size=page_size)
    raise BrowserError(f"浏览器搜索不支持站点：{site}")


def info(cfg: Config) -> dict[str, Any]:
    iso = isolation_status(cfg)
    return {
        "playwright_installed": playwright_installed(),
        "channel": detect_channel(cfg.browser.channel) or "bundled-chromium",
        "profile_dir": iso["profile_dir"],
        "profile_isolated": iso["isolated"],
        "default_profiles": iso["default_profiles"],
        "isolated_note": iso["note"],
        "cookie_dir": str(cfg.secrets_path),
    }
