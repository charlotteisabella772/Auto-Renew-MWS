#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import subprocess
import base64
import urllib.parse
from datetime import datetime, timezone, timedelta
from curl_cffi import requests

# ================= 环境变量配置(可私库直接填写在双引号里) =================
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱仅通知使用，可随意填写
SERVER_IDS    = os.environ.get("SERVER_IDS") or ""      # server id，多个用逗号分隔
COOKIE        = os.environ.get("COOKIE") or ""          # __Host-mrtcloud_token 的值，有效期约1个月，失效后自动用 Discord 登录刷新
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token，COOKIE 失效时用它自动重新登录
GH_TOKEN      = os.environ.get("GH_TOKEN") or ""        # GitHub PAT，用于把新 COOKIE 写回仓库 Secrets（留空则跳过写入）
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG 通知，可选
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG 通知，可选

# ------------- 代理配置 -------------
IS_PROXY     = os.environ.get("IS_PROXY", "false").lower() == "true"   # 是否启用代理, 节点配置NODE_LINK节点自动开启
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1081"

# ------------- MWS 站点 / Discord OAuth 配置 -------------
SITE_ORIGIN = "https://cloud.m-ws.cc"          # 控制台前端（续期 API 入口）
API_ORIGIN  = "https://cloud-api.m-ws.cc"      # 后端 auth 所在域
COOKIE_NAME = "__Host-mrtcloud_token"
DISCORD_CLIENT_ID     = "1508034084377464903"
OAUTH_REDIRECT_URI    = f"{API_ORIGIN}/auth/callback"
OAUTH_SCOPE           = "identify"
DISCORD_AUTHORIZE_API = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# ==========================================
def build_proxies():
    """按配置返回 curl_cffi 需要的代理字典，未启用则返回 None"""
    if IS_PROXY:
        return {"http": PROXY_SERVER, "https": PROXY_SERVER}
    return None

def send_tg_notification(message: str):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 TG 通知。")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}

    try:
        resp = requests.post(
            url, json=payload, timeout=10, impersonate="chrome", proxies=build_proxies()
        )
        if resp.status_code == 200:
            print("✅ TG 通知发送成功")
        else:
            print(f"❌ TG 通知发送失败: {resp.text}")
    except Exception as e:
        print(f"❌ TG 通知发送异常: {e}")

def mask_secret(value: str) -> str:
    if not value:
        return "***"
    return value[:4] + "..." + value[-4:] if len(value) > 8 else "***"

def update_github_secret(secret_name: str, new_value: str) -> bool:
    """调用 gh cli 更新仓库 Secrets；未配置 GH_TOKEN 时直接跳过"""
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    if not GH_TOKEN:
        print(f"ℹ️ 未配置 GH_TOKEN，跳过写入 {secret_name}（新值: {mask_secret(new_value)}）")
        return False

    print(f"🔄 更新 Secret: {secret_name} (新值: {mask_secret(new_value)})")
    try:
        env = os.environ.copy()
        env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_value],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
        if proc.returncode == 0:
            print(f"✅ Secret {secret_name} 更新成功")
            return True
        print(f"❌ 更新 Secret {secret_name} 失败: {proc.stderr.strip()}")
        return False
    except Exception as e:
        print(f"❌ 更新 Secret {secret_name} 异常: {e}")
        return False

def login_headers():
    """带 COOKIE 的 API 请求头"""
    return {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "Cookie": f"{COOKIE_NAME}={COOKIE}",
        "Origin": SITE_ORIGIN,
        "Referer": f"{SITE_ORIGIN}/",
    }

def get_expiry_from_cookie(value: str):
    """从 JWT 里解析过期时间（JST 展示），解析失败返回 None"""
    try:
        payload_b64 = value.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not exp:
            return None
        # 站点是日本服务，按 JST (UTC+9) 展示
        return datetime.fromtimestamp(exp, tz=timezone.utc).astimezone(
            timezone(timedelta(hours=9))
        )
    except Exception:
        return None

def renew_server(server_id: str):
    """调用续期 API，返回 (是否成功, HTTP 状态码, 剩余小时数)"""
    url = f"{SITE_ORIGIN}/api/bots/{server_id}/renew"

    try:
        response = requests.post(
            url,
            headers=login_headers(),
            timeout=15,
            impersonate="chrome",
            proxies=build_proxies(),
        )

        if response.status_code == 200:
            try:
                remaining_hours = response.json().get("timer", {}).get("remaining_hours", "未知")
            except json.JSONDecodeError:
                remaining_hours = "未知"

            print(f"✅ 服务器 [{server_id}] 续期成功，剩余 {remaining_hours} 小时")
            return True, response.status_code, remaining_hours

        print(f"❌ 服务器 [{server_id}] 续期失败，状态码: {response.status_code}, 响应: {response.text[:300]}")
        return False, response.status_code, None

    except Exception as e:
        print(f"❌ 服务器 [{server_id}] 请求发生异常, 请检查服务器是否被删除: {e}")
        return False, None, None

def get_server_status_http(server_id: str):
    """用 API 查服务器运行状态，返回 'running' / 'offline' / None（查不到）。
    先试 GET /api/bots/{id}，这个接口不一定存在，查不到就返回 None，不影响主流程，
    真正的状态判断在浏览器里做（页面正文里有 Running / Offline）。"""
    for url in (f"{SITE_ORIGIN}/api/bots/{server_id}", f"{API_ORIGIN}/api/bots/{server_id}"):
        try:
            resp = requests.get(url, headers=login_headers(), timeout=15, impersonate="chrome", proxies=build_proxies())
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            # 兼容几种可能的字段：data.status / status / state / online / running
            raw = ""
            if isinstance(data, dict):
                for key in ("status", "state", "running_status"):
                    if key in data:
                        raw = str(data[key]); break
                if not raw and isinstance(data.get("data"), dict):
                    for key in ("status", "state"):
                        if key in data["data"]:
                            raw = str(data["data"][key]); break
                if not raw:
                    for key in ("online", "running", "is_running"):
                        if key in data:
                            raw = "running" if data[key] else "offline"; break
            print(f"🔍 [{server_id}] API 状态查询 {url}: {str(data)[:300]}")
            low = raw.lower()
            if "run" in low or low in ("online", "up", "active", "1", "true"):
                return "running"
            if any(w in low for w in ("off", "stop", "dead", "exit", "down", "0", "false")):
                return "offline"
            return raw or None
        except Exception as e:
            print(f"🔍 [{server_id}] API 状态查询异常: {e}")
            continue
    return None

# ================= Discord OAuth 登录（API 请求失败时的兜底，纯 HTTP） =================
#   1) GET /auth/login 拿授权 URL（含 state），Session 自动种下 oauth_state cookie
#   2) DISCORD_TOKEN 调 Discord OAuth2 API 授权，拿回带 code 的回调 location
#   3) GET 回调 -> 302 到 /auth/success?code=<一次性code>
#   4) POST /api/auth/exchange 用一次性 code 换取新 __Host-mrtcloud_token

STATE_RE = re.compile(r"[?&]state=([^&]+)")

def discord_authorize(state: str) -> str:
    """用 DISCORD_TOKEN 请求 Discord OAuth2 API 授权，返回带 code 的回调 location"""
    query = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope":         OAUTH_SCOPE,
        "state":         state,
    })
    authorize_url = f"{DISCORD_AUTHORIZE_API}?{query}"
    referer = f"https://discord.com/oauth2/authorize?{query}"

    headers = {
        "accept":           "*/*",
        "authorization":    DISCORD_TOKEN,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "ja",
        "x-super-properties": base64.b64encode(json.dumps({
            "os": "Windows",
            "browser": "Chrome",
            "device": "",
            "system_locale": "ja-JP",
            "browser_version": "140.0.0.0",
            "os_version": "10",
        }).encode()).decode(),
    }

    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })

    data = None

    # 途径1：curl_cffi 直连（CI/有代理环境通常可用）
    try:
        resp = requests.post(
            authorize_url,
            headers=headers,
            data=body,
            timeout=12,
            impersonate="chrome",
            proxies=build_proxies(),
        )
        if resp.status_code == 200:
            data = resp.json()
        else:
            print(f"⚠️ Discord OAuth2 直连返回 HTTP {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"⚠️ Discord OAuth2 直连失败: {e}")

    if not data:
        print("❌ Discord OAuth2 授权失败，请检查 DISCORD_TOKEN 或网络")
        return ""

    location = data.get("location", "")
    if not location:
        print(f"❌ 授权响应没有 location 字段: {data}")
        return ""

    print(f"✅ 拿到回调 URL: {re.sub(r'code=[^&]+', 'code=***', location)}")
    return location

def fetch_new_cookie_via_http() -> str:
    """纯 HTTP 完成 Discord OAuth 授权拿cookie：
    1) GET /auth/login 拿 302 -> discord 授权页 URL（含 state），Session 自动种下 oauth_state cookie
    2) DISCORD_TOKEN 调 Discord OAuth2 API 授权 -> 带 code 的回调 location
    3) GET 回调 -> 302 到 /auth/success?code=<一次性code>（Session 自动带 state cookie）
    4) POST /api/auth/exchange 用一次性 code 换取新 __Host-mrtcloud_token
    curl_cffi 的 chrome TLS 指纹真实，不会被 Cloudflare 掐断。"""
    if not DISCORD_TOKEN:
        print("❌ 未配置 DISCORD_TOKEN，无法自动重新登录")
        return ""

    print("\n🔑 COOKIE 续期失败，切换到Discord OAuth登录获取新的Cookie...")

    try:
        sess = requests.Session(impersonate="chrome")
        if IS_PROXY:
            sess.proxies = build_proxies()

        # 1) 触发 /auth/login，取授权 URL（含后端种下的 state）
        resp = sess.get(f"{API_ORIGIN}/auth/login", timeout=20, allow_redirects=False)
        authorize_page = resp.headers.get("location", "")
        if resp.status_code != 302 or not authorize_page:
            print(f"❌ /auth/login 未返回授权跳转: HTTP {resp.status_code} {resp.text[:150]}")
            return ""
        print(f"✅ 拿到 Discord 授权入口: {re.sub(r'state=[^&]+', 'state=***', authorize_page)}")

        m = STATE_RE.search(authorize_page) or STATE_RE.search(urllib.parse.unquote(authorize_page))
        if not m:
            print("❌ 授权 URL 中未找到 state")
            return ""
        state = urllib.parse.unquote(m.group(1))

        # 2) Discord 侧授权（带 DISCORD_TOKEN 的 API 调用）
        location = discord_authorize(state)
        if not location:
            return ""

        # 3) 打开回调，Session 会自动带上 oauth_state cookie。
        #    成功时后端 302 到 cloud.m-ws.cc/auth/success?code=<一次性code>（不直接发 token）
        print("↩️ 携带授权码请求回调...")
        cb = sess.get(location, timeout=20, allow_redirects=False)
        # print(f"↩️ 回调响应: HTTP {cb.status_code}")

        success_code = ""
        if cb.status_code == 302:
            loc = cb.headers.get("location", "")
            if "/auth/success" in loc:
                mm = re.search(r"[?&]code=([^&]+)", loc)
                if mm:
                    success_code = urllib.parse.unquote(mm.group(1))
            else:
                print(f"   回调跳转到: {loc[:120]}")
        else:
            print(f"   回调返回异常状态: {cb.text[:150]}")

        if not success_code:
            print("❌ 回调未返回 auth/success 换取码（可能 state 校验失败或 code 无效）")
            return ""

        # 4) 模拟前端 auth/success 页面：用一次性 code 调 exchange 换真正的 token
        print("🔄 用一次性 code 调 /api/auth/exchange 换取 token ...")
        ex = sess.post(
            f"{SITE_ORIGIN}/api/auth/exchange",
            json={"code": success_code},
            headers={"Content-Type": "application/json", "Referer": f"{SITE_ORIGIN}/auth/success"},
            timeout=20,
        )
        if ex.status_code != 200:
            print(f"❌ exchange 失败: HTTP {ex.status_code} - {ex.text[:200]}")
            return ""

        token = ""
        for ck in sess.cookies.jar:
            if ck.name == COOKIE_NAME:
                token = ck.value
                break
        if not token:
            set_cookie = ex.headers.get("set-cookie", "")
            mm = re.search(rf"{re.escape(COOKIE_NAME)}=([^;]+)", set_cookie)
            if mm:
                token = urllib.parse.unquote(mm.group(1))

        if not token:
            print(f"❌ exchange 响应未包含 {COOKIE_NAME}（HTTP {ex.status_code}，body: {ex.text[:150]}）")
            return ""

        expiry = get_expiry_from_cookie(token)
        if expiry:
            print(f"📅 新 COOKIE 到期时间: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"✅ HTTP 登录成功，拿到新的 {COOKIE_NAME}: {mask_secret(token)}")
        return token
    except Exception as e:
        print(f"❌ HTTP OAuth 登录异常: {e}")
        return ""


# ================= 浏览器兜底：captcha_required 时用真浏览器点“重置计时器” =================
# 原理和 HidenCloud 脚本一样：Playwright/Patchright + 代理 + ignore_https_errors，
# 用 COOKIE 进 https://cloud.m-ws.cc/bots/<id>，点“重置计时器”，再点 Cloudflare Turnstile 复选框。
# Turnstile 是“请验证您是真人”那个复选框，不是选图验证码，真浏览器 + 正常代理一般能自动过。
CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'

def _browser_turnstile_token(page):
    try:
        return page.evaluate(
            """() => {
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        let t = null;
                        try { t = window.turnstile.getResponse(); } catch (e) {}
                        if (t && t.length > 30) return t;
                    }
                } catch (e) {}
                const inputs = document.querySelectorAll('input[name="cf-turnstile-response"], input[id$="_response"]');
                for (const el of inputs) { if (el.value && el.value.length > 30) return el.value; }
                return null;
            }"""
        )
    except Exception:
        return None

def _browser_click_turnstile(page, timeout=90):
    """在 Turnstile 弹窗里点复选框，成功返回 True"""
    import time as _time, random as _random
    start = _time.time()
    clicks = 0
    last_click = 0
    while _time.time() - start < timeout:
        if _browser_turnstile_token(page):
            print("✅ Turnstile 验证通过！")
            return True
        frames = []
        try:
            for h in page.query_selector_all(CF_IFRAME_SELECTOR):
                try:
                    box = h.bounding_box()
                    if box and box['width'] > 10 and box['height'] > 10:
                        frames.append(h)
                except Exception:
                    continue
        except Exception:
            pass
        if not frames:
            _time.sleep(1)
            continue
        # 复选框是否还在
        visible = False
        for frame in page.frames:
            if 'challenges.cloudflare.com' not in (frame.url or ''):
                continue
            try:
                if frame.locator('input[type="checkbox"]:visible').count() > 0:
                    visible = True
                    break
            except Exception:
                continue
        if clicks > 0 and _time.time() - last_click > 4 and not visible:
            print("✅ Turnstile 复选框已消失，视为验证通过！")
            return True
        if _time.time() - last_click > 6 and visible and frames:
            print(f"🖱️ 点击 Turnstile 复选框（第 {clicks + 1} 次）...")
            try:
                for frame in page.frames:
                    if 'challenges.cloudflare.com' not in (frame.url or ''):
                        continue
                    try:
                        frame.locator('input[type="checkbox"]').first.click(timeout=2500)
                        clicks += 1
                        last_click = _time.time()
                        break
                    except Exception:
                        continue
                else:
                    # 坐标点击兜底
                    box = frames[-1].bounding_box()
                    if box:
                        x = box['x'] + _random.uniform(18, 34)
                        y = box['y'] + box['height'] / 2
                        page.mouse.click(x, y)
                        clicks += 1
                        last_click = _time.time()
            except Exception:
                pass
            _time.sleep(3)
            continue
        _time.sleep(1)
    return bool(_browser_turnstile_token(page))

def _browser_check_and_restart(page) -> str:
    """在浏览器里看服务器是不是 Running，不是就点 Restart。
    返回 'running' / 'offline' / None（没识别到状态）。"""
    try:
        body = page.locator("body").inner_text(timeout=5000) or ""
    except Exception:
        return None
    if "Running" in body:
        print("✅ 服务器运行中，无需重启")
        return "running"
    offline_words = ("Offline", "Stopped", "Not running", "离线", "已停止", "已离线")
    if not any(w in body for w in offline_words):
        print("🔍 页面里没看到 Running，也没看到 Offline/Stopped，跳过重启判断")
        return None
    print("⚠️ 服务器离线，尝试点 Restart 重启...")
    try:
        restart_btn = page.locator('button:has-text("Restart"), button:has-text("重启")').first
        restart_btn.wait_for(state="visible", timeout=15000)
        restart_btn.click(timeout=10000)
    except Exception as e:
        print(f"❌ 没找到/点不了 Restart 按钮: {e}")
        return "offline"
    time.sleep(3)
    # 重启也可能弹出 Turnstile，顺手点掉
    try:
        if page.query_selector_all(CF_IFRAME_SELECTOR):
            _browser_click_turnstile(page, timeout=60)
    except Exception:
        pass
    for _ in range(60):
        time.sleep(1)
        try:
            txt = page.locator("body").inner_text(timeout=5000) or ""
        except Exception:
            txt = ""
        if "Running" in txt:
            print("✅ 重启成功，服务器已回到 Running")
            return "running"
    print("⚠️ 点了 Restart，等了 60 秒还是没看到 Running，请手动去面板看一眼")
    return "offline"

def renew_via_browser(server_id: str) -> bool:
    """用浏览器手动点一次“重置计时器（续期7天）”，成功返回 True"""
    if not COOKIE:
        print("❌ 浏览器续期需要 COOKIE，先走 Discord OAuth 拿新 COOKIE")
        return False
    try:
        from patchright.sync_api import sync_playwright
        using_patchright = True
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright
            using_patchright = False
        except ImportError:
            print("❌ 未安装 playwright / patchright，无法用浏览器续期。请在 workflow 里 pip install patchright")
            return False
    print(f"🌐 浏览器兜底：打开 https://cloud.m-ws.cc/bots/{server_id} 点重置计时器...")
    try:
        with sync_playwright() as pw:
            proxy_arg = {"server": PROXY_SERVER} if IS_PROXY else None
            launch_args = ['--no-sandbox', '--disable-infobars', '--ignore-certificate-errors']
            if using_patchright:
                browser = pw.chromium.launch(channel="chrome", headless=False, args=launch_args, proxy=proxy_arg)
                ctx = browser.new_context(viewport=None, proxy=proxy_arg, ignore_https_errors=True)
            else:
                browser = pw.chromium.launch(channel="chrome", headless=False, args=launch_args, proxy=proxy_arg)
                ctx = browser.new_context(viewport={'width': 1920, 'height': 1080}, user_agent=DISCORD_UA, proxy=proxy_arg, ignore_https_errors=True)
            try:
                # __Host- 开头的 Cookie 不能带 Domain 属性，否则浏览器会拒收、登录失败。
                # 只用 url 来设（不给 domain/path），这样才是 host-only Cookie，符合 __Host- 要求。
                # Playwright 规定：url 和 domain/path 二选一，只给 url，让它自己推 domain/path。
                # 上一版同时给了 url 和 path，patchright 校验报 "Cookie should have either url or path"。
                ctx.add_cookies([{
                    'name': COOKIE_NAME, 'value': COOKIE,
                    'url': SITE_ORIGIN,
                    'secure': True, 'httpOnly': True, 'sameSite': 'Lax',
                }])
                page = ctx.new_page()
                page.goto(f"{SITE_ORIGIN}/bots/{server_id}", wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)
                # 调试：打印页面标题和正文前 300 字，看是不是没登录 / 被踢回登录页
                try:
                    print(f"🔍 浏览器当前 URL: {page.url}，标题: {page.title()}")
                    _body = page.locator("body").inner_text(timeout=5000) or ""
                    print(f"🔍 页面正文前 300 字: {_body[:300].replace(chr(10), ' ')}")
                    print(f"🔍 页面里有没有 Reset timer / 重置计时器: {('Reset timer' in _body or '重置计时器' in _body)}，有没有登录提示: {('登录' in _body or 'Sign in' in _body)}")
                except Exception as _e:
                    print(f"🔍 页面信息采集失败: {_e}")
                # 先判运行状态：离线就点 Restart，Running 就直接续期
                _browser_check_and_restart(page)
                # 找到“重置计时器”按钮并点击：页面是英文版，按钮文字是 Reset timer，不是中文“重置计时器”
                # 上次调试日志里正文就是 "Reset timer (extend 7 days)"，所以中文定位不到
                btn = page.locator('button:has-text("Reset timer"), button:has-text("重置计时器")').first
                try:
                    btn.wait_for(state="visible", timeout=30000)
                except Exception:
                    print("❌ 没找到“重置计时器”按钮，可能没登录成功，当前 URL:", page.url)
                    page.screenshot(path="mws_no_renew_btn.png")
                    return False
                # 先记下点击前的计时器（小时数），续期成功后它应该变大 7 天左右
                _initial_hours = None
                try:
                    _t0 = page.locator("body").inner_text(timeout=5000) or ""
                    import re as _re2
                    _m0 = _re2.search(r"(\d+)d\s+(\d+)h\s+left", _t0)
                    if _m0:
                        _initial_hours = int(_m0.group(1)) * 24 + int(_m0.group(2))
                        print(f"🔍 点击前计时器: {_initial_hours} 小时")
                except Exception:
                    pass
                # 先去掉可能挡住按钮的 ncmp 横幅（HidenCloud 同款问题）
                try:
                    page.evaluate("() => { const el = document.getElementById('ncmp__tool'); if (el) el.remove(); }")
                except Exception:
                    pass
                try:
                    btn.click(timeout=10000)
                except Exception:
                    btn.click(timeout=10000, force=True)
                print("🖱️ 已点击“重置计时器”，等待 Turnstile 验证...")
                time.sleep(2)
                # 弹窗里有 Turnstile 就点掉，没有就直接等
                if page.query_selector_all(CF_IFRAME_SELECTOR):
                    if not _browser_click_turnstile(page, timeout=120):
                        print("⚠️ Turnstile 120 秒内未确认通过，可能是代理 IP 被 Cloudflare 标记了，仍继续等页面变化...")
                        try:
                            page.screenshot(path="mws_turnstile_fail.png")
                            print("📸 已保存 Turnstile 失败截图 mws_turnstile_fail.png")
                        except Exception:
                            pass
                # 等计时器变化，最长等 90 秒。页面是英文版，计时器是 "Timer 4d 18h left" / "Timer 11d 18h left"，
                # 不是中文“剩余”，上一版用中文判断永远等不到。
                import re as _re
                def _timer_hours(txt):
                    m = _re.search(r"(\d+)d\s+(\d+)h\s+left", txt)
                    if m:
                        return int(m.group(1)) * 24 + int(m.group(2))
                    return None
                # 只等 90 秒就停，不要在循环里再调 Turnstile（每次 20 秒，90 次就变成半小时，日志刷屏还停不下来）
                for _i in range(90):
                    time.sleep(1)
                    try:
                        txt = page.locator("body").inner_text(timeout=5000)
                    except Exception:
                        txt = ""
                    h = _timer_hours(txt)
                    if h is not None:
                        if _i % 15 == 0 or h != _initial_hours:
                            print(f"🔍 当前计时器: {h} 小时（点击前: {_initial_hours} 小时）")
                        if _initial_hours is None or h > _initial_hours:
                            print("✅ 浏览器续期完成，页面计时器已更新")
                            return True
                print(f"⚠️ 等了 90 秒计时器没变（点击前/当前都是 {_initial_hours} 小时），浏览器续期未确认成功")
                # 刷新一次再看：MWS 是 SPA，续期成功后前端计时器可能不刷新，不刷新看不出来。
                # 截图里没弹窗也一样：弹窗消失可能是通过了，也可能是超时/失败后自动关了，不能单凭没弹窗就认定成功。
                try:
                    print("🔄 刷新页面再确认一次计时器...")
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    time.sleep(3)
                    _txt2 = page.locator("body").inner_text(timeout=5000) or ""
                    _h2 = _timer_hours(_txt2)
                    print(f"🔍 刷新后计时器: {_h2} 小时（点击前: {_initial_hours} 小时）")
                    page.screenshot(path="mws_browser_renew_after_reload.png")
                    if _h2 is not None and _initial_hours is not None and _h2 > _initial_hours:
                        print("✅ 刷新后计时器变大了，浏览器续期成功")
                        return True
                    if _h2 is not None and _h2 >= 168:
                        print("✅ 刷新后计时器已到满格 7 天（168 小时），视为续期成功")
                        return True
                except Exception as _e:
                    print(f"🔍 刷新确认失败: {_e}")
                print("⚠️ 等了 90 秒没确认到计时器更新，请看截图/手动检查")
                try:
                    page.screenshot(path="mws_browser_renew.png")
                except Exception:
                    pass
                return False
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"❌ 浏览器续期异常: {e}")
        return False

# ================= 主流程 =================
def main():
    global COOKIE

    print("#" * 25)
    print("   MWS 自动续期")
    print("#" * 25)

    server_list = [s_id.strip() for s_id in SERVER_IDS.split(",") if s_id.strip()]
    if not server_list:
        print("❌ 未配置有效的 SERVER_IDS")
        return

    if not COOKIE and not DISCORD_TOKEN:
        print("❌ COOKIE 和 DISCORD_TOKEN 均为空，请添加相关变量后再运行！")
        return

    print(f"🚀 开始续期任务，共 {len(server_list)} 个服务器: {server_list}")
    print(f"🔗 使用代理: {PROXY_SERVER}" if IS_PROXY else "🍭 未使用代理，直连访问")

    if COOKIE:
        expiry = get_expiry_from_cookie(COOKIE)
        if expiry:
            print(f"📅 当前 COOKIE 到期时间: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("ℹ️ 未配置 COOKIE，将直接走 Discord 登录流程")

    # ---------- 第一轮：使用现有 COOKIE 调续期 API ----------
    results = {}      
    all_ok = True
    for s_id in server_list:
        ok, status_code, remaining = renew_server(s_id)
        results[s_id] = remaining if ok else None
        if not ok:
            all_ok = False
            if status_code in (401, 403):
                print(f"⚠️ [{s_id}] 认证失败（HTTP {status_code}），COOKIE 可能已失效")
        time.sleep(2)

    new_cookie = ""

    if all_ok:
        print("🏁 全部服务器续期成功，续期任务完成")
        # API 续期成功时也顺手查一下运行状态，离线就走浏览器重启（浏览器里会先 Restart 再续期）
        for s_id in server_list:
            st = get_server_status_http(s_id)
            if st == "offline":
                print(f"⚠️ [{s_id}] API 续期成功但服务器离线，走浏览器重启...")
                renew_via_browser(s_id)
    else:
        # ---------- COOKIE失效走第二轮：浏览器登录 Discord 获取新 COOKIE ----------
        if not DISCORD_TOKEN:
            print("❌ 存在续期失败的服务器，但未配置 DISCORD_TOKEN，无法自动获取新的Cookie")
            send_tg_notification(
                "🇯🇵 MWS 续期通知\n\n"
                f"❌ 续期失败\n👤 账户: {EMAIL}\n"
                "⚠️ COOKIE 可能已失效，且未配置 DISCORD_TOKEN，无法自动刷新。"
            )
            return

        # 纯 HTTP OAuth（curl_cffi chrome TLS 指纹，无需浏览器）
        new_cookie = fetch_new_cookie_via_http()
        if not new_cookie:
            send_tg_notification(
                "🇯🇵 MWS 续期通知\n\n"
                f"❌ 续期失败\n👤 账户: {EMAIL}\n"
                "⚠️ COOKIE 失效且 Discord OAuth 登录失败，请检查 DISCORD_TOKEN 或代理节点。"
            )
            return

        COOKIE = new_cookie

        # ---------- 用新 COOKIE 重新请求续期 API ----------
        print("\n🔁 使用新 COOKIE 重新请求续期 API ...")
        for s_id in server_list:
            print(f"🔁 续期服务器 [{s_id}] ...")
            ok2, status2, remaining2 = renew_server(s_id)
            results[s_id] = remaining2 if ok2 else None
            # 403 captcha_required：API 不让续，改用浏览器点“重置计时器”
            if not ok2 and status2 == 403:
                print(f"⚠️ [{s_id}] API 返回 captcha_required，改用浏览器点击续期...")
                if renew_via_browser(s_id):
                    # 浏览器点完后，再调一次 API 看剩余时间（能拿到就更新 results）
                    ok3, _, remaining3 = renew_server(s_id)
                    # 浏览器已点成功时，即使 API 仍报 captcha，也算成功（计时器已在页面更新）
                    # 浏览器已点成功，API 再调大概率仍报 captcha，这里用 168h（7天）作占位，只表示已续期
                    results[s_id] = remaining3 if remaining3 is not None else 168
                    print(f"✅ [{s_id}] 浏览器续期成功")
            time.sleep(2)

        all_ok = all(v is not None for v in results.values())

    # ---------- 最后统一发送一次 TG 通知 ----------
    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    lines = ["🇯🇵 MWS 续期通知\n"]
    if all_ok:
        lines.append("✅ 全部续期成功")
    else:
        lines.append("⚠️ 部分服务器续期失败，请手动检查")
    for s_id, remaining in results.items():
        if remaining is None:
            lines.append(f"🖥 服务器: [{s_id}] 续期失败")
        else:
            lines.append(f"🖥 服务器: [{s_id}] 剩余 {remaining} H")
    lines.append(f"👤 续期账户: {EMAIL}")
    lines.append(f"⏱️ 运行时间: {current_time}")
    send_tg_notification("\n".join(lines))

    # ---------- 续期完成后：把新 COOKIE 写回 GitHub Secret ----------
    if new_cookie:
        if GH_TOKEN:
            update_github_secret("COOKIE", new_cookie)
        else:
            print("ℹ️ 未配置 GH_TOKEN，跳过写入 COOKIE Secret")
            # print(f"📋 如需手动更新，请将 COOKIE 设置为: {new_cookie}")

    print("🏁 脚本执行完毕")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_tg_notification(
            f"🇯🇵 MWS 续期通知\n\n❌ 脚本异常退出\n👤 账户: {EMAIL}\n⚠️ {e}"
        )
