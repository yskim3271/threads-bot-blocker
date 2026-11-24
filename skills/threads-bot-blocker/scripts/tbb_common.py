"""threads-bot-blocker v3.0 공통 유틸 (표준 라이브러리만 사용) — 댓글 봇 단일 타겟.

- 공용 정규식(P5_KW)·UI 크롬 정제(clean_ui_text)·카드 반응 수(parse_counts)·봇 유인도(score_bot_magnet, rank_posts).

- Windows PowerShell 콘솔에서도 한글/이모지가 깨지지 않도록 stdout/stderr를 UTF-8로 재설정한다.
- 스킬 자체 파일은 이 파일(scripts/) 기준 상대 경로로 찾고, 실행 중 생기는 캐시·작업 파일은 스킬 폴더 밖
  작업 디렉터리(THREADS_BOT_BLOCKER_HOME, 기본 ~/.threads-bot-blocker)에 쓴다 → 설치 폴더를 깨끗하게 유지.
- 사용자 핸들은 하드코딩하지 않는다: THREADS_SELF_HANDLE 환경변수 또는 --self 인자.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
DATA_DIR = SKILL_DIR / "data"
WORK_DIR = Path(os.environ.get("THREADS_BOT_BLOCKER_HOME") or (Path.home() / ".threads-bot-blocker")).expanduser()
CACHE_DIR = WORK_DIR / "cache"
RUNS_DIR = WORK_DIR / "runs"
KST = dt.timezone(dt.timedelta(hours=9))
PROFILE_URL = "https://www.threads.com/@{handle}"
VERSION = "3.0.0"


def setup_utf8() -> None:
    """콘솔 인코딩(cp949 등)과 무관하게 UTF-8로 출력한다."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))


def load_json(path: str | os.PathLike):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def dump_json(obj, path: str | os.PathLike | None = None) -> str:
    text = json.dumps(obj, ensure_ascii=False, indent=1, default=str)
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
    return text


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def kst_from_ts(ts: int | float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, KST)


def kst_iso(ts: int | float) -> str:
    return kst_from_ts(ts).isoformat()


def normalize_handle(h: str) -> str:
    h = (h or "").strip()
    if "threads.com/" in h or "threads.net/" in h:
        h = h.split("/@", 1)[-1] if "/@" in h else h.rsplit("/", 1)[-1]
        h = h.split("/", 1)[0].split("?", 1)[0]
    return h.lstrip("@").strip().lower()


def profile_url(handle: str) -> str:
    return PROFILE_URL.format(handle=normalize_handle(handle))


def self_handle_default() -> str:
    """본인 핸들 기본값: THREADS_SELF_HANDLE 환경변수(없으면 빈 문자열 → 본인 제외 생략)."""
    return normalize_handle(os.environ.get("THREADS_SELF_HANDLE", ""))


# ---------------------------------------------------------------- Threads UI 크롬(버튼·배지·반응 수) 정제
# 라이브 /replies 수집에서 본문 뒤에 공백 없이 붙어 나온 문자열(2026-11-23 실측):
#   '…ㅋㅋ좋아요1답글리포스트공유하기저장', '원본 가 좋아함 …', '인증된 계정3분', '…AI 콘텐츠좋아요3답글1…'
_N = r"\s*[\d.,]*\s*(?:천|만|K|M|k)?"
_UI_TAIL = re.compile(r"\s*(?:AI\s*콘텐츠\s*)?(?:좋아요" + _N + r"\s*)?(?:답글" + _N + r"\s*)?(?:리포스트" + _N + r"\s*)?"
                      r"(?:공유하기" + _N + r"|공유\d[\d.,]*\s*(?:천|만|K|M|k)?)\s*(?:저장)?\s*$")
_UI_TAIL_EN = re.compile(r"\s*(?:Like" + _N + r"\s*)?(?:Reply" + _N + r"\s*)?(?:Repost" + _N + r"\s*)?Share\s*(?:Save)?\s*$", re.I)
_UI_INLINE = re.compile(r"원본\s*(?:작성자)?\s*가\s*좋아함|Liked by (?:original )?author|인증된\s*계정\s*(?:\d+\s*(?:초|분|시간|일|주))?"
                        r"|Verified account|AI\s*콘텐츠|AI info|번역하기|See translation|Translate|더\s*보기"
                        r"|@[\w.]+님에게 남긴 답글|Instagram logo\w*"
                        # 댓글창 실측(2026-11-23): 페디버스 공유 배지·접근 불가 원글·스티커 말풍선·오디오 배지
                        r"|게시물이 페디버스에 공유되었습니다(?:\s*\d+\s*(?:초|분|시간|일|주)(?![가-힣]))?|This post was shared to the fediverse"
                        r"|접근할 수 없는 게시물에 남긴 답글|채팅 말풍선 (?:꼬리|테두리)|오디오 소리 꺼짐", re.I)
_EDITED = re.compile(r"^(?:수정됨|Edited)\s+")


def clean_ui_text(text: str | None) -> str:
    """수집된 본문에서 Threads UI 문자열을 제거한다(멱등). 답글 유사도(E4_sim)가 UI 꼬리말로 부풀려지지 않게 한다."""
    s = re.sub(r"\s+", " ", text or "").strip()
    for _ in range(2):
        s = _UI_TAIL.sub("", s)
        s = _UI_TAIL_EN.sub("", s)
    s = _UI_INLINE.sub(" ", s)
    s = _EDITED.sub("", re.sub(r"\s+", " ", s).strip())
    return s.strip()


# ---------------------------------------------------------------- P5_kw 자동화·봇 키워드 (R5 프로필 신호; 사용자 확정 2026-11-23)
P5_KW = re.compile(
    r"자동화|자동\s?포스팅|자동\s?발행|예약\s?발행|(?<![가-힣])봇(?![가-힣])|(?<![가-힣a-zA-Z])bot(?![a-zA-Z])"
    r"|AI\s?봇|답글\s?봇|n8n|make\.com|zapier|cron|크론|스케줄러",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- 카드 반응 수 파싱 (bsk_scan·adapter 공용)
_CNT = r"([\d][\d.,]*\s*(?:천|만|K|M|k)?)?"
COUNTS_KO = re.compile(r"좋아요\s*" + _CNT + r"\s*답글\s*" + _CNT + r"\s*리포스트\s*" + _CNT + r"\s*공유(?:하기)?\s*" + _CNT)
COUNTS_EN = re.compile(r"Like\s*" + _CNT + r"\s*Reply\s*" + _CNT + r"\s*Repost\s*" + _CNT + r"\s*Share\s*" + _CNT, re.I)


def _count_value(s: str | None) -> int:
    if not s:
        return 0
    s = s.replace(",", "").strip()
    mult = 10000 if s.endswith("만") else 1000 if s[-1:] in ("천", "K", "k") else 1000000 if s.endswith("M") else 1
    try:
        return int(float(re.sub(r"[^\d.]", "", s) or 0) * mult)
    except ValueError:
        return 0


def parse_counts(raw_text: str | None) -> dict | None:
    """카드 textContent 끝의 '좋아요94답글13리포스트1공유하기10저장' → {"like":94,"reply":13,"repost":1,"share":10}. 없으면 None."""
    s = re.sub(r"\s+", " ", raw_text or "")
    found = list(COUNTS_KO.finditer(s)) or list(COUNTS_EN.finditer(s))
    if not found:
        return None
    m = found[-1]
    return {k: _count_value(m.group(i + 1)) for i, k in enumerate(("like", "reply", "repost", "share"))}


# ---------------------------------------------------------------- 봇 유인도(bot_magnet_score) — 어떤 글의 댓글창을 먼저 열까
# 댓글 봇은 LLM이 '요약+공감+질문'을 뽑기 쉽고 타겟 오디언스가 모인 텍스트 글(AI·개발, 문제→해결, 창업·수익, 질문)에 기생한다.
# 일상·밈 단문(답글은 많아도 사람 잡담 위주)에 스캔 슬롯을 쓰지 않도록 결정론적으로 점수화한다.
MAGNET_TOPICS = {
    "ai_tech": (30, re.compile(r"AI|클로드|Claude|GPT|ChatGPT|제미나이|Gemini|바이브코딩|에이전트|Agent|자동화|코딩|개발|SaaS|노코드|프롬프트|LLM"
                               r"|스레드|MCP|n8n|커서|Cursor|Codex|코덱스", re.I)),
    "problem_solve": (25, re.compile(r"해결|삽질|오류|에러|버그|실패|고쳤|바꿨더니|적용|비교|테스트|후기|팁|정리|노하우|성과|달성|시행착오"
                                     r"|트러블슈팅|원인|개선|만들었", re.I)),
    "biz_growth": (20, re.compile(r"매출|수익|창업|1인\s*(?:기업|개발|마케터)|스타트업|마케팅|팔로워|조회수|브랜딩|강의|런칭|고객|전환|트래픽"
                                  r"|부업|구독|결제", re.I)),
    "question_discuss": (20, re.compile(r"[?？]|추천|뭐\s*쓰|어떻게\s*(?:하|생각|쓰)|궁금|알려\s*주|vs\b|의견", re.I)),
}
MAGNET_LOW_YIELD = 15        # 이 점수 미만 = 저수율 일상·밈 글 (--min-magnet-score 기본값)


def score_bot_magnet(post: dict, now_ts: int | None = None) -> dict:
    """게시글 1개의 봇 유인도. post: {text 또는 excerpt, reply_count, taken_at(epoch 초)} → {"score","reasons","is_low_yield"}."""
    import time as _time
    text = clean_ui_text(post.get("text") or post.get("excerpt") or "")
    score, reasons = 0, []
    topic_hits = 0
    for key, (pts, rx) in MAGNET_TOPICS.items():
        if rx.search(text):
            score += pts
            reasons.append(key)
            topic_hits += 1
    try:
        rc = int(post.get("reply_count") or 0)
    except (TypeError, ValueError):
        rc = 0
    if 3 <= rc <= 80:
        score += 15
        reasons.append("reply_golden_zone")
    elif rc > 80:
        score += 8
        reasons.append("reply_golden_zone")
    if len(text) >= 80:
        score += 10
        reasons.append("text_substance")
    ts = post.get("taken_at")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        now = int(now_ts if now_ts is not None else _time.time())
        if 0 <= now - ts <= 86400:
            score += 10
            reasons.append("freshness")
    if len(text) < 35 and topic_hits == 0:
        score -= 25
        reasons.append("daily_meme_penalty")
    return {"score": int(score), "reasons": reasons, "is_low_yield": score < MAGNET_LOW_YIELD}


def rank_posts(posts, strategy: str = "magnet", min_replies: int = 3, max_posts: int = 8, min_magnet_score: int = MAGNET_LOW_YIELD,
               self_handle: str = "", now_ts: int | None = None) -> tuple[list[dict], list[str], int]:
    """댓글창을 열 게시글 선별 (bsk_scan feed-comments · adapter rank-posts 공용 엔진).
    strategy='magnet': bot_magnet_score 내림차순(동점이면 reply_count 내림차순). min_magnet_score 미만은 뒤로 미루고,
                       기준 통과 글이 max_posts보다 적으면 차순위로 채운 뒤 경고를 남긴다.
    strategy='replies': 답글 많은 순(reply_count 내림차순).
    반환: (선별 목록 — 각 항목에 bot_magnet_score·magnet_reasons·is_low_yield, 경고, 기준 통과 수)"""
    me = normalize_handle(self_handle or "")
    seen, pool = set(), []
    for p in posts:
        code = p.get("code")
        if not code or code in seen or (me and normalize_handle(p.get("handle") or "") == me) or (p.get("reply_count") or 0) < min_replies:
            continue
        seen.add(code)
        m = score_bot_magnet(p, now_ts)
        pool.append(dict(p, bot_magnet_score=m["score"], magnet_reasons=m["reasons"], is_low_yield=m["is_low_yield"]))
    warnings: list[str] = []
    if strategy == "replies":
        out = sorted(pool, key=lambda x: (-(x.get("reply_count") or 0), x.get("seen_order", 0)))[:max_posts]
        return out, warnings, len(out)
    key = lambda x: (-x["bot_magnet_score"], -(x.get("reply_count") or 0), x.get("seen_order", 0))  # noqa: E731
    good = sorted([x for x in pool if x["bot_magnet_score"] >= min_magnet_score], key=key)
    rest = sorted([x for x in pool if x["bot_magnet_score"] < min_magnet_score], key=key)
    out = good[:max_posts]
    if len(out) < max_posts and rest:
        fill = rest[: max_posts - len(out)]
        out += fill
        warnings.append(f"봇 유인도 {min_magnet_score}점 이상 글 {len(good)}개 → 차순위(저수율 일상·밈) {len(fill)}개로 폴백")
    return out, warnings, len(good)
