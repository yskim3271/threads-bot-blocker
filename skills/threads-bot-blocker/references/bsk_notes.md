# bsk 운영 노트 (browser-skill CLI) + `bsk_scan.py` 사용법 (v3.0)

> bsk가 없는 환경에서는 `browser_adapter.md`의 계약으로 다른 브라우저 도구를 연결하거나 모드 3(계정 정밀 진단, SSR)으로 진행한다.

## 1. 데몬과 확장
- 매 호출마다 `BSK_AUTO_START=0`을 지정하고 `bsk status --json`으로 **기존 데몬을 재사용**한다. 환경변수는 호출 사이에 유지되지 않으므로 매번 다시 지정한다.
  - PowerShell: `$env:BSK_AUTO_START = '0'; bsk status --json`
  - Bash: `BSK_AUTO_START=0 bsk status --json`
- 데몬이 없으면 에이전트의 백그라운드 실행 기능으로 `bsk daemon start --foreground`를 띄운다. 준비 여부는 **별도 호출**에서 `status`로 확인한다(최대 5회, 1초 간격). `Start-Process`, `nohup`, 끝에 붙이는 `&`는 쓰지 않는다.
- `browsers`가 비어 있으면 확장이 연결되지 않은 것이다. `bsk doctor`를 실행해 안내를 따른다. 페어링 비밀값은 로그나 리포트에 남기지 않는다.
- `bsk_scan.py`는 모든 호출에 `BSK_AUTO_START=0`을 자동으로 넣는다. 데몬 기동은 하지 않는다.

## 2. 관찰(observation) 규칙
- `@eN` ref는 **가장 최근 관찰** 기준이다. 페이지를 이동한 뒤나 사람이 조작한 뒤에는 `bsk sync`로 다시 관찰한다.
- 쓰기 동작(차단에 한함, `blocking_safety.md` 부록 A)에는 `--expect`로 핸들 텍스트를 고정한다.
- 사용자 탭은 건드리지 않는다. 기본 Agent Window를 쓴다.
- 작업이 끝나면 항상 `bsk close`를 실행한다(오류가 났을 때도).
- 수집에는 `read`를 쓴다(고정된 동기 DOM 읽기). `evaluate`는 고정된 읽기 전용 스크립트(`SCROLL_JS`, `READ_POSTS_JS`, `READ_COMMENTS_JS`)에만 쓴다.

## 3. Windows PowerShell 주의
- `@e3`은 스플래팅 문법과 충돌하므로 **`'@e3'`처럼 작은따옴표로 감싼다**. 예: `bsk click '@e3'`
- `--steps`나 `--expect` JSON은 작은따옴표 here-string으로 넘긴다. 닫는 `'@`는 반드시 0열에 둔다.
  ```powershell
  $expect = @'
  [{"target":"@e5","field":"text","equals":"<handle>"}]
  '@
  bsk click '@e12' --expect $expect
  ```
- `& 'C:\path\bsk.exe'`의 `&`는 호출 연산자다. 백그라운드 실행과는 관계없다.
- **댓글창·답글 수집은 `bsk_scan.py`로 한다.** 인자를 리스트로 넘기므로(`shell=False`) 따옴표 문제가 생기지 않는다.

## 4. `bsk_scan.py` (읽기 전용, 서브커맨드 4개)
```
python scripts/bsk_scan.py feed-comments --max-posts 8 --min-replies 3 --out $run/feed_comments.json          # 모드 1 트랙 A(기본, 선택지 5/8/12)
python scripts/bsk_scan.py feed-comments --query "바이브코딩" --max-posts 8 --out $run/feed_comments.json      # 트랙 B: 키워드 검색 결과에서 선별
python scripts/bsk_scan.py replies --handle <handle> --max-items 15 --out $run/replies_<handle>.json            # 댓글러 최근 답글 15개
python scripts/bsk_scan.py post-comments --url "https://www.threads.com/@<author>/post/<code>" --max-scrolls 3 --out $run/comments.json   # 모드 2
python scripts/bsk_scan.py blocked-list --out $run/blocked.json                                                # 모드 4
python scripts/bsk_scan.py feed-comments --dry-run       # bsk를 실행하지 않고 계획된 명령만 JSON으로 출력(4개 모두 지원)
```
- **읽기 전용 가드(`_guard`)**: `click`, `fill`, `press`, `select`, `drag`, `navigate`, `recipe`와 임의 스크립트를 `ReadOnlyViolation`으로 막는다. 허용 명령은 `status`, `open`, `read`, `extract`, 고정된 `evaluate`(`SCROLL_JS`·`READ_POSTS_JS`·`READ_COMMENTS_JS`), `step wheel|scroll-to`, `wheel`, `sync`, `cooperate sync|wait`, `observe`, `view`, `close`다.
- **댓글창 팔로잉 판별**: 댓글창에서는 텍스트 영역만 잡혀 `textContent`가 늘 `<h>`로 시작한다("핸들로 시작 = 팔로잉" 규칙을 쓰면 123개 중 118개를 버렸다, 2026-11-23 실측). 그래서 `read_comment_rows()`(bsk `read` + 행 단위 `has_follow` exists 필드, 중첩 컨테이너면 고정 `READ_COMMENTS_JS` 폴백)로 **행 전체의 팔로우 오버레이**만 보고 `comment_following()`이 `false` 또는 `"unknown"`만 돌려준다. 아바타 aria-label도 비팔로잉 증거(`false`)만 채택한다.
- **`feed-comments`**: 홈 피드(또는 `--query` 검색 결과) 카드 하단 반응 수(`좋아요94답글13리포스트1공유하기10`)를 `parse_counts()`로 읽고 `rank_posts()`로 `--max-posts`개 고른다(본인 글 제외). 각 글의 댓글창을 `--comments-scrolls`회까지 읽고 `source_post`·`source_author`·`source_excerpt`를 붙인다. 원글 작성자 댓글은 `is_post_author: true`로 남기고 `commenters`(댓글러 단위 집계)에서 뺀다. `--dry-run`은 예시 게시글(`example_author/EXAMPLE`)로 댓글창 단계 계획까지 보여 준다.
- **`replies`**: 기본 `--max-items 15 --max-scrolls 6`. `/@h/replies`의 [타인 원글 → h의 답글] 순서에서 `reply_to_author`·`parent_excerpt`를 붙인다. 각 항목의 `code`로 의심 댓글 직접 링크(`https://www.threads.com/@<h>/post/<code>`)가 만들어진다.
- **스크롤**: `SCROLL_JS`가 게시물 링크를 가장 많이 담은 스크롤 컨테이너를 찾아 컨테이너 높이의 1.6배씩 `scrollBy`한다. 움직이지 않거나 끝에 닿으면 `wheel`로 폴백한다. 한 번 내릴 때마다 1.2~2.2초 기다린다.
- **본문 정제**: `clean_post_text()`가 팔로우 접두, 작성자명, 상대 시각, 반응 수와 공백 없이 붙는 UI 문자열(`좋아요1답글1리포스트1공유하기저장`, `원본 가 좋아함`, `인증된 계정3분`, `AI 콘텐츠`, `게시물이 페디버스에 공유되었습니다`, `접근할 수 없는 게시물에 남긴 답글`, 앞머리 `수정됨`, `채팅 말풍선 꼬리/테두리`, `오디오 소리 꺼짐`)을 제거한다. 덕분에 답글 유사도(R2)가 부풀려지지 않는다.
- **출력**은 `browser_adapter.md`의 계약을 따른다.
  - `feed-comments`: `{"source":"feed-comments","track","url","params","selected_posts":[{author, code, url, reply_count, bot_magnet_score, magnet_reasons, is_low_yield, excerpt, comments_collected}],"items":[…],"commenters":[{handle, is_following, n_comments, n_posts, posts}],"magnet_passed","warnings"}` (`posts`는 `selected_posts`와 같은 하위 호환 키)
  - `post-comments`: `{"source":"post-comments","post_author","post_url","post_code","source_excerpt","items":[{handle, code, url, text, taken_at, is_following, source_post, source_author, is_post_author}]}`
  - `replies`: `{"source":"replies","handle","items":[{handle, code, url, text, taken_at, reply_to_author, parent_excerpt}]}`
  - `blocked-list`: `{"source":"blocked-list","items":[{handle, display_name}]}`

## 5. 봇 유인도(`bot_magnet_score`) 게시글 선별
답글 많은 순으로만 고르면 마시멜로·쇼츠·지역 잡담 같은 **일상·밈 단문**(사람 단문 잡담 위주)에 슬롯을 쓴다. 댓글 봇은 LLM이 요약+공감+질문을 뽑기 쉬운 AI·문제해결·창업·질문 글에 몰린다. 그래서 `feed-comments`는 기본 `--strategy magnet`으로 `tbb_common.score_bot_magnet()` 점수 순(동점이면 답글 수 순)으로 고른다. `adapter.py rank-posts`도 같은 엔진이다.

| 항목 | 점수 | 조건 |
|---|---:|---|
| `ai_tech` | +30 | `AI·클로드·Claude·GPT·제미나이·바이브코딩·에이전트·자동화·코딩·개발·SaaS·노코드·프롬프트·LLM·스레드·MCP·n8n·커서·Codex` 등 |
| `problem_solve` | +25 | `해결·삽질·오류·에러·버그·실패·고쳤·바꿨더니·적용·비교·테스트·후기·팁·정리·노하우·성과·달성·시행착오·원인·개선·만들었` |
| `biz_growth` | +20 | `매출·수익·창업·1인 기업/개발/마케터·스타트업·마케팅·팔로워·조회수·브랜딩·강의·런칭·고객·전환·트래픽·부업·구독·결제` |
| `question_discuss` | +20 | `?·추천·뭐 쓰·어떻게 하/생각/쓰·궁금·알려 주·vs·의견` |
| `reply_golden_zone` | +15 / +8 | 답글 3~80개 +15, 80개 초과 +8, 3개 미만 0 |
| `text_substance` | +10 | 정제된 본문 ≥80자 |
| `freshness` | +10 | 게시 24시간 이내 |
| `daily_meme_penalty` | −25 | 정제된 본문 <35자이고 위 4개 주제 적중 0 |

- `--min-magnet-score 15`(기본) 미만 = `is_low_yield`(저수율). 뒤로 밀리고, 통과 글이 `--max-posts`보다 적으면 차순위로 채운 뒤 `warnings`에 폴백을 기록한다. 통과 글이 목표 수만큼 모이면 피드 스크롤을 멈춘다.
- `--min-replies` 기본 3. `--strategy replies`는 답글 많은 순이다.
- `--query "<검색어>"`(트랙 B)는 홈 피드 대신 `https://www.threads.com/search?q=<URL 인코딩>&serp_type=default`를 **열기만** 하고 같은 선별·댓글창 수집을 한다. 출력 `track: "B"`.
- 예(단위 테스트): "클로드 코드 오류 해결 삽질 후기, 다들 어떻게 쓰시나요?"(답글 15) **90점** vs "쫜득하게 익은 마시멜로"(답글 64) **−10점**, "내가 유튜브 쇼츠로 뭘 본거지 😂"(답글 26) **−10점**.

## 6. 라이브 드라이런 검증 결과 (2026-11-23)
| 항목 | 검증 결과 |
|---|---|
| `read` 응답 JSON 구조 | `items[].values`로 반환된다(`iter_read_items`는 `values`와 `fields`를 모두 지원) |
| 컨테이너 선택자 | `div[data-pressable-container="true"]`가 홈 피드에서 정상 동작한다 |
| 필드 선택자 중복 (`/replies`·댓글창) | 원글과 답글이 한 컨테이너에 묶여 `multiple descendants`가 발생한다 → 고정 `READ_POSTS_JS`/`READ_COMMENTS_JS`로 자동 폴백 |
| `wheel` / `evaluate` | `bsk sync --json`의 `observation_id`를 `--observation`으로 넘겨 실행한다 |
| `--scope` 전달 | 전역 옵션 위치(`bsk --scope <name> ...`)에 둔다 |
| SSR 헤더 (`fetch_profile.py`) | `Sec-Fetch-Mode: navigate`와 `Accept` 헤더가 필요하다(없으면 `not_found`) |
| 차단 목록 경로 | `https://www.threads.com/settings/privacy` → `차단된 프로필` 버튼 → 모달 다이얼로그 |
