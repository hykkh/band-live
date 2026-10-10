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


def notify(msg):
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


def next_service_date(cfg, today=None):
    today = today or dt.date.today()
    d = today + dt.timedelta(days=(int(cfg["weekday"]) - today.weekday()) % 7)
    if d == today and dt.datetime.now() > hhmm(cfg["start"]) + dt.timedelta(minutes=int(cfg["wait_min"])):
        d += dt.timedelta(days=7)
    return d


# ---------------------------------------------------------------- 브라우저(CDP)
class CdpError(Exception):
    pass


class Tab:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        self.n = 0

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def call(self, method, params=None, timeout=15):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            self.ws.settimeout(max(0.2, end - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                break
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError("%s: %s" % (method, msg["error"].get("message")))
                return msg.get("result", {})
        raise CdpError("%s 응답 없음(%d초)" % (method, timeout))

    def js(self, expr, timeout=15):
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
    def __init__(self):
        self.base = "http://127.0.0.1:%d" % CDP_PORT

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
  const bad = e => !!e.closest('[class*="rite"],[class*="reate"],[class*="ost_write"],[class*="Editor"]');
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const cand = [...document.querySelectorAll('span,em,strong,i,div,a,button')].filter(e => {
    if (e.children.length > 2 || !vis(e) || bad(e)) return false;
    const t = (e.textContent || '').replace(/\s+/g, ' ').trim();
    return /^(LIVE|ON AIR|라이브 중|방송 중|라이브 방송 중)$/i.test(t);
  });
  if (!cand.length) return {href: null, how: null};
  const badge = cand[0];
  const box = badge.closest('a,button,[role="button"],li,article') || badge.parentElement;
  document.querySelectorAll('[data-bandlive]').forEach(e => e.removeAttribute('data-bandlive'));
  box.setAttribute('data-bandlive', '1');
  return {href: null, how: 'badge', text: (box.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
          cls: String(box.className).slice(0, 120)};
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
  const base = {url: location.href, n: vs.length,
                endText: /(라이브|방송)[^\n]{0,20}(종료|끝났)|종료된\s*(라이브|방송)|(live|broadcast)[^\n]{0,20}ended/i.test(text)};
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

JS_MY_BANDS = r"""
(() => [...document.querySelectorAll('a[href*="/band/"]')].map(a => ({
   h: a.getAttribute('href') || '', t: (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80)}))
   .filter(x => /\/band\/\d+/.test(x.h) && x.t))()
"""


def is_live_url(url):
    return bool(re.search(r"/(band|page)/\d+/live/\d+", url or ""))


class Band:
    def __init__(self, browser):
        self.b = browser

    def snapshot(self, tag):
        if not load_cfg().get("record", True):
            return
        try:
            SNAPS.mkdir(parents=True, exist_ok=True)
            stem = SNAPS / ("%s_%s" % (dt.datetime.now().strftime("%Y%m%d_%H%M%S"), tag))
            with self.b.tab() as t:
                html = t.js("document.documentElement.outerHTML.slice(0, 1500000)") or ""
                url = t.url()
                shot = t.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60}, timeout=20)
            stem.with_suffix(".html").write_text("<!-- %s -->\n%s" % (url, html), encoding="utf-8")
            stem.with_suffix(".jpg").write_bytes(base64.b64decode(shot.get("data", "")))
        except Exception as e:
            log("화면 기록 실패(%s): %s" % (tag, e))

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

    def find_live(self):
        with self.b.tab() as t:
            t.goto(self.home_url())
            return t.js(JS_FIND_LIVE) or {}

    def enter(self, found):
        """찾은 라이브로 들어간다. 들어간 주소를 돌려준다(못 들어가면 빈 문자열)."""
        with self.b.tab() as t:
            if found.get("href"):
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

    def video(self):
        with self.b.tab(prefer="/live/") as t:
            return t.js(JS_VIDEO) or {}


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
            if any("band.us" in u and "auth.band.us" not in u and re.search(r"band\.us/(feed|band/|discover|my)", u) for u in urls):
                time.sleep(3)
                if self.band.logged_in():
                    no = self.band.resolve_band()
                    save_state({"login_ok": True, "login_checked": dt.datetime.now().isoformat(timespec="minutes")})
                    name = load_cfg()["band_name"]
                    notify("로그인 확인됨. " + ("%s 밴드 번호 %s" % (name, no) if no
                                           else "다만 내 밴드 목록에서 '%s' 를 못 찾음 → 관리 화면에 밴드 주소를 넣어 주세요" % name))
                    self.band.snapshot("login_ok")
                    return
        log("로그인 창 시간 초과 또는 중지")

    def _login_check(self):
        self.set("로그인 점검")
        self.browser.launch("https://www.band.us/feed")
        ok = self.band.logged_in()
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
        today = dt.date.today()
        save_state({"last_run_date": today.isoformat()})
        deadline = hhmm(cfg["start"]) + dt.timedelta(minutes=int(cfg["wait_min"]))
        self._attend(deadline, hhmm(cfg["hard_end"]), "정기")

    def _attend_manual(self):
        now = dt.datetime.now()
        hard = max(hhmm(load_cfg()["hard_end"]), now + dt.timedelta(hours=3))
        self._attend(now + dt.timedelta(minutes=10), hard, "수동")

    def _attend(self, deadline, hard_end, kind):
        self.set("준비", "브라우저 띄우는 중")
        self.browser.launch("https://www.band.us/feed")
        told_login = False
        while not self.band.logged_in():
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
            found = self.band.find_live()
            if shots == 0:
                self.band.snapshot("home_waiting")
                shots = 1
            if found.get("how"):
                log("라이브 발견(%s): %s" % (found["how"], found.get("href") or found.get("text")))
                self.band.snapshot("home_live")
                live_url = self.band.enter(found)
                if not live_url:
                    log("라이브 화면으로 넘어가지 못함 → 다시 시도")
            if not live_url and self._sleep(20):
                return

        # 2) 실제로 재생되는지 확인한다(재생 시간이 늘어나야 인정)
        self.entered_at = dt.datetime.now()
        self.set("입장", live_url)
        playing = self._wait_playing(90)
        self.band.snapshot("live_entered")
        if playing:
            notify("예배 라이브 입장 완료 (%s, 무음 재생 확인)" % self.entered_at.strftime("%H:%M"))
        else:
            notify("라이브 화면에는 들어갔으나 재생이 확인되지 않습니다. 계속 시도합니다. (%s)" % live_url)

        # 3) 끝날 때까지 지킨다
        reason = self._watch(live_url, hard_end)
        mins = int((dt.datetime.now() - self.entered_at).total_seconds() // 60)
        self.band.snapshot("live_end")
        notify("예배 라이브 종료 — %s~%s (%d분) / 사유: %s" % (
            self.entered_at.strftime("%H:%M"), dt.datetime.now().strftime("%H:%M"), mins, reason))
        add_history({"date": dt.date.today().isoformat(), "kind": kind, "result": "참석 %d분" % mins,
                     "from": self.entered_at.strftime("%H:%M"), "to": dt.datetime.now().strftime("%H:%M"),
                     "reason": reason})

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
        while True:
            if self._sleep(20):
                return "수동 중지"
            if dt.datetime.now() >= hard_end:
                return "상한 시각 %s 도달" % hard_end.strftime("%H:%M")
            try:
                v = self.band.video()
            except Exception as e:
                log("영상 확인 실패: %s" % e)
                v = {}
            moved = v.get("t") is not None and (last_t is None or v["t"] > last_t + 0.5)
            if moved:
                last_t, last_move = v["t"], time.time()
                self.set("시청 중", "재생 %d초째" % int(v["t"]))
            suspect = None
            if v.get("endText"):
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


def scheduler():
    mtime = Path(__file__).stat().st_mtime
    last_clean = 0
    while True:
        try:
            HEARTBEAT.write_text(dt.datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
            now = dt.datetime.now()
            cfg, st = load_cfg(), load_state()
            today = now.date().isoformat()
            if time.time() - last_clean > 6 * 3600:
                clean_old()
                last_clean = time.time()
            if not runner.busy():
                if Path(__file__).stat().st_mtime != mtime:
                    log("코드가 바뀌어 스스로 재시작")
                    subprocess.Popen(["wscript.exe", str(BASE / "scripts" / "start_hidden.vbs")], creationflags=NOWIN)
                    os._exit(0)
                service_day = now.weekday() == int(cfg["weekday"])
                on = cfg["enabled"] and cfg.get("skip_date") != today
                start = hhmm(cfg["start"])
                if service_day and on:
                    # 낮 12시에 로그인이 살아 있는지 미리 본다
                    if now.hour == 12 and st.get("login_check_date") != today:
                        save_state({"login_check_date": today})
                        runner.start("check")
                    elif (start - dt.timedelta(minutes=int(cfg["lead_min"])) <= now
                          < start + dt.timedelta(minutes=int(cfg["wait_min"]))
                          and st.get("last_run_date") != today):
                        log("정기 참석 시작")
                        runner.start("attend")
        except Exception as e:
            log("스케줄러 오류: %r" % e)
        time.sleep(15)


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
    if "band_name" in body:
        ch["band_name"] = str(body["band_name"]).strip()
        ch["band_no"] = ""
    if "band_url" in body:
        m = re.search(r"/band/(\d+)", str(body["band_url"])) or re.fullmatch(r"\s*(\d+)\s*", str(body["band_url"]))
        if not m:
            return jsonify({"ok": False, "msg": "밴드 주소(https://www.band.us/band/숫자)를 넣어 주세요"}), 400
        ch["band_no"] = m.group(1)
    cfg = save_cfg(ch)
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
