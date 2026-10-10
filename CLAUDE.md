# band-live — 네이버 밴드 라이브 예배 자동 참석

## 목적
세종행복교회 밴드에서 매주 금요일 19:30 에 하는 라이브 예배에, 서버가 형님 계정으로 자동으로 들어가
무음으로 시청하고, 방송이 끝나면 스스로 닫는다. 켜고 끄는 것은 관리 화면에서 한다.

## 실행
- **평소엔 창 없이 돈다.** 자동시동 Task `BandLive_AutoStart`(로그온 1분 뒤, RunLevel Limited)
  → `wscript scripts\start_hidden.vbs` → `python.exe app.py`(SW_HIDE)
  - 재등록: `scripts\register_task.ps1` (`-WindowStyle Hidden` 으로. 관리자 권한 필요 없음)
- 관리 화면: http://127.0.0.1:7960 (같은 공유기 안에서는 `http://서버IP:7960`). 인증 없음, 바깥 포트 매핑 없음
- 중복 방지: 관리 화면 포트 7960 을 잡지 못하면 "이미 실행 중" 남기고 종료
- `run.bat` — 로그를 눈으로 보고 싶을 때만(돌고 있으면 바로 꺼진다)
- 코드(`app.py`)가 바뀌면 **작업 중이 아닐 때 스스로 재시작**한다
- 감시: `manage.ps1` / `global_watchdog.ps1` 의 `bandlive` 항목. `data\heartbeat.txt` 30분 정체면 재시동

## 구조
- `app.py` — 전부 여기. 스케줄러 스레드 + Flask 관리 화면 + CDP 브라우저 조작
- `static\index.html` — 관리 화면(켜기/끄기, 이번 한 번 건너뛰기, 지금 들어가기/끄기, 로그인 창, 시간 설정)
- `data\config.json` — 설정(없으면 app.py 의 DEFAULTS). `data\state.json` — 마지막 실행일·기록 30건
- `data\profile\` — **전용 브라우저 프로필(밴드 로그인 쿠키가 여기 있다). 지우면 다시 로그인해야 한다**
- `data\snapshots\` — 화면 사진+HTML 기록(최근 120개). 화면 구조가 바뀌었을 때 원인 찾는 재료
- `data\logs\band_YYYYMMDD.log` — 15일 보관

## 동작
1. 금요일 12시: 로그인이 살아 있는지 미리 점검. 풀렸으면 텔레그램으로 알림
2. 19:20(시작 10분 전)~20:10(시작 40분 뒤): 전용 Edge 를 `--mute-audio` 로 띄워 20초마다 밴드 홈에서 라이브를 찾음
3. 찾으면 `/band/<번호>/live/<방송번호>` 로 들어가 **영상 재생 시간이 실제로 늘어나는지** 확인 → "입장 완료" 알림
4. 20초마다 영상을 살핌. 종료 의심 신호가 나오면 밴드 홈에서 라이브가 사라졌는지 30초 간격 2회 확인 후에만 닫음
   - 의심 신호: 화면에 방송 종료 문구 / 영상 ended / 라이브 주소에서 벗어남 / 재생 90초 넘게 멈춤
   - 홈에 라이브가 남아 있으면 끊긴 것으로 보고 다시 들어감
   - 22:00(hard_end)에는 무조건 닫음
5. 닫을 때는 디버깅 포트 9460 을 쥔 브라우저만 닫는다(다른 Edge·Chrome 은 건드리지 않음)

## 밴드 웹 구조 (2026-10-10 확인)
- 로그인 안 된 채 `https://www.band.us/feed` → `https://auth.band.us/login?next_url=…` 로 밀려남(실측)
- 라이브 주소: `/band/:band_no/live/:live_id` (웹 번들 라우트에서 확인). 밴드 정보에 `live_ids` 가 실림
- 시청 중 표시는 플레이어가 보내는 신호(`live_participating_heartbeat`)라 **영상을 실제로 틀어 놔야** 잡힌다
- 로그인하면 밴드 홈은 `/band/58221837/post` (세종행복교회, 멤버 146). 2026-10-11 실제 화면에서 확인한 것:
  - 진행 중 라이브가 뜨는 자리: `._groupBandLiveCardRegion`(없을 땐 빈 div), 페이지형 밴드는 `._pageBandLiveCardRegion`
  - 끝난 방송 글: `.liveState.-finished` 안에 "종료된 LIVE", 재생 단추 `.btnLivePlay`, 영상 `.liveVideo video`
  - 종료 안내: `.liveMessageBox ._liveMessage` 의 "라이브 방송이 종료되었습니다."(평소 display:none)
  - **'LIVE' 글자만으로 찾으면 끝난 방송 글을 집는다**(초판이 실제로 그랬다) → `-finished`·'종료' 제외
  - 이 밴드는 토요일(안식일 예배)에도 라이브를 한다. 금요일만 들어가는 것은 요일 설정으로 걸러진다
- **로그인 쿠키는 세션 쿠키로 나온다**(닫으면 삭제). `save_cookies` 가 만료일(180일)을 붙여 다시 심고
  `C:\H-Secrets\band-live\cookies.json` 에도 보관, 풀려 있으면 `ensure_login` 이 되넣는다.
  로그인 직후에는 브라우저를 실제로 껐다 켜서 유지되는지 확인한다. 서버가 그 로그인을 며칠 인정하는지는 미확인
- **아직 진행 중인 라이브로 확인하지 못한 것**: 전용 영역 카드 안쪽 구조(링크인지 단추인지), 라이브 화면의 플레이어,
  웹 시청이 진행자 명단에 뜨는지. 첫 실전 뒤 `data\snapshots` 를 보고 `JS_FIND_LIVE`·`JS_VIDEO` 를 맞출 것
- 라이브 화면(`/live/번호`)이 아닌 곳의 영상은 틀지 않는다. 피드의 지난 방송을 틀면 "재생한 멤버"로 남는다

## 수정 시 주의
- **'라이브 방송' 글자만 보고 누르지 말 것.** 글쓰기 메뉴의 방송 *시작* 단추일 수 있다.
  `JS_FIND_LIVE` 는 주소에 `/live/번호` 가 든 링크나 LIVE 딱지만 인정하고, `create-live` 가 열리면 바로 닫는다
- 탭 연결은 쓸 때마다 새로 연다(닫힌 탭 번호를 쥐고 있으면 멈춤)
- 완전 숨김(headless)으로 바꾸지 말 것 — 시청으로 안 잡힐 수 있다. 실제 창 + 백그라운드 절전 해제 옵션으로 돈다
- 채팅·댓글은 넣지 않는다. 시청만
- 시험: 종료 판정은 가짜 시계로 돌려 볼 수 있다(Band·Browser 를 가짜로 바꾸고 `Runner._attend` 호출)

## 기밀
- `C:\H-Secrets\band-live\.env` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (dreame 과 같은 범용 봇)
- `C:\H-Secrets\band-live\cookies.json` — 밴드·네이버 로그인 쿠키. **계정 열쇠와 같다. git·로그·채팅에 내지 말 것**
- 밴드 비밀번호는 어디에도 저장하지 않는다. 형님이 로그인 창에서 직접 넣고, 쿠키만 `data\profile` 과 위 파일에 남는다
