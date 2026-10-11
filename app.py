# -*- coding: utf-8 -*-
"""band-live — 네이버 밴드 라이브 예배 자동 참석.

매주 정해진 요일·시각에 전용 브라우저(무음)를 띄워 밴드 라이브에 들어가고,
방송이 끝나면 브라우저를 닫는다. 켜고 끄는 것은 관리 화면(:7960)에서 한다.
"""
import base64
import collections
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
import websocket
from flask import Flask, jsonify, request

VERSION = "0.1.0"
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
LOGS = DATA / "logs"
SNAPS = DATA / "snapshots"
PROFILE = DATA / "profile"
CONFIG = DATA / "config.json"
STATE = DATA / "state.json"
HEARTBEAT = DATA / "heartbeat.txt"
SECRETS = Path(os.environ.get("H_SECRETS", r"C:\H-Secrets")) / "band-live" / ".env"
COOKIES = SECRETS.parent / "cookies.json"  # 로그인 쿠키 보관(기밀)

WEB_PORT = 7960
CDP_PORT = 9460
NOWIN = 0x08000000  # CREATE_NO_WINDOW
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

DEFAULTS = {
    "enabled": True,
    "band_name": "세종행복교회",
    "band_no": "",
    "weekday": 4,          # 0=월 … 4=금
    "start": "19:30",      # 예배 시작 시각
    "lead_min": 10,        # 시작 몇 분 전부터 기다릴지
    "wait_min": 40,        # 시작 뒤 몇 분까지 라이브를 기다릴지
    "hard_end": "22:00",   # 무슨 일이 있어도 이 시각엔 끈다
    "skip_date": "",       # 이 날짜(YYYY-MM-DD)는 건너뛴다
    "browser": "edge",     # edge | chrome
    "record": True,        # 화면·구조를 data\snapshots 에 남긴다
    "once": [],            # 한 번만 들어가는 날 [{"date": "2026-10-13", "start": "19:30"}]
}

BROWSERS = {
    "edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
    "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
               r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
}

_lock = threading.RLock()
_recent = collections.deque(maxlen=300)


# ---------------------------------------------------------------- 기록·설정
def log(msg):
    now = dt.datetime.now()
    line = "%s %s" % (now.strftime("%H:%M:%S"), msg)
    with _lock:
        _recent.append(now.strftime("%m-%d ") + line)
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            with open(LOGS / ("band_%s.log" % now.strftime("%Y%m%d")), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def clean_old():
    limit = time.time() - 15 * 86400
    for p in LOGS.glob("band_*.log"):
        if p.stat().st_mtime < limit:
            p.unlink(missing_ok=True)
    snaps = sorted(SNAPS.glob("*"), key=lambda p: p.stat().st_mtime)
    for p in snaps[:-120]:
        p.unlink(missing_ok=True)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_cfg():
    cfg = dict(DEFAULTS)
    cfg.update(read_json(CONFIG, {}))
    return cfg


def save_cfg(changes):
    with _lock:
        cfg = load_cfg()
        cfg.update(changes)
        write_json(CONFIG, cfg)
        return cfg


def load_state():
    return read_json(STATE, {"history": []})


def save_state(changes):
    with _lock:
        st = load_state()
        st.update(changes)
        write_json(STATE, st)
        return st


def add_history(entry):
    with _lock:
        st = load_state()
        hist = st.get("history", [])
        hist.insert(0, entry)
        st["history"] = hist[:30]
        write_json(STATE, st)


def notify(msg, photo=None):
    log("알림: " + msg.replace("\n", " / "))
    env = {}
    try:
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        log("텔레그램 설정 파일 없음: %s" % SECRETS)
        return
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("텔레그램 토큰·chat_id 없음")
        return
    try:
        if photo and Path(photo).exists():
            with open(photo, "rb") as f:
                r = requests.post("https://api.telegram.org/bot%s/sendPhoto" % token,
                                  data={"chat_id": chat, "caption": ("[밴드예배] " + msg)[:1000]},
                                  files={"photo": f}, timeout=30)
            if r.status_code == 200:
                return
            log("텔레그램 사진 전송 실패: HTTP %s → 글만 보냄" % r.status_code)
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                          data={"chat_id": chat, "text": "[밴드예배] " + msg}, timeout=10)
        if r.status_code != 200:
            log("텔레그램 전송 실패: HTTP %s %s" % (r.status_code, r.text[:120]))
    except requests.RequestException as e:
        log("텔레그램 전송 실패: %s" % e)


def hhmm(text, day=None):
    day = day or dt.date.today()
    h, m = [int(x) for x in text.split(":")]
    return dt.datetime.combine(day, dt.time(h, m))


def plan_for(cfg, day):
    """그날 들어갈 일정이 있으면 (시작 시각, 종류). 한 번짜리(once)가 정기보다 먼저다."""
    iso = day.isoformat()
    for o in cfg.get("once", []):
        if o.get("date") == iso:
            return o["start"], "한 번"
    if cfg["enabled"] and day.weekday() == int(cfg["weekday"]) and cfg.get("skip_date") != iso:
        return cfg["start"], "정기"
    return None


def hard_end_for(cfg, start, day=None):
    """상한 시각. 시작이 상한보다 늦거나 바짝 붙은 일정은 시작 3시간 뒤로 잡는다."""
    hard, begin = hhmm(cfg["hard_end"], day), hhmm(start, day)
    return hard if hard > begin + dt.timedelta(minutes=30) else begin + dt.timedelta(hours=3)


def next_service_date(cfg, today=None):
    """다음 정기 예배 날짜(건너뛰기 표시는 화면에서 따로 한다)."""
    today = today or dt.date.today()
    d = today + dt.timedelta(days=(int(cfg["weekday"]) - today.weekday()) % 7)
    if d == today and dt.datetime.now() > hhmm(cfg["start"]) + dt.timedelta(minutes=int(cfg["wait_min"])):
        d += dt.timedelta(days=7)
    return d


def upcoming(cfg, days=14):
    """앞으로 들어갈 일정 목록 [(날짜, 시작, 종류)]"""
    out, today, now = [], dt.date.today(), dt.datetime.now()
    for i in range(days):
        d = today + dt.timedelta(days=i)
        p = plan_for(cfg, d)
        if p and not (i == 0 and now > hhmm(p[0]) + dt.timedelta(minutes=int(cfg["wait_min"]))):
            out.append((d, p[0], p[1]))
    return out


# ---------------------------------------------------------------- 브라우저(CDP)
class CdpError(Exception):
    pass


# 밴드는 방송이 끝나면 alert('라이브 방송이 종료되었습니다.') 를 띄우고 밴드 홈으로 돌려보낸다(2026-10-11 실측).
# alert 가 떠 있는 동안은 화면 읽기(Runtime.evaluate)가 통째로 멈추므로, 보이는 대로 닫고 내용을 남겨 둔다.
DIALOGS = collections.deque(maxlen=20)   # (시각, 내용)


def recent_dialog(sec=120):
    """최근 sec 초 안에 닫은 알림창 내용들"""
    now = time.time()
    return [m for t, m in DIALOGS if now - t <= sec]


def _guard_tab(page, active):
    """탭 하나에 연결을 물고 있다가 알림창이 뜨는 즉시 닫는다.
    알림창이 뜬 '뒤에' 붙은 연결로는 닫을 수 없어서(실측), 미리 물고 있어야 한다."""
    try:
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Page.enable", "params": {}}))
        n = 1
        while True:
            ws.settimeout(60)
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("method") == "Page.javascriptDialogOpening":
                text = msg["params"].get("message", "")
                DIALOGS.append((time.time(), text))
                log("알림창 닫음: %s" % text[:100])
                n += 1
                ws.send(json.dumps({"id": n, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}}))
    except Exception:
        pass   # 탭이나 브라우저가 닫힘
    finally:
        active.discard(page["id"])


def _guard_loop(browser):
    active = set()
    while True:
        try:
            for page in browser.pages():
                if page["id"] not in active and page.get("webSocketDebuggerUrl"):
                    active.add(page["id"])
                    threading.Thread(target=_guard_tab, args=(page, active), daemon=True).start()
        except Exception:
            pass
        time.sleep(1)


class Tab:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        self.n = 0
        if "/devtools/page/" in ws_url:
            # 알림창 뜨는 것을 알려 받으려면 필요. 응답은 기다리지 않는다(막힌 탭에서는 응답이 안 온다)
            self.n += 1
            self.ws.send(json.dumps({"id": self.n, "method": "Page.enable", "params": {}}))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _dialog(self, msg):
        """알림창이 뜨면 닫는다(상시 감시 연결이 먼저 닫았으면 오류 응답이 올 뿐이다)."""
        if msg.get("method") != "Page.javascriptDialogOpening":
            return
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}}))

    def _wait(self, mid, method, timeout):
        end = time.time() + timeout
        while time.time() < end:
            self.ws.settimeout(max(0.2, end - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                break
            self._dialog(msg)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError("%s: %s" % (method, msg["error"].get("message")))
                return msg.get("result", {})
        return None


    def call(self, method, params=None, timeout=15):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        r = self._wait(mid, method, timeout)
        if r is not None:
            return r
        raise CdpError("%s 응답 없음(%d초)" % (method, timeout))

    def js(self, expr, timeout=8):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": True, "userGesture": True}, timeout)
        if r.get("exceptionDetails"):
            raise CdpError("스크립트 오류: %s" % json.dumps(r["exceptionDetails"], ensure_ascii=False)[:300])
        return r.get("result", {}).get("value")

    def goto(self, url, settle=3.0):
        self.call("Page.navigate", {"url": url})
        end = time.time() + 20
        while time.time() < end:
            time.sleep(0.5)
            try:
                if self.js("document.readyState") == "complete":
                    break
            except CdpError:
                pass
        time.sleep(settle)  # 밴드는 화면을 나중에 그린다

    def url(self):
        return self.js("location.href") or ""


class Browser:
    _guard_on = False

    def __init__(self):
        self.base = "http://127.0.0.1:%d" % CDP_PORT

    def guard(self):
        if not Browser._guard_on:
            Browser._guard_on = True
            threading.Thread(target=_guard_loop, args=(self,), daemon=True).start()

    def unstick(self):
        """화면 읽기가 안 되는 탭(알림창 등에 막힘)을 닫고 빈 탭을 새로 연다. 닫은 개수를 돌려준다."""
        closed = 0
        for page in self.pages():
            try:
                with Tab(page["webSocketDebuggerUrl"]) as t:
                    t.js("1", timeout=4)
            except Exception:
                try:
                    requests.put(self.base + "/json/new?about:blank", timeout=3)
                    requests.get(self.base + "/json/close/" + page["id"], timeout=3)
                    closed += 1
                    log("막힌 탭을 닫고 새로 엶: %s" % page.get("url", "")[:80])
                except requests.RequestException as e:
                    log("막힌 탭 닫기 실패: %s" % e)
        return closed

    def exe(self):
        kind = load_cfg().get("browser", "edge")
        for p in BROWSERS.get(kind, []) + BROWSERS["edge"] + BROWSERS["chrome"]:
            if os.path.exists(p):
                return p
        raise RuntimeError("Edge·Chrome 을 찾지 못함")

    def alive(self):
        try:
            return requests.get(self.base + "/json/version", timeout=2).status_code == 200
        except requests.RequestException:
            return False

    def launch(self, url):
        self.guard()
        if self.alive():
            return
        PROFILE.mkdir(parents=True, exist_ok=True)
        args = [self.exe(),
                "--user-data-dir=%s" % PROFILE,
                "--remote-debugging-port=%d" % CDP_PORT,
                "--remote-allow-origins=*",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWinOcclusion",
                "--no-first-run", "--no-default-browser-check",
                "--window-position=40,40", "--window-size=1180,860",
                url]
        subprocess.Popen(args, creationflags=NOWIN)
        end = time.time() + 30
        while time.time() < end:
            if self.alive():
                time.sleep(1.5)
                return
            time.sleep(0.5)
        raise RuntimeError("브라우저가 30초 안에 뜨지 않음")

    def settle_guard(self):
        """새 탭에 감시 연결이 붙을 틈(1초 주기)을 준다."""
        time.sleep(1.5)

    def pages(self):
        try:
            return [t for t in requests.get(self.base + "/json/list", timeout=3).json() if t.get("type") == "page"]
        except (requests.RequestException, ValueError):
            return []

    def tab(self, prefer=None):
        """탭 연결은 쓸 때마다 새로 연다(닫힌 탭 번호를 쥐고 있으면 멈춘다)."""
        pages = self.pages()
        if not pages:
            try:
                requests.put(self.base + "/json/new?about:blank", timeout=3)
            except requests.RequestException:
                pass
            pages = self.pages()
        if not pages:
            raise CdpError("열린 탭이 없음")
        pick = None
        if prefer:
            pick = next((p for p in pages if prefer in p.get("url", "")), None)
        pick = pick or next((p for p in pages if "band.us" in p.get("url", "")), None) or pages[0]
        return Tab(pick["webSocketDebuggerUrl"])

    def close_other_tabs(self, keep_substr):
        for p in self.pages():
            if keep_substr not in p.get("url", ""):
                try:
                    requests.get(self.base + "/json/close/" + p["id"], timeout=3)
                except requests.RequestException:
                    pass

    # 밴드 로그인 쿠키는 '닫으면 지워지는' 종류로 나온다(2026-10-11 실측: 로그인 확인 뒤 창을 닫으니 풀림).
    # 그래서 만료일을 붙여 브라우저에 다시 심고, 파일로도 보관했다가 풀려 있으면 되넣는다.
    def _browser_ws(self):
        return Tab(requests.get(self.base + "/json/version", timeout=3).json()["webSocketDebuggerUrl"])

    @staticmethod
    def _cookie_param(c, keep_until):
        p = {k: c[k] for k in ("name", "value", "domain", "path", "secure", "httpOnly") if k in c}
        if c.get("sameSite"):
            p["sameSite"] = c["sameSite"]
        p["expires"] = keep_until if c.get("session") or c.get("expires", -1) <= 0 else c["expires"]
        return p

    def save_cookies(self):
        """로그인된 상태에서만 부를 것. 저장한 개수를 돌려준다."""
        keep_until = time.time() + 180 * 86400
        with self._browser_ws() as t:
            cs = [c for c in t.call("Storage.getCookies")["cookies"]
                  if "band.us" in c["domain"] or "naver.com" in c["domain"]]
            params = [self._cookie_param(c, keep_until) for c in cs]
            t.call("Storage.setCookies", {"cookies": params})
        COOKIES.parent.mkdir(parents=True, exist_ok=True)
        tmp = COOKIES.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved": dt.datetime.now().isoformat(timespec="seconds"), "cookies": params}),
                       encoding="utf-8")
        os.replace(tmp, COOKIES)
        return len(params)

    def restore_cookies(self):
        data = read_json(COOKIES, None)
        if not data or not data.get("cookies"):
            return 0
        with self._browser_ws() as t:
            t.call("Storage.setCookies", {"cookies": data["cookies"]})
        return len(data["cookies"])

    def close(self):
        """이 프로그램이 띄운 브라우저만 닫는다(디버깅 포트를 쥔 프로세스로 특정)."""
        if self.alive():
            try:
                ws_url = requests.get(self.base + "/json/version", timeout=3).json()["webSocketDebuggerUrl"]
                with Tab(ws_url) as b:
                    b.call("Browser.close", timeout=5)
            except Exception:
                pass
            end = time.time() + 8
            while time.time() < end and self.alive():
                time.sleep(0.5)
        pid = pid_on_port(CDP_PORT)
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NOWIN)


def pid_on_port(port):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                             creationflags=NOWIN, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[3] == "LISTENING" and p[1].endswith(":%d" % port):
            return int(p[4])
    return None


# ---------------------------------------------------------------- 밴드 화면 읽기
JS_TEXT = "(document.body ? (document.body.innerText || document.body.textContent || '') : '')"

# 밴드 홈에서 진행 중인 라이브를 찾는다.
# '라이브 방송' 글자만 보고 누르면 글쓰기 메뉴의 방송 '시작' 단추를 누를 수 있으므로
# 주소에 /live/번호 가 든 링크, 아니면 LIVE 딱지가 붙은 카드만 인정한다.
JS_FIND_LIVE = r"""
(() => {
  const ok = h => /\/(band|page)\/\d+\/live\/\d+/.test(h || '');
  const a = [...document.querySelectorAll('a[href*="/live/"]')].find(x => ok(x.href));
  if (a) return {href: a.href, how: 'link'};
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const mark = (box, how) => {
    document.querySelectorAll('[data-bandlive]').forEach(e => e.removeAttribute('data-bandlive'));
    box.setAttribute('data-bandlive', '1');
    return {href: null, how: how, text: (box.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
            cls: String(box.className).slice(0, 120)};
  };
  // 진행 중인 라이브는 밴드 홈의 전용 영역에 카드로 뜬다(없을 땐 빈 div). 2026-10-11 실제 화면에서 확인
  const card = [...document.querySelectorAll('[data-viewname="DLiveCardView"]')].find(vis);
  if (card) return mark(card.querySelector('.liveVideo') || card.querySelector('.videoArea') || card, 'card');
  const region = [...document.querySelectorAll('._groupBandLiveCardRegion,._pageBandLiveCardRegion')]
    .find(r => r.children.length && vis(r));
  if (region) return mark(region.querySelector('a,button,[role="button"]') || region.firstElementChild, 'card');
  // 피드 안의 라이브 딱지. 끝난 방송 글은 liveState 에 -finished 가 붙고 '종료된 LIVE' 라고 나온다 → 제외
  const st = [...document.querySelectorAll('.liveState')].find(e =>
    vis(e) && !e.classList.contains('-finished') && !/종료/.test(e.textContent || ''));
  if (st) {
    const area = st.closest('.videoArea,.videoSection') || st.parentElement;
    return mark(area.querySelector('.btnLivePlay') || area, 'feed');
  }
  return {href: null, how: null};
})()
"""

JS_CLICK_MARKED = r"""
(() => { const e = document.querySelector('[data-bandlive="1"]'); if (!e) return false;
  const t = e.matches('a,button') ? e : (e.querySelector('a,button') || e); t.click(); return true; })()
"""

# 영상 상태. 멈춰 있으면 무음으로 재생을 건다.
JS_VIDEO = r"""
(() => {
  const vs = [...document.querySelectorAll('video')];
  const text = %s;
  const shown = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  // 밴드 플레이어는 끝나면 .liveMessageBox('라이브 방송이 종료되었습니다.')를 보이고 liveState 에 -finished 를 붙인다
  const endBox = [...document.querySelectorAll('.liveMessageBox,._liveMessage,.liveState.-finished')]
    .some(e => shown(e) && /종료/.test(e.textContent || ''));
  const vc = document.querySelector('[data-viewname="DLiveViewerCountView"]');
  const vm = vc && (vc.textContent || '').match(/\d[\d,]*/);
  const base = {url: location.href, n: vs.length, viewers: vm ? +vm[0].replace(/,/g, '') : null,
                endText: endBox || /(라이브|방송)[^\n]{0,10}(종료되었|종료됐|끝났습니다)|종료된\s*(라이브|방송|LIVE)|(live|broadcast) (has |is )?ended/i.test(text)};
  // 라이브 화면이 아닌 곳(밴드 홈 피드의 지난 방송 등)의 영상은 절대 틀지 않는다 — 재생한 멤버로 남는다
  if (!/\/live\/\d+/.test(location.pathname)) return base;
  if (!vs.length) {
    const b = document.querySelector('.btnLivePlay,[class*="btnPlay"],button[aria-label*="재생"]');
    if (b) b.click();
    return base;
  }
  const v = vs.sort((x, y) => y.clientWidth * y.clientHeight - x.clientWidth * x.clientHeight)[0];
  v.muted = true;
  if (v.paused && !v.ended) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
  return Object.assign(base, {t: v.currentTime, paused: v.paused, ended: v.ended, rs: v.readyState,
                              muted: v.muted, w: v.clientWidth, h: v.clientHeight});
})()
""" % JS_TEXT

JS_VIEWER_COUNT = r"""
(() => { const e = document.querySelector('[data-viewname="DLiveViewerCountView"]');
  const m = e && (e.textContent || '').match(/\d[\d,]*/); return m ? m[0].replace(/,/g, '') : null; })()
"""

JS_MY_BANDS = r"""
(() => [...document.querySelectorAll('a[href*="/band/"]')].map(a => ({
   h: a.getAttribute('href') || '', t: (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80)}))
   .filter(x => /\/band\/\d+/.test(x.h) && x.t))()
"""


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def is_live_url(url):
    return bool(re.search(r"/(band|page)/\d+/live/\d+", url or ""))


class Band:
    def __init__(self, browser):
        self.b = browser
        self.entered = {}

    def snapshot(self, tag):
        if not load_cfg().get("record", True):
            return None
        try:
            SNAPS.mkdir(parents=True, exist_ok=True)
            stem = SNAPS / ("%s_%s" % (dt.datetime.now().strftime("%Y%m%d_%H%M%S"), tag))
            with self.b.tab() as t:
                html = t.js("document.documentElement.outerHTML.slice(0, 1500000)") or ""
                url = t.url()
                shot = t.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60}, timeout=20)
            stem.with_suffix(".html").write_text("<!-- %s -->\n%s" % (url, html), encoding="utf-8")
            stem.with_suffix(".jpg").write_bytes(base64.b64decode(shot.get("data", "")))
            return stem.with_suffix(".jpg")
        except Exception as e:
            log("화면 기록 실패(%s): %s" % (tag, e))
            return None

    def logged_in(self):
        """로그인 여부. 로그인 안 된 채 피드를 열면 인증 화면이나 첫 화면으로 밀려난다."""
        with self.b.tab() as t:
            t.goto("https://www.band.us/feed")
            url = t.url()
            has_login = t.js("!!document.querySelector('a[href*=\"auth.band.us\"],a[href*=\"login\"],.btnLogin,._loginBtn')")
        if "auth.band.us" in url or "/login" in url:
            return False
        path = re.sub(r"^https?://[^/]+", "", url).split("?")[0].rstrip("/")
        if path in ("", "/home", "/intro", "/ko", "/en"):
            return False
        return not (has_login and "/feed" not in url)

    def ensure_login(self):
        """로그인돼 있으면 쿠키를 갱신 보관하고, 풀려 있으면 보관해 둔 쿠키를 되넣어 본다."""
        ok = self.logged_in()
        if not ok and self.b.restore_cookies():
            log("로그인이 풀려 있어 보관한 쿠키를 되넣음")
            ok = self.logged_in()
        if ok:
            try:
                self.b.save_cookies()
            except Exception as e:
                log("쿠키 보관 실패: %s" % e)
        return ok

    def resolve_band(self):
        cfg = load_cfg()
        if cfg.get("band_no"):
            return cfg["band_no"]
        name = cfg["band_name"]
        found = []
        with self.b.tab() as t:
            for url in ("https://www.band.us/", "https://www.band.us/feed"):
                t.goto(url, settle=4)
                t.js("window.scrollTo(0, 600)")
                time.sleep(1)
                found = [x for x in (t.js(JS_MY_BANDS) or []) if name.replace(" ", "") in x["t"].replace(" ", "")]
                if found:
                    break
        if not found:
            return ""
        no = re.search(r"/band/(\d+)", found[0]["h"]).group(1)
        save_cfg({"band_no": no})
        log("밴드 번호 확인: %s → %s" % (found[0]["t"], no))
        return no

    def home_url(self):
        return "https://www.band.us/band/%s" % load_cfg()["band_no"]

    def _home_live_ids(self, t):
        """밴드 홈을 열면서 화면이 받아 오는 밴드 정보(get_band_information)를 엿본다.
        진행 중인 방송이 있으면 거기 live_ids 에 방송 번호가 실린다(웹 번들의 getOnAirLiveId 가 읽는 값).
        (방송 번호 목록, 밴드 정보를 읽었는지) 를 돌려준다."""
        t.call("Network.enable")
        t.ws.send(json.dumps({"id": 900001, "method": "Page.navigate", "params": {"url": self.home_url()}}))
        want, done, start = [], set(), time.time()
        while time.time() - start < 15:
            t.ws.settimeout(1.0)
            try:
                m = json.loads(t.ws.recv())
            except websocket.WebSocketTimeoutException:
                m = {}
            t._dialog(m)
            if m.get("method") == "Network.responseReceived":
                if "get_band_information" in m["params"]["response"]["url"]:
                    want.append(m["params"]["requestId"])
            elif m.get("method") == "Network.loadingFinished":
                done.add(m["params"]["requestId"])
            if time.time() - start >= 5 and any(i in done for i in want):
                break
        ids, read = [], False
        for i in want:
            try:
                body = json.loads(t.call("Network.getResponseBody", {"requestId": i}).get("body") or "null")
            except (CdpError, ValueError):
                continue  # 사전 요청(OPTIONS)은 본문이 없다
            if isinstance(body, dict) and body.get("result_data"):
                read = True
                ids += [str(x) for x in _find_key(body["result_data"], "live_ids") or []]
        t.call("Network.disable")
        return ids, read

    def find_live(self):
        with self.b.tab() as t:
            try:
                ids, read = self._home_live_ids(t)
            except Exception as e:
                log("밴드 정보 읽기 실패: %s → 화면에서 찾음" % e)
                t.goto(self.home_url())
                ids, read = [], False
            if ids:
                return {"how": "api", "api_read": True,
                        "href": "https://www.band.us/band/%s/live/%s" % (load_cfg()["band_no"], ids[0])}
            found = t.js(JS_FIND_LIVE) or {}
            found["api_read"] = read
            return found

    def _sniff(self, t, keys, sec, url=None, click_js=None):
        """화면이 서버와 주고받는 응답 중 주소에 keys 가 든 것의 본문을 모은다. {key: 본문(dict)}"""
        t.call("Network.enable")
        if url:
            t.ws.send(json.dumps({"id": 900002, "method": "Page.navigate", "params": {"url": url}}))
        if click_js:
            t.ws.send(json.dumps({"id": 900003, "method": "Runtime.evaluate",
                                  "params": {"expression": click_js, "userGesture": True}}))
        want, done, start = {}, set(), time.time()
        while time.time() - start < sec:
            t.ws.settimeout(0.5)
            try:
                m = json.loads(t.ws.recv())
            except websocket.WebSocketTimeoutException:
                m = {}
            t._dialog(m)
            if m.get("method") == "Network.responseReceived":
                u = m["params"]["response"]["url"].split("?")[0]
                for k in keys:
                    if u.endswith("/" + k):
                        want[m["params"]["requestId"]] = k
            elif m.get("method") == "Network.loadingFinished":
                done.add(m["params"]["requestId"])
            got = {want[i] for i in want if i in done}
            if time.time() - start >= 4 and got >= set(keys):
                break
        out = {}
        for i, k in want.items():
            try:
                body = json.loads(t.call("Network.getResponseBody", {"requestId": i}).get("body") or "null")
            except (CdpError, ValueError):
                continue  # 사전 요청(OPTIONS)은 본문이 없다
            if isinstance(body, dict):
                out[k] = body
        t.call("Network.disable")
        return out

    def enter(self, found):
        """찾은 라이브로 들어간다. 들어간 주소를 돌려준다(못 들어가면 빈 문자열).
        들어갈 때 서버 응답을 엿봐 self.entered 에 남긴다:
        밴드 웹은 라이브 화면을 열면 get_live(방송 정보) 뒤 view_live(시청 시작 등록)를 부른다(웹 번들에서 확인)."""
        self.entered = {}
        with self.b.tab() as t:
            if found.get("href"):
                try:
                    res = self._sniff(t, ["get_live", "view_live"], 12, url=found["href"])
                    self.entered = {k: {"code": v.get("result_code"),
                                        "msg": (v.get("result_data") or {}).get("message") if isinstance(v.get("result_data"), dict) else None}
                                    for k, v in res.items()}
                    log("입장 응답: %s" % json.dumps(self.entered, ensure_ascii=False))
                except Exception as e:
                    log("입장 응답 엿보기 실패(%s) → 그냥 엶" % e)
                    t.goto(found["href"])
            else:
                if not t.js(JS_CLICK_MARKED):
                    return ""
                time.sleep(4)
        for p in self.b.pages():  # 새 탭으로 열릴 수도 있다
            u = p.get("url", "")
            if "create-live" in u:
                log("경고: 방송 '시작' 화면이 열려 바로 닫음 (%s)" % u)
                requests.get(self.b.base + "/json/close/" + p["id"], timeout=3)
                continue
            if is_live_url(u):
                self.b.close_other_tabs("/live/")
                return u
        return ""

    def registered(self):
        """서버가 시청 등록을 받아 줬는지: True / False / None(확인 못 함)"""
        e = getattr(self, "entered", {})
        v = e.get("view_live") or e.get("get_live")
        return None if v is None else v.get("code") == 1

    def viewers(self):
        """지금 시청자 수. 라이브 화면 머리의 시청자 수 표시(DLiveViewerCountView)를 읽기만 한다.
        누르면 명단 창이 화면 위에 떠서 남으므로 누르지 않는다. (시청자 수 또는 None, [])"""
        try:
            with self.b.tab(prefer="/live/") as t:
                n = t.js(JS_VIEWER_COUNT)
        except Exception as e:
            log("시청자 수 읽기 실패: %s" % e)
            return None, []
        return (int(n) if n is not None else None), []

    def leave(self):
        """라이브 화면에서 밴드 홈으로 나간다(화면이 서버에 시청 종료 quit_live 를 알리게)."""
        try:
            with self.b.tab(prefer="/live/") as t:
                if is_live_url(t.url()):
                    t.goto(self.home_url(), settle=2)
        except Exception as e:
            log("퇴장 처리 실패: %s" % e)

    def video(self):
        with self.b.tab(prefer="/live/") as t:
            v = t.js(JS_VIDEO) or {}
        v["dialog"] = next((m for m in recent_dialog() if "종료" in m), None)
        return v


# ---------------------------------------------------------------- 참석 진행
class Runner:
    def __init__(self):
        self.browser = Browser()
        self.band = Band(self.browser)
        self.thread = None
        self.stop_flag = threading.Event()
        self.phase = "대기"
        self.detail = ""
        self.entered_at = None
        self.max_viewers = 0
        self.fails = 0
        self.next_try = 0

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def set(self, phase, detail=""):
        if (phase, detail) != (self.phase, self.detail):
            log("상태: %s %s" % (phase, detail))
        self.phase, self.detail = phase, detail

    def start(self, kind):
        with _lock:
            if self.busy():
                return False
            self.stop_flag.clear()
            target = {"attend": self._attend_scheduled, "manual": self._attend_manual,
                      "login": self._login, "check": self._login_check}[kind]
            self.thread = threading.Thread(target=self._guard, args=(target,), daemon=True)
            self.thread.start()
            return True

    def stop(self):
        self.stop_flag.set()

    def _guard(self, fn):
        try:
            fn()
        except Exception as e:
            log("오류: %r" % e)
            notify("오류로 중단: %s" % e)
        finally:
            try:
                self.browser.close()
            except Exception as e:
                log("브라우저 닫기 실패: %s" % e)
            self.entered_at = None
            self.set("대기")
            _wake.set()

    def _sleep(self, sec):
        return self.stop_flag.wait(sec)

    # -- 로그인
    def _login(self):
        self.set("로그인 창", "창에서 직접 로그인해 주세요(10분 안)")
        self.browser.launch("https://auth.band.us/login_page?next_url=https%3A%2F%2Fwww.band.us%2Ffeed")
        end = time.time() + 600
        while time.time() < end and not self._sleep(4):
            if not self.browser.alive():
                log("로그인 창이 닫힘")
                return
            urls = [p.get("url", "") for p in self.browser.pages()]
            if any(re.match(r"https?://(www\.)?band\.us/", u) and "/about/" not in u for u in urls):
                time.sleep(3)
                if self.band.ensure_login():
                    no = self.band.resolve_band()
                    self.band.snapshot("login_ok")
                    # 창을 실제로 껐다 켜서 로그인이 남는지까지 본다
                    self.set("로그인 창", "껐다 켜서 유지되는지 확인 중")
                    self.browser.close()
                    time.sleep(2)
                    self.browser.launch("https://www.band.us/feed")
                    kept = self.band.ensure_login()
                    save_state({"login_ok": kept, "login_checked": dt.datetime.now().isoformat(timespec="minutes")})
                    name = load_cfg()["band_name"]
                    if kept:
                        notify("로그인 확인됨(브라우저를 껐다 켜도 유지). " + (
                            "%s 밴드 번호 %s" % (name, no) if no
                            else "다만 내 밴드 목록에서 '%s' 를 못 찾음 → 관리 화면에 밴드 주소를 넣어 주세요" % name))
                    else:
                        notify("로그인은 됐으나 브라우저를 껐다 켜니 풀립니다. 자동 입장이 안 되는 상태입니다.")
                        self.band.snapshot("login_not_kept")
                    return
        log("로그인 창 시간 초과 또는 중지")

    def _login_check(self):
        self.set("로그인 점검")
        self.browser.launch("https://www.band.us/feed")
        ok = self.band.ensure_login()
        if ok:
            self.band.resolve_band()
        save_state({"login_ok": ok, "login_checked": dt.datetime.now().isoformat(timespec="minutes")})
        if ok:
            log("로그인 점검: 정상")
        else:
            notify("밴드 로그인이 풀렸습니다. 예배 전에 관리 화면에서 '로그인 창 열기'로 다시 로그인해 주세요.\nhttp://127.0.0.1:%d" % WEB_PORT)

    # -- 참석
    def _attend_scheduled(self):
        cfg = load_cfg()
        today = dt.date.today().isoformat()
        start, kind = plan_for(cfg, dt.date.today()) or (cfg["start"], "정기")
        deadline = hhmm(start) + dt.timedelta(minutes=int(cfg["wait_min"]))
        if load_state().get("attending_date") == today:  # 시청 중에 재시작된 경우
            deadline = max(deadline, dt.datetime.now() + dt.timedelta(minutes=10))
        try:
            self._attend(deadline, hard_end_for(cfg, start), kind)
        except Exception:
            self.fails += 1
            self.next_try = time.time() + 120
            if self.fails >= 5:
                save_state({"last_run_date": today, "attending_date": ""})
            raise
        self.fails = 0
        save_state({"last_run_date": today, "attending_date": ""})

    def _attend_manual(self):
        now = dt.datetime.now()
        hard = max(hhmm(load_cfg()["hard_end"]), now + dt.timedelta(hours=3))
        self._attend(now + dt.timedelta(minutes=10), hard, "수동")

    def _attend(self, deadline, hard_end, kind):
        self.set("준비", "브라우저 띄우는 중")
        self.browser.launch("https://www.band.us/feed")
        told_login = False
        while not self.band.ensure_login():
            save_state({"login_ok": False})
            if not told_login:
                notify("로그인이 풀려 있어 들어가지 못하고 있습니다. 관리 화면에서 다시 로그인해 주세요.")
                self.band.snapshot("logged_out")
                told_login = True
            self.set("로그인 필요", "1분마다 다시 확인")
            if dt.datetime.now() > deadline or self._sleep(60):
                add_history({"date": dt.date.today().isoformat(), "kind": kind, "result": "로그인 풀림"})
                return
        save_state({"login_ok": True})
        if not self.band.resolve_band():
            notify("내 밴드 목록에서 '%s' 를 찾지 못했습니다. 관리 화면에 밴드 주소를 넣어 주세요." % load_cfg()["band_name"])
            self.band.snapshot("band_not_found")
            add_history({"date": dt.date.today().isoformat(), "kind": kind, "result": "밴드 못 찾음"})
            return

        # 1) 라이브가 뜰 때까지 기다린다
        live_url, shots = "", 0
        while not live_url:
            if self.stop_flag.is_set():
                log("수동 중지")
                return
            if dt.datetime.now() > deadline:
                notify("%s 까지 라이브가 올라오지 않아 종료했습니다." % deadline.strftime("%H:%M"))
                self.band.snapshot("no_live")
                add_history({"date": dt.date.today().isoformat(), "kind": kind, "result": "라이브 없음"})
                return
            self.set("라이브 대기", "%s 까지" % deadline.strftime("%H:%M"))
            live_url, how = self._try_enter(first=(shots == 0))
            shots = 1
            if not live_url and self._sleep(20):
                return

        # 2) 실제로 재생되는지 확인한다(재생 시간이 늘어나야 인정)
        self.entered_at = dt.datetime.now()
        if kind != "수동":
            save_state({"attending_date": dt.date.today().isoformat()})
        self.set("입장", live_url)
        playing = self._wait_playing(90)
        reg = self.band.registered()
        reg_text = {True: "서버가 방송 입장을 받아 줌", False: "서버가 입장을 거절(%s)" % self.band.entered,
                    None: "서버 응답은 확인 못 함"}[reg]
        self.max_viewers = 0
        who = ""   # 시청자 수는 입장 직후엔 아직 0 이라 종료 알림에 최대값으로 싣는다
        log("입장 점검: 재생=%s, 등록=%s, 응답=%s" % (playing, reg, json.dumps(self.band.entered, ensure_ascii=False)))
        shot = self.band.snapshot("live_entered")
        if playing:
            notify("예배 라이브 입장 완료 (%s) — 무음 재생 확인, %s%s / 찾은 방법: %s" % (
                self.entered_at.strftime("%H:%M"), reg_text, who, how), shot)
        else:
            notify("라이브 화면에는 들어갔으나 재생이 확인되지 않습니다(%s). 계속 시도합니다. (%s)" % (reg_text, live_url), shot)

        # 3) 끝날 때까지 지킨다. 끝난 뒤 5분은 방송이 새로 다시 열리는지 본다(끊겼다 재시작하면 방송 번호가 바뀐다)
        while True:
            reason = self._watch(live_url, hard_end)
            if reason.startswith(("수동", "상한")):
                break
            self.set("종료 확인", "5분간 방송이 다시 열리는지 봄")
            live_url, until = "", time.time() + 300
            while not live_url and time.time() < until and dt.datetime.now() < hard_end:
                if self._sleep(20):
                    break
                live_url, how = self._try_enter()
            if not live_url:
                break
            log("방송이 다시 열려 재입장: %s" % live_url)
            self._wait_playing(60)
        if reason.startswith(("수동", "상한")):
            self.band.leave()
        try:
            self.browser.save_cookies()  # 시청 중 갱신된 쿠키를 닫기 전에 보관
        except Exception as e:
            log("쿠키 보관 실패: %s" % e)
        mins = int((dt.datetime.now() - self.entered_at).total_seconds() // 60)
        shot = self.band.snapshot("live_end")
        notify("예배 라이브 종료 — %s~%s (%d분) / 시청자 최대 %d명(본인 포함) / 사유: %s" % (
            self.entered_at.strftime("%H:%M"), dt.datetime.now().strftime("%H:%M"), mins, self.max_viewers, reason), shot)
        add_history({"date": dt.date.today().isoformat(), "kind": kind, "result": "참석 %d분" % mins,
                     "from": self.entered_at.strftime("%H:%M"), "to": dt.datetime.now().strftime("%H:%M"),
                     "reason": reason})

    def _revive(self):
        """브라우저가 죽었으면 다시 띄우고 로그인을 되살린다."""
        if self.browser.alive():
            return
        log("브라우저가 꺼져 있어 다시 띄움")
        self.browser.launch("https://www.band.us/feed")
        self.band.ensure_login()

    def _try_enter(self, first=False):
        """라이브를 찾아 들어가 본다. 통신 오류는 삼키고 다음 차례에 다시 한다. (들어간 주소, 찾은 방법)"""
        try:
            self._revive()
            found = self.band.find_live()
            if first:
                self.band.snapshot("home_waiting")
                log("밴드 정보 읽기: %s" % ("됨" if found.get("api_read") else "안 됨(화면에서만 찾음)"))
            if not found.get("how"):
                return "", ""
            log("라이브 발견(%s): %s" % (found["how"], found.get("href") or found.get("text")))
            self.band.snapshot("home_live")
            url = self.band.enter(found)
            if not url:
                log("라이브 화면으로 넘어가지 못함 → 다시 시도")
                self.band.snapshot("enter_failed")
            return url, found["how"]
        except Exception as e:
            log("라이브 찾기 실패(다시 시도): %r" % e)
            try:
                self.browser.unstick()
            except Exception:
                pass
            return "", ""

    def _wait_playing(self, sec):
        first, end = None, time.time() + sec
        while time.time() < end and not self._sleep(4):
            try:
                v = self.band.video()
            except Exception as e:
                log("영상 확인 실패: %s" % e)
                continue
            if v.get("n") and v.get("t") is not None:
                if first is None:
                    first = v["t"]
                elif v["t"] > first + 1:
                    return True
        return False

    def _watch(self, live_url, hard_end):
        """종료 판정. 영상이 멈추거나 사라지면 밴드 홈에 라이브가 남아 있는지 두 번 확인한 뒤에만 끈다."""
        last_t, last_move = None, time.time()
        last_shot = time.time()
        fails = 0
        while True:
            if self._sleep(20):
                return "수동 중지"
            if dt.datetime.now() >= hard_end:
                return "상한 시각 %s 도달" % hard_end.strftime("%H:%M")
            died = not self.browser.alive()
            if died:
                try:
                    self._revive()
                except Exception as e:
                    log("브라우저 되살리기 실패: %r" % e)
            stuck = False
            try:
                v = {} if died else self.band.video()
                fails = 0
            except Exception as e:
                log("영상 확인 실패: %s" % e)
                v = {}
                fails += 1
                if fails >= 2:
                    stuck = self.browser.unstick() > 0
                    fails = 0
            if v.get("viewers") is not None:   # 시청자 수는 입장 뒤 1분쯤 지나야 반영된다(실측)
                self.max_viewers = max(self.max_viewers, v["viewers"])
            moved = v.get("t") is not None and (last_t is None or v["t"] > last_t + 0.5)
            if moved:
                last_t, last_move = v["t"], time.time()
                self.set("시청 중", "재생 %d초째" % int(v["t"]))
            suspect = None
            if died:
                suspect = "브라우저가 꺼짐"
            elif stuck:
                suspect = "화면이 막혀 탭을 새로 엶"
            elif v.get("dialog"):
                suspect = "종료 알림창(%s)" % v["dialog"][:30]
                DIALOGS.clear()
            elif v.get("endText"):
                suspect = "화면에 방송 종료 문구"
            elif v.get("ended"):
                suspect = "영상이 끝남"
            elif v and not is_live_url(v.get("url")):
                suspect = "라이브 화면에서 벗어남"
            elif time.time() - last_move > 90:
                suspect = "재생이 90초 넘게 멈춤"
            if time.time() - last_shot > 600:
                self.band.snapshot("live_watching")
                last_shot = time.time()
            if not suspect:
                continue
            log("종료 의심: %s → 밴드 홈에서 확인" % suspect)
            self.band.snapshot("end_suspect")
            again = self._recheck()
            if again:
                log("라이브가 아직 진행 중 → 다시 입장 (%s)" % again)
                live_url = again
                last_t, last_move = None, time.time()
                self._wait_playing(60)
                continue
            return suspect + ", 밴드 홈에도 라이브 없음(2회 확인)"

    def _recheck(self):
        for i in range(2):
            try:
                found = self.band.find_live()
                if found.get("how"):
                    url = self.band.enter(found)
                    if url:
                        return url
            except Exception as e:
                log("홈 확인 실패: %s" % e)
            if i == 0 and self._sleep(30):
                return ""
        return ""


runner = Runner()


def due_and_next(cfg, st, now, next_try=0.0):
    """지금 할 일("attend" / "check" / None)과, 다음에 깨어날 시각(없으면 None)을 정한다."""
    today = now.date().isoformat()
    due = None
    plan = plan_for(cfg, now.date())
    if plan and st.get("last_run_date") != today:
        start = hhmm(plan[0], now.date())
        lead = start - dt.timedelta(minutes=int(cfg["lead_min"]))
        in_window = lead <= now < start + dt.timedelta(minutes=int(cfg["wait_min"]))
        # 시청 중에 프로그램·서버가 재시작됐으면 상한 시각 전까지는 다시 들어간다
        resume = st.get("attending_date") == today and lead <= now < hard_end_for(cfg, plan[0], now.date())
        # 시작 3시간 전쯤 로그인이 살아 있는지 미리 본다
        precheck = start - dt.timedelta(hours=3) <= now < start - dt.timedelta(hours=2)
        if in_window or resume:
            due = "attend" if time.time() >= next_try else None
        elif precheck and st.get("login_check_date") != today:
            due = "check"
    # 다음에 깨어날 시각: 앞으로의 일정마다 '로그인 미리 점검'과 '대기 시작' 두 번
    times = []
    for i in range(15):
        d = now.date() + dt.timedelta(days=i)
        p = plan_for(cfg, d)
        if not p or (i == 0 and st.get("last_run_date") == today):
            continue
        start = hhmm(p[0], d)
        times += [start - dt.timedelta(hours=3), start - dt.timedelta(minutes=int(cfg["lead_min"]))]
    future = [t for t in times if t > now]
    nxt = min(future) if future else None
    if next_try > time.time():   # 오류 뒤 재시도 시각
        retry = now + dt.timedelta(seconds=next_try - time.time())
        nxt = min(nxt, retry) if nxt else retry
    return due, nxt


_wake = threading.Event()   # 설정이 바뀌거나 작업이 끝나면 스케줄러를 깨워 다시 계산하게 한다


def scheduler():
    """주기적으로 시계를 보지 않는다. 다음 일정 시각을 계산해 그때까지 잔다."""
    last_clean = 0
    while True:
        wait = 600
        try:
            HEARTBEAT.write_text(dt.datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
            if time.time() - last_clean > 86400:
                clean_old()
                last_clean = time.time()
            now = dt.datetime.now()
            cfg, st = load_cfg(), load_state()
            if runner.busy():
                wait = None     # 작업이 끝나면 _guard 가 깨운다
            else:
                due, nxt = due_and_next(cfg, st, now, runner.next_try)
                if due == "check":
                    save_state({"login_check_date": now.date().isoformat()})
                if due:
                    log("%s 시작" % {"attend": "참석", "check": "로그인 미리 점검"}[due])
                    runner.start(due)
                    wait = None
                else:
                    wait = max(1.0, (nxt - now).total_seconds() + 0.5) if nxt else None
                    log("다음 깨어날 시각: %s" % (nxt.strftime("%m-%d %H:%M") if nxt else "없음(일정 없음)"))
        except Exception as e:
            log("스케줄러 오류: %r" % e)
        _wake.wait(wait)
        _wake.clear()


# ---------------------------------------------------------------- 관리 화면
app = Flask(__name__)
app.json.ensure_ascii = False


@app.get("/api/status")
def api_status():
    cfg, st = load_cfg(), load_state()
    nxt = next_service_date(cfg)
    skip = cfg.get("skip_date") == nxt.isoformat()
    return jsonify({
        "version": VERSION, "cfg": cfg, "phase": runner.phase, "detail": runner.detail,
        "busy": runner.busy(),
        "entered_at": runner.entered_at.strftime("%H:%M") if runner.entered_at else None,
        "next": "%s (%s) %s" % (nxt.isoformat(), WEEKDAYS[nxt.weekday()], cfg["start"]),
        "upcoming": ["%s (%s) %s · %s" % (d.isoformat(), WEEKDAYS[d.weekday()], st_, k) for d, st_, k in upcoming(cfg)],
        "next_date": nxt.isoformat(), "next_skipped": skip,
        "login_ok": st.get("login_ok"), "login_checked": st.get("login_checked"),
        "history": st.get("history", [])[:8], "log": list(_recent)[-60:],
    })


@app.post("/api/config")
def api_config():
    body = request.get_json(force=True) or {}
    ch = {}
    if "enabled" in body:
        ch["enabled"] = bool(body["enabled"])
    if "weekday" in body:
        ch["weekday"] = max(0, min(6, int(body["weekday"])))
    for k in ("start", "hard_end"):
        if k in body:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body[k])):
                return jsonify({"ok": False, "msg": "시각은 19:30 처럼 적어 주세요"}), 400
            ch[k] = body[k]
    for k in ("lead_min", "wait_min"):
        if k in body:
            ch[k] = max(1, min(180, int(body[k])))
    if "skip_date" in body:
        ch["skip_date"] = str(body["skip_date"])
    if "once" in body:
        once = []
        for o in body["once"] or []:
            if (re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(o.get("date", "")))
                    and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(o.get("start", "")))
                    and o["date"] >= dt.date.today().isoformat()):
                once.append({"date": o["date"], "start": o["start"]})
        ch["once"] = sorted(once, key=lambda o: o["date"])[:20]
    if "band_name" in body:
        ch["band_name"] = str(body["band_name"]).strip()
        ch["band_no"] = ""
    if "band_url" in body:
        m = re.search(r"/band/(\d+)", str(body["band_url"])) or re.fullmatch(r"\s*(\d+)\s*", str(body["band_url"]))
        if not m:
            return jsonify({"ok": False, "msg": "밴드 주소(https://www.band.us/band/숫자)를 넣어 주세요"}), 400
        ch["band_no"] = m.group(1)
    cfg = save_cfg(ch)
    _wake.set()   # 일정이 바뀌었을 수 있으니 다음 깨어날 시각을 다시 계산
    log("설정 변경: %s" % json.dumps(ch, ensure_ascii=False))
    return jsonify({"ok": True, "cfg": cfg})


@app.post("/api/do/<what>")
def api_do(what):
    if what == "stop":
        runner.stop()
        log("관리 화면: 지금 끄기")
        return jsonify({"ok": True, "msg": "끄는 중입니다"})
    kinds = {"run": "manual", "login": "login", "check": "check"}
    if what not in kinds:
        return jsonify({"ok": False, "msg": "모르는 명령"}), 404
    if not runner.start(kinds[what]):
        return jsonify({"ok": False, "msg": "다른 작업이 진행 중입니다(%s)" % runner.phase}), 409
    log("관리 화면: %s" % what)
    return jsonify({"ok": True, "msg": "시작했습니다"})


@app.get("/")
def index():
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")


def main():
    for d in (DATA, LOGS, SNAPS):
        d.mkdir(parents=True, exist_ok=True)
    probe = socket.socket()
    try:
        probe.bind(("0.0.0.0", WEB_PORT))  # 관리 화면 포트가 곧 중복 실행 잠금
    except OSError:
        log("이미 실행 중(포트 %d) → 종료" % WEB_PORT)
        return
    finally:
        probe.close()
    log("band-live %s 시작 — 관리 화면 http://127.0.0.1:%d" % (VERSION, WEB_PORT))
    threading.Thread(target=scheduler, daemon=True).start()
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    if sys.stdout:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
