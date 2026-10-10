# band-live — 네이버 밴드 라이브 예배 자동 참석

## 목적
세종행복교회 밴드(번호 58221837)에서 매주 금요일 19:30 에 하는 라이브 예배에, 서버가 형님 계정으로 자동으로 들어가
무음으로 시청하고, 방송이 끝나면 스스로 닫는다. 켜고 끄는 것은 관리 화면에서 한다.

## 실행
- **상주 프로그램이다.** 창 없이 떠 있되 **주기적으로 시계를 보지 않는다**(형님 지시 2026-10-11: "그 시간에 움직이면 되지 왜 계속 확인하냐").
  `due_and_next` 가 다음 일정 시각(로그인 미리 점검 = 시작 3시간 전, 대기 시작 = 시작 10분 전)을 계산해 그때까지 잔다.
  설정이 바뀌거나 작업이 끝나면 `_wake` 로 깨워 다시 계산. 로그에 "다음 깨어날 시각"이 남는다
  (윈도우 예약작업은 "로그온하면 이 프로그램을 띄워라" 하나뿐)
- 자동시동 Task `BandLive_AutoStart`(로그온 1분 뒤, RunLevel Limited)
  → `wscript scripts\start_hidden.vbs` → `python.exe app.py`(SW_HIDE). 재등록: `scripts\register_task.ps1`
- 관리 화면: http://127.0.0.1:7960 (같은 공유기 안에서는 `http://서버IP:7960`). 인증 없음, 바깥 포트 매핑 없음
- 중복 방지: 관리 화면 포트 7960 을 잡지 못하면 "이미 실행 중" 남기고 종료
- `run.bat` — 로그를 눈으로 보고 싶을 때만(돌고 있으면 바로 꺼진다)
- 코드를 고친 뒤에는 **직접 다시 띄운다**: 포트 7960 을 쥔 프로세스만 끄고 `wscript scripts\start_hidden.vbs`
  (스스로 재시작 기능은 없앴다 — 주기적으로 파일을 들여다봐야 해서)
- 감시: `manage.ps1` / `global_watchdog.ps1` 의 `bandlive` 항목. 포트 7960 으로만 생사를 본다(자는 동안 heartbeat 가 안 바뀌므로 정체 판정은 뺐다)

## 구조
- `app.py` — 전부 여기. 스케줄러 스레드 + Flask 관리 화면 + CDP 브라우저 조작 + 알림창 감시 스레드
- `static\index.html` — 관리 화면(켜기/끄기, 이번 한 번 건너뛰기, 한 번만 들어가는 날, 지금 들어가기/끄기, 로그인 창, 시간 설정)
- `data\config.json` — 설정(없으면 app.py 의 DEFAULTS). `once` = 한 번만 들어가는 날 목록
- `data\state.json` — `last_run_date`(그날 끝냄), `attending_date`(시청 중 표시), 기록 30건
- `data\profile\` — 전용 브라우저 프로필. `data\snapshots\` — 화면 사진+HTML(최근 120개). `data\logs\` — 15일 보관

## 동작
1. 시작 3시간 전쯤: 로그인이 살아 있는지 미리 점검. 풀렸으면 텔레그램
2. 시작 10분 전~40분 뒤: 전용 Edge 를 `--mute-audio` 로 띄워 20초마다 진행 중인 라이브를 찾는다
   - ① 밴드 홈이 받아 오는 `get_band_information` 응답의 `live_ids` (밴드 웹이 라이브 카드를 띄울지 정하는 바로 그 값)
   - ② 화면의 `/live/번호` 링크 ③ `._groupBandLiveCardRegion` 카드 ④ 피드의 진행 중 딱지
3. `/band/<번호>/live/<방송번호>` 로 들어간다. 이때 `get_live`·`view_live` 응답을 엿봐 **서버가 시청 등록을 받았는지** 확인하고,
   영상 재생 시간이 늘어나는지, 시청자 명단까지 읽어 화면 사진과 함께 텔레그램으로 보낸다
4. 20초마다 살핀다. 종료 의심 신호 → 밴드 홈에서 30초 간격 2회 확인 → 그래도 없으면 5분 더 지켜본 뒤 닫는다
   - 의심 신호: 종료 알림창 / 화면의 종료 문구 / 영상 ended / 라이브 주소에서 벗어남 / 재생 90초 멈춤 / 브라우저 죽음 / 화면 막힘
   - 홈에 라이브가 남아 있거나 새 방송 번호로 다시 열리면 다시 들어간다
   - 상한 시각(기본 22:00, 시작이 늦으면 시작+3시간)에는 무조건 닫는다
5. 예배 도중 프로그램·서버가 재시작돼도 상한 시각 전이면 다시 들어간다(`attending_date`). 오류로 죽으면 2분 뒤 재시도, 5번까지
6. 닫을 때는 디버깅 포트 9460 을 쥔 브라우저만 닫는다(다른 Edge·Chrome 은 건드리지 않음)

## 밴드 웹에서 확인한 사실 (2026-10-11, 실측과 웹 번들 코드)
- 로그인 안 된 채 `/feed` → `auth.band.us/login` 으로 밀려남. 로그인하면 밴드 홈은 `/band/58221837/post`
- **로그인 쿠키는 세션 쿠키**(닫으면 삭제). `save_cookies` 가 만료일(180일)을 붙여 다시 심고
  `C:\H-Secrets\band-live\cookies.json` 에도 보관, 풀려 있으면 `ensure_login` 이 되넣는다.
  서버가 그 로그인을 며칠 인정하는지는 미확인
- 라이브 주소 `/band/:band_no/live/:live_id`. 홈은 `showBandLiveCardView(region, bandCore, getOnAirLiveId())` 로 카드를 띄우고
  `getOnAirLiveId` 는 밴드 정보의 `live_ids[0]`. **방송이 없으면 `live_ids` 항목 자체가 응답에 없다**
- 서버 호출(v2.0.0): `get_live`(방송 정보) `view_live`(시청 시작) `quit_live`(시청 종료) `get_live_viewing_members`(시청 중 명단)
  `get_live_viewer_count` `get_member_in_live`. `off_live`·`create_live`·`on_live` 는 방송을 끄고 켜는 것 — 절대 건드리지 말 것
- **끝난 방송 주소를 열면** `get_live` 가 `result_code 1002 "라이브 방송이 종료되었습니다."` 를 주고,
  화면은 같은 문구로 `alert()` 를 띄운 뒤 밴드 홈으로 돌려보낸다
- **alert 가 떠 있으면 화면 읽기(Runtime.evaluate)가 통째로 멈춘다. 뜬 뒤에 붙은 연결로는 닫을 수도 없다**
  ("No dialog is showing"). 그래서 탭마다 연결을 미리 물고 있는 감시 스레드(`_guard_loop`)가 뜨는 즉시 닫고 내용을 `DIALOGS` 에 남긴다.
  그래도 막힌 탭은 `Browser.unstick()` 이 닫고 새로 연다
- 라이브 화면: `section.lyVideoLiveWrap`(DLiveViewerLayoutView) 안에 `.videoArea`(DLiveVideoPlayerView)의 `video`.
  붙으면 자동 재생, **시청자는 소리 켠 채 시작**(방송한 본인만 무음 시작) → 브라우저 `--mute-audio` 가 실제로 필요
  `._offBtn` 은 방송 끄기 단추(권한자에게만) — 누르지 말 것. 재생 단추는 `._btnPlay`, 시청자 수는 `DLiveViewerCountView`
- 끝난 방송 글: `.liveState.-finished` "종료된 LIVE". **'LIVE' 글자만으로 찾으면 이걸 집는다**(초판이 그랬다)
- 지난 방송 글은 방송이 끝난 직후 올라온다. 금·화 예배 글이 20:20~20:25 → 둘 다 19:30 시작, 50분쯤. 토요일은 10:37/12:12 무렵
- 공개 페이지의 진행 중 방송을 한눈에 보는 목록은 없다(발견·검색에 안 나옴). 2026-10-11 02시에 페이지 120곳 조회 → 0곳

## 아직 진행 중인 라이브로 확인하지 못한 것 (2026-10-13 화 19:30 시험에서 판가름)
- `live_ids` 가 방송 중에 실제로 실리는지(코드로는 확실, 값은 못 봄) → 로그 "라이브 발견(api)"
- `view_live` 의 정상 `result_code` 가 1 인지 → 로그 "입장 응답"
- 시청자 명단을 권한 없이 읽을 수 있는지, 형님 이름이 뜨는지 → 로그 "입장 점검"
- 서버 원격 접속이 끊긴 상태에서 재생이 이어지는지
- 시험 뒤 `data\snapshots\*live_entered*`, `*live_end*` 와 로그를 보고 맞출 것

## 수정 시 주의
- 라이브 화면(`/live/번호`)이 아닌 곳의 영상은 틀지 않는다. 피드의 지난 방송을 틀면 "재생한 멤버"로 남는다
  (2026-10-11 시험 중 한 번 실제로 1초쯤 틀었다)
- 탭 연결은 쓸 때마다 새로 연다(닫힌 탭 번호를 쥐고 있으면 멈춤)
- 완전 숨김(headless)으로 바꾸지 말 것 — 시청으로 안 잡힐 수 있다
- 채팅·댓글은 넣지 않는다. 시청만
- 시험: 가짜 시계로 참석·종료 흐름을 돌릴 수 있다(Band·Browser 를 가짜로 바꾸고 `Runner._attend` 호출).
  끝난 방송 주소 `/band/58221837/live/34060422` 는 알림창·종료 처리 실측에 쓸 수 있다(재생되지 않음)

## 기밀
- `C:\H-Secrets\band-live\.env` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (dreame 과 같은 범용 봇)
- `C:\H-Secrets\band-live\cookies.json` — 밴드·네이버 로그인 쿠키. **계정 열쇠와 같다. git·로그·채팅에 내지 말 것**
- 밴드 비밀번호는 어디에도 저장하지 않는다. 형님이 로그인 창에서 직접 넣는다
