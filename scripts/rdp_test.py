# -*- coding: utf-8 -*-
"""원격 접속이 끊긴 상태에서도 밴드 라이브가 실제로 재생되는지 시험한다.

이 PC 의 원격 접속 세션이 '연결 끊김'이 되기를 기다렸다가(최대 기다림은 인자, 기본 120분):
  1) 전용 브라우저를 끊긴 상태에서 새로 띄우고
  2) 시험용 밴드(104761721, 형님 혼자 + 시험 계정)에서 ffmpeg 로 시험 방송을 켜고
  3) 60초 동안 재생 시간·그려진 화면 수를 재고 화면을 찍은 뒤
  4) 방송을 끄고(그 밴드 주소인지 확인한 뒤에만 종료 단추) 브라우저·송출을 정리하고
  5) 결과를 텔레그램으로 보낸다.
교회 밴드(58221837)에는 손대지 않는다. 운영 프로그램(:7960)이 참석 중이면 시작하지 않는다.
"""
import ctypes
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

TEST_BAND = "104761721"
FF = r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
OUT = app.DATA / "rdp_test"
LOG = OUT / "rdp_test.log"
NOWIN = 0x08000000


def say(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (dt.datetime.now().strftime("%m-%d %H:%M:%S"), msg))


app.log = say


def session_state():
    """이 프로세스가 속한 윈도우 세션의 연결 상태: 0=사용 중, 4=연결 끊김 …"""
    wts = ctypes.windll.wtsapi32
    sid = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(sid))
    buf, n = ctypes.c_void_p(), ctypes.c_ulong()
    if not wts.WTSQuerySessionInformationW(None, sid.value, 8, ctypes.byref(buf), ctypes.byref(n)):  # 8 = WTSConnectState
        return None
    st = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
    wts.WTSFreeMemory(buf)
    return st


def wait_js(t, expr, sec, what):
    end = time.time() + sec
    while time.time() < end:
        try:
            if t.js(expr):
                return True
        except app.CdpError:
            pass
        time.sleep(1)
    raise RuntimeError("%s (%d초 안에 안 됨)" % (what, sec))


def main():
    wait_min = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    say("=== 시작: 원격 접속이 끊기기를 최대 %d분 기다림 (지금 상태 %s)" % (wait_min, session_state()))
    end = time.time() + wait_min * 60
    while session_state() != 4:
        if time.time() > end:
            say("기다리는 동안 원격 접속이 끊기지 않아 시험하지 않고 끝냄")
            return
        time.sleep(5)
    say("원격 접속 끊김 확인 → 30초 뒤 시작")
    time.sleep(30)
    try:
        st = json.loads(app.requests.get("http://127.0.0.1:%d/api/status" % app.WEB_PORT, timeout=5).text)
        if st.get("busy"):
            say("운영 프로그램이 작업 중이라 시험하지 않음: %s" % st.get("phase"))
            return
    except Exception:
        pass

    b = app.Browser()
    ff = None
    result = {"state_at_start": session_state()}
    shot_path = None
    try:
        b.close()                      # 끊긴 상태에서 새로 띄우는 것까지 시험
        time.sleep(2)
        b.launch("https://www.band.us/feed")
        b.settle_guard()
        band = app.Band(b)
        result["login"] = band.ensure_login()
        with b.tab() as t:
            t.goto("https://www.band.us/band/%s/create-live" % TEST_BAND, settle=3)
            if "/band/%s/create-live" % TEST_BAND not in t.url():
                raise RuntimeError("방송 만들기 화면이 아님: %s" % t.url())
            t.js("(() => { document.querySelectorAll('button._btnCloseHelpLayer').forEach(x => x.click()); })()")
            time.sleep(1)
            t.js("document.querySelector('button._btnGetStreamUrl').click()")
            wait_js(t, "document.querySelector('#_streamUrl').value && document.querySelector('#_streamKey').value ? 1 : 0", 15, "송출 키 발급")
            u, k = t.js("document.querySelector('#_streamUrl').value"), t.js("document.querySelector('#_streamKey').value")
            cmd = [FF, "-hide_banner", "-loglevel", "error", "-re",
                   "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                   "-filter:a", "volume=0.05", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                   "-b:v", "1500k", "-maxrate", "1500k", "-bufsize", "3000k", "-g", "60", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-t", "600", "-f", "flv", u.rstrip("/") + "/" + k]
            ff = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=NOWIN)
            say("송출 시작")
            # 미리보기 영상이 실제로 돌 때 시작을 누른다(너무 일찍 누르면 '미송출')
            wait_js(t, "(() => { const v = document.querySelector('.lyVideoLiveWrap video'); const s = document.querySelector('.lyVideoLiveWrap button._startBtn');"
                       " return v && v.currentTime > 8 && s && s.offsetWidth ? 1 : 0; })()", 90, "미리보기 재생")
            t.js("document.querySelector('.lyVideoLiveWrap button._startBtn').click()")
            wait_js(t, "/\\/band\\/%s\\/live\\/\\d+/.test(location.pathname) ? 1 : 0" % TEST_BAND, 25, "방송 시작")
            result["live_url"] = t.url()
            say("방송 켜짐: %s" % result["live_url"])
        time.sleep(10)
        q = ("(() => { const v = document.querySelector('.lyVideoLiveWrap video'); const qq = v.getVideoPlaybackQuality();"
             " return {t: v.currentTime, frames: qq.totalVideoFrames, paused: v.paused, hidden: document.hidden, vis: document.visibilityState}; })()")
        with b.tab(prefer="/live/") as t:
            a = t.js(q)
            time.sleep(60)
            c = t.js(q)
            try:
                shot = t.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60}, timeout=20)
                shot_path = OUT / ("rdp_test_%s.jpg" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
                shot_path.write_bytes(app.base64.b64decode(shot.get("data", "")))
            except Exception as e:
                result["shot_error"] = str(e)
        result.update({"played_sec": round(c["t"] - a["t"], 1), "frames": c["frames"] - a["frames"],
                       "paused": c["paused"], "hidden": c["hidden"], "vis": c["vis"], "state_during": session_state()})
        say("측정: %s" % json.dumps(result, ensure_ascii=False))
    except Exception as e:
        result["error"] = repr(e)
        say("시험 실패: %r" % e)
    finally:
        # 시험 방송 끄기: 시험 밴드 주소일 때만 종료 단추 → 확인
        try:
            for p in b.pages():
                if "/band/%s/live/" % TEST_BAND not in p.get("url", ""):
                    continue
                with app.Tab(p["webSocketDebuggerUrl"]) as t:
                    t.js("(() => { const x = document.querySelector('.lyVideoLiveWrap button._offBtn'); if (x) x.click(); })()")
                    time.sleep(2)
                    t.js("(() => { const l = [...document.querySelectorAll('.lyWrap.layer_wrap')].find(e => e.offsetWidth && /이 방송을 종료하시겠습니까/.test(e.innerText || ''));"
                         " if (l) l.querySelector('button._btnConfirm').click(); })()")
                    time.sleep(4)
                say("시험 방송 종료 단추 누름")
                break
        except Exception as e:
            say("방송 끄기 실패: %r" % e)
        if ff and ff.poll() is None:
            ff.kill()
        try:
            b.close()
        except Exception:
            pass
        say("정리 끝")

    ok = (not result.get("error")) and result.get("played_sec", 0) > 50 and result.get("frames", 0) > 600
    msg = ("원격 접속 끊긴 상태 재생 시험: %s\n재생 +%s초, 화면 %s장 그려짐(60초 동안), 화면 상태 %s, 세션 상태 %s%s" % (
        "정상" if ok else "문제 있음", result.get("played_sec"), result.get("frames"), result.get("vis"),
        "끊김" if result.get("state_during") == 4 else result.get("state_during"),
        ("\n오류: " + result["error"]) if result.get("error") else ""))
    say(msg.replace("\n", " / "))
    app.notify(msg, shot_path)


if __name__ == "__main__":
    main()
