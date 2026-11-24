#!/usr/bin/env python3
"""bsk(browser-skill) CLI를 Python subprocess로 안전하게 호출하는 **읽기 전용** 댓글창 수집기 (v3.0, 댓글 봇 단일 타겟).

- 인자를 리스트로 넘기고 shell을 쓰지 않으므로 PowerShell의 '@eN' 스플래팅이나 따옴표 문제가 생기지 않는다.
- 모든 호출에 BSK_AUTO_START=0 을 넣어 기존 데몬을 재사용한다(데몬 기동은 bsk_notes.md 절차).
- 쓰기 동작(click/fill/press/select/drag/navigate·임의 evaluate)은 _guard()가 차단한다. 차단 기능은 이 파일에 없다.
  승인된 차단은 references/blocking_safety.md 절차대로 bsk 명령을 1개씩 직접 실행한다.

서브커맨드:
  feed-comments  --max-posts 8 --min-replies 3 --strategy magnet --min-magnet-score 15 [--query "AI 해결"] --out feed_comments.json
                 (모드 1 기본: 트랙 A 홈 피드 / 트랙 B 키워드 검색에서 봇 유인도(bot_magnet_score) 상위 K개 글
                  → 댓글창 → 비팔로잉 댓글러·댓글·원글 요약. --strategy replies 는 답글 많은 순)
  replies        --handle <h> --max-items 15 --out replies.json          (댓글러 최근 답글 15개 — 댓글 봇 진단 핵심 표본)
  post-comments  --url <post_url> --max-scrolls 3 --out comments.json    (모드 2 특정 게시글·내 최근 글 댓글창)
  blocked-list   --out blocked.json                                       (모드 4 차단 목록 학습)

댓글창 팔로잉 판별: 댓글창에서는 카드 textContent가 '{handle}'로 시작해도 팔로잉이라는 뜻이 아니다.
행(row) 전체에 팔로우 오버레이(svg[aria-label="팔로우"] 등)가 보이면 is_following=False, 안 보이면 "unknown"이다.
댓글창 수집은 절대 True로 판정하지 않는다(comment_following) → 비팔로잉 댓글러를 잘못 버리지 않는다.
공통: --dry-run(실행 없이 계획된 bsk 명령 출력), --bsk <경로>, --scope <이름>, --timeout <초>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tbb_common import (KST, MAGNET_LOW_YIELD, clean_ui_text, dump_json, normalize_handle, now_kst, parse_counts,  # noqa: E402,F401
                        rank_posts, score_bot_magnet, setup_utf8)

THREADS = "https://www.threads.com"
DEFAULT_BLOCKED_URL = THREADS + "/settings/privacy/blocked_profiles"  # 라이브 드라이런에서 실제 경로 확인 필요
POST_HREF = re.compile(r"/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)")
PROFILE_HREF = re.compile(r"^(?:https?://www\.threads\.(?:com|net))?/@([A-Za-z0-9._]+)/?(?:\?.*)?$")
LABEL_KO = re.compile(r"^\s*([A-Za-z0-9._]+)님의 프로필 사진(.*)$")
LABEL_EN = re.compile(r"^\s*([A-Za-z0-9._]+)'s profile picture(.*)$", re.I)
FOLLOW_WORDS = ("팔로우", "follow")

# ---------------------------------------------------------------- 읽기 전용 가드
ALLOWED_SUBCOMMANDS = {"status", "open", "read", "extract", "evaluate", "step", "close", "cooperate", "observe", "view", "sync", "wheel"}
ALLOWED_STEP_ACTIONS = {"wheel", "scroll-to"}
FORBIDDEN_JS = re.compile(r"\.click\(|dispatchEvent|submit\(|\.value\s*=|fetch\(|XMLHttpRequest|localStorage|document\.cookie|innerHTML\s*=", re.I)

SCROLL_JS = r"""(() => {
  const posts = () => document.querySelectorAll('a[href*="/post/"]').length;
  let best = null, bestN = 0;
  for (const el of document.querySelectorAll('div, main, section')) {
    const s = getComputedStyle(el);
    if (!/(auto|scroll)/.test(s.overflowY) || el.scrollHeight <= el.clientHeight + 50) continue;
    const n = el.querySelectorAll('a[href*="/post/"]').length;
    if (n > bestN) { best = el; bestN = n; }
  }
  const target = best || document.scrollingElement || document.documentElement;
  const before = best ? best.scrollTop : window.scrollY;
  const step = Math.round((best ? best.clientHeight : window.innerHeight) * 1.6);
  if (best) best.scrollBy(0, step); else window.scrollBy(0, step);
  const after = best ? best.scrollTop : window.scrollY;
  return {container: best ? (best.tagName + '.' + String(best.className || '').slice(0, 40)) : 'window',
          before, after, moved: after > before, posts: posts(),
          atEnd: (target.scrollTop + target.clientHeight) >= target.scrollHeight - 5};
})()"""
READ_POSTS_JS = r"""(() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/post/"]')) {
    const t = a.querySelector('time');
    if (!t) continue;
    const href = a.getAttribute('href') || '';
    if (!href || seen.has(href)) continue;
    seen.add(href);
    let box = a.parentElement;
    for (let i = 0; i < 8 && box; i++) {
      const links = box.querySelectorAll('a[href*="/post/"] time').length;
      if (links > 1) { box = box.parentElement ? a.parentElement : box; break; }
      if ((box.textContent || '').length >= 40) break;
      box = box.parentElement;
    }
    const text = (box && box.textContent) ? box.textContent : '';
    out.push({permalink: href, datetime: t.getAttribute('datetime') || '', text});
  }
  return out;
})()"""
# 댓글창 전용: 행(row = 가장 가까운 pressable 컨테이너) 단위 텍스트 + 팔로우 오버레이 존재 여부 (읽기 전용)
READ_COMMENTS_JS = r"""(() => {
  const out = [];
  const seen = new Set();
  const FOLLOW = 'svg[aria-label="팔로우"], svg[aria-label="Follow"], [aria-label$="팔로우"], [aria-label$="Follow"]';
  for (const a of document.querySelectorAll('a[href*="/post/"]')) {
    const t = a.querySelector('time');
    if (!t) continue;
    const href = a.getAttribute('href') || '';
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const row = a.closest('div[data-pressable-container="true"]') || a.parentElement;
    const hasFollow = !!(row && row.querySelector(FOLLOW));
    const text = (row && row.textContent) ? row.textContent : '';
    out.push({permalink: href, datetime: t.getAttribute('datetime') || '', text, has_follow: hasFollow});
  }
  return out;
})()"""
ALLOWED_JS = {SCROLL_JS, READ_POSTS_JS, READ_COMMENTS_JS}
WHEEL_PARAMS = {"delta": {"x": 0, "y": 1200}}  # bsk step wheel 파라미터 — 라이브 드라이런에서 `bsk step --help`로 확인

# bsk read 필드 정의 (고정 동기 코드로 DOM만 읽음 — 입력·네트워크 없음)
CONTAINER_SEL = 'div[data-pressable-container="true"]'
POST_FIELDS = [
    {"key": "permalink", "read": "attribute", "attribute": "href", "selector": 'a[href*="/post/"]:has(time)'},
    {"key": "datetime", "read": "attribute", "attribute": "datetime", "selector": "time"},
    {"key": "text", "read": "text"},
]
FOLLOW_OVERLAY_SEL = 'svg[aria-label="팔로우"], svg[aria-label="Follow"], [aria-label$="팔로우"], [aria-label$="Follow"]'
COMMENT_FIELDS = POST_FIELDS + [{"key": "has_follow", "read": "exists", "selector": FOLLOW_OVERLAY_SEL}]
AVATAR_SEL = '[aria-label*="프로필 사진"], [aria-label*="profile picture"]'
AVATAR_FIELDS = [
    {"key": "label", "read": "attribute", "attribute": "aria-label"},
    {"key": "follow_svg", "read": "exists", "selector": 'svg[aria-label="팔로우"], svg[aria-label="Follow"]'},
]
LINK_SEL = 'a[href^="/@"]'
LINK_FIELDS = [{"key": "href", "read": "attribute", "attribute": "href"}, {"key": "text", "read": "text"}]


class ReadOnlyViolation(RuntimeError):
    pass


def _guard(args: list[str]) -> None:
    if not args:
        raise ReadOnlyViolation("빈 명령")
    sub = args[0]
    if sub not in ALLOWED_SUBCOMMANDS:
        raise ReadOnlyViolation(f"읽기 전용 수집기에서 허용되지 않는 bsk 명령: {sub}")
    if sub == "step":
        action = args[args.index("--action") + 1] if "--action" in args else ""
        if action not in ALLOWED_STEP_ACTIONS:
            raise ReadOnlyViolation(f"step 동작 '{action}' 금지 (허용: {sorted(ALLOWED_STEP_ACTIONS)})")
    if sub == "evaluate":
        js = args[1] if len(args) > 1 else ""
        if js not in ALLOWED_JS or FORBIDDEN_JS.search(js):
            raise ReadOnlyViolation("고정된 스크롤 스크립트 외 evaluate 금지")
    if sub == "cooperate" and (len(args) < 2 or args[1] not in ("sync", "wait")):
        raise ReadOnlyViolation("cooperate 는 sync/wait 만 허용")


# ---------------------------------------------------------------- 파싱 유틸 (오프라인 단위 테스트 대상)
def parse_post_href(href: str | None) -> tuple[str, str] | None:
    m = POST_HREF.search(href or "")
    return (normalize_handle(m.group(1)), m.group(2)) if m else None


def parse_profile_href(href: str | None) -> str | None:
    m = PROFILE_HREF.match((href or "").strip())
    return normalize_handle(m.group(1)) if m else None


def parse_avatar_label(label: str | None, follow_svg: bool | None = None) -> tuple[str, bool | str] | None:
    """'some_user님의 프로필 사진 팔로우' → (some_user, False=비팔로잉)
       'friend_user님의 프로필 사진'     → (friend_user, True=팔로잉)"""
    for rx in (LABEL_KO, LABEL_EN):
        m = rx.match(label or "")
        if m:
            rest = m.group(2).strip().lower()
            if follow_svg or any(w in rest for w in FOLLOW_WORDS):
                return normalize_handle(m.group(1)), False
            if follow_svg is False or rest == "":
                return normalize_handle(m.group(1)), True
            return normalize_handle(m.group(1)), "unknown"
    return None


def iter_read_items(obj) -> list[dict]:
    """bsk read/extract JSON 응답에서 항목 리스트를 관대하게 찾아 {key: value} 로 평탄화."""
    def flat(it):
        if not isinstance(it, dict):
            return {}
        for bag_key in ("values", "fields"):
            bag = it.get(bag_key)
            if isinstance(bag, dict):
                return {k: (v.get("value") if isinstance(v, dict) and "value" in v else v) for k, v in bag.items()}
            if isinstance(bag, list):
                return {f.get("key"): f.get("value") for f in bag if isinstance(f, dict)}
        return it
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for key in ("items", "results", "nodes", "rows"):
                v = cur.get(key)
                if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                    return [flat(x) for x in v]
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            if cur and all(isinstance(x, dict) for x in cur) and any("values" in x or "fields" in x or "permalink" in x or "label" in x for x in cur):
                return [flat(x) for x in cur]
            stack.extend(cur)
    return []


def parse_json_output(stdout: str):
    s = (stdout or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        for opener in ("{", "["):
            i = s.find(opener)
            if i >= 0:
                try:
                    return json.loads(s[i:])
                except json.JSONDecodeError:
                    continue
    return None


def parse_context_id(stdout: str) -> str | None:
    obj = parse_json_output(stdout)
    if isinstance(obj, dict):
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k in ("context_id", "contextId"):
                    if isinstance(cur.get(k), str):
                        return cur[k]
                stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
            elif isinstance(cur, list):
                stack.extend(cur)
    m = re.search(r"ctx=([A-Za-z0-9_-]+)", stdout or "")
    return m.group(1) if m else None


_TIME_TOKENS = re.compile(r"^\s*(\d+\s*(초|분|시간|일|주)|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|\d+\s*[smhdw])\s*")
_TRAILING_COUNTS = re.compile(r"(\s*(좋아요|답글|리포스트|공유|Like|Reply|Repost|Share)?\s*\d[\d,.]*\s*(천|만|K|M)?)+\s*$")
_NOISE = re.compile(r"번역하기|Translate|더 보기|See translation|작성자|Author")
_PINNED = re.compile(r"^\s*(?:고정됨|고정된 게시물|Pinned)\s*", re.I)


def infer_following_from_text(raw_text: str | None, author: str | None) -> bool | str:
    """Threads pressable-container textContent: 비팔로잉은 '팔로우{handle}'/'Follow{handle}'로 시작하고,
    팔로잉 중이면 '{handle}'로 바로 시작한다."""
    if not raw_text or not author:
        return "unknown"
    s = re.sub(r"\s+", "", raw_text)
    h = normalize_handle(author)
    sl = s.lower()
    if sl.startswith(f"팔로우{h}") or sl.startswith(f"follow{h}"):
        return False
    if sl.startswith(h):
        return True
    return "unknown"


def comment_following(has_follow=None, *hints) -> bool | str:
    """댓글창 전용 팔로잉 판별: 행에 팔로우 오버레이가 있거나 비팔로잉 증거(False)가 하나라도 있으면 False,
    그 밖에는 "unknown". 텍스트가 '{handle}'로 시작하는 것(textContent가 텍스트 영역만 잡힌 경우)은 팔로잉 증거가 아니므로
    True를 절대 반환하지 않는다(2026-11-23 실측: 123개 중 118개를 팔로잉으로 오판해 버린 버그 수정)."""
    if has_follow is True or (isinstance(has_follow, str) and has_follow.strip().lower() == "true") or any(h is False for h in hints):
        return False
    return "unknown"


def clean_post_text(text: str | None, author: str | None = None) -> str:
    """bsk read 'text'(textContent) 에서 팔로우 접두·작성자명·상대 시각·하단 반응 수와
    공백 없이 붙는 UI 크롬('좋아요1답글1리포스트1공유하기저장', '원본 가 좋아함', '인증된 계정3분', 'AI 콘텐츠')을 걷어낸다."""
    s = clean_ui_text(text)
    s = _PINNED.sub("", s)
    s = re.sub(r"^(?:팔로우|Follow)\s*", "", s, flags=re.I)
    if author and s.lower().startswith(author.lower()):
        s = s[len(author):].strip()
    s = _TIME_TOKENS.sub("", s)
    s = _NOISE.sub(" ", s)
    s = _TRAILING_COUNTS.sub("", s)
    return clean_ui_text(s)


def iso_to_epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def posts_from_read(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        pc = parse_post_href(it.get("permalink"))
        if not pc:
            continue
        ts = iso_to_epoch(it.get("datetime"))
        raw_text = it.get("text")
        out.append({"handle": pc[0], "code": pc[1], "url": f"{THREADS}/@{pc[0]}/post/{pc[1]}",
                    "taken_at": ts, "taken_at_kst": dt.datetime.fromtimestamp(ts, KST).isoformat() if ts else None,
                    "is_following_hint": infer_following_from_text(raw_text, pc[0]),
                    "text": clean_post_text(raw_text, pc[0])})
    return out


def feed_posts_with_counts(rows: list[dict]) -> list[dict]:
    """홈 피드 카드 → posts_from_read 결과 + 답글 수(reply_count, 카드 하단 반응 수에서 파싱)."""
    out = []
    for it in rows:
        ps = posts_from_read([it])
        if not ps:
            continue
        p = ps[0]
        p.pop("is_following_hint", None)
        c = parse_counts(it.get("text")) or {}
        p["reply_count"] = c.get("reply", 0)
        p["like_count"] = c.get("like", 0)
        out.append(p)
    return out


def select_hot_posts(posts, min_replies: int = 5, max_posts: int = 8, self_handle: str = "", strategy: str = "replies",
                     min_magnet_score: int = MAGNET_LOW_YIELD, now_ts: int | None = None) -> list[dict]:
    """tbb_common.rank_posts 래퍼(선별 목록만). 기본 strategy='replies' = 답글 많은 순(하위 호환)."""
    return rank_posts(posts, strategy, min_replies, max_posts, min_magnet_score, self_handle, now_ts)[0]


def search_url(query: str) -> str:
    """트랙 B 키워드 사냥터: Threads 검색 결과(읽기 전용으로 열기만 한다)."""
    return f"{THREADS}/search?q={urllib.parse.quote(query.strip())}&serp_type=default"


def comments_from_rows(rows: list[dict], post_author: str, post_code: str, avatar_obs: dict | None = None,
                       source_excerpt: str = "") -> list[dict]:
    """댓글창 행 → 댓글 항목. 원글 자체는 빼고, 원글 작성자 댓글은 is_post_author=True로 표시한다.
    is_following: comment_following(행 오버레이, 아바타 비팔로잉 증거) → False 또는 "unknown"(True 없음)."""
    avatar_obs = avatar_obs or {}
    out, seen = [], set()
    for it in rows:
        ps = posts_from_read([it])
        if not ps:
            continue
        p = ps[0]
        p.pop("is_following_hint", None)                  # 텍스트 접두 기반 추론은 댓글창에서 쓰지 않는다
        if p["code"] == post_code or p["code"] in seen:
            continue
        seen.add(p["code"])
        fol = comment_following(it.get("has_follow"), avatar_obs.get(p["handle"]))
        out.append(dict(p, is_following=fol, source_post=f"{THREADS}/@{post_author}/post/{post_code}", source_author=post_author,
                        source_excerpt=source_excerpt[:80], is_post_author=(p["handle"] == post_author)))
    return out


def summarize_commenters(items: list[dict], self_handle: str = "", exclude_following: bool = True) -> list[dict]:
    """댓글 항목 → 댓글러 단위 집계(원글 작성자·본인 제외). 비팔로잉 확인 → 여러 글에 등장 → 댓글 수 많은 순."""
    me = normalize_handle(self_handle or "")
    by: dict[str, dict] = {}
    for order, it in enumerate(items):
        h = it["handle"]
        if it.get("is_post_author") or (me and h == me) or (exclude_following and it.get("is_following") is True) or not it.get("text"):
            continue                                       # 빈 댓글(이미지·스티커만)은 어댑터와 같게 후보 집계에서 뺀다
        c = by.setdefault(h, {"handle": h, "is_following": "unknown", "n_comments": 0, "n_posts": 0, "posts": [], "first_seen": order})
        c["n_comments"] += 1
        if it.get("source_post") not in c["posts"]:
            c["posts"].append(it.get("source_post"))
            c["n_posts"] = len(c["posts"])
        if it.get("is_following") is False:
            c["is_following"] = False
    return sorted(by.values(), key=lambda c: (c["is_following"] is not False, -c["n_posts"], -c["n_comments"], c["first_seen"]))


def replies_from_posts(posts: list[dict], handle: str) -> list[dict]:
    """/@h/replies 화면: [원글(타인) → h의 답글] 순서로 렌더링된다. h의 글마다 직전 타인 글 작성자를 reply_to로 붙인다."""
    h = normalize_handle(handle)
    out, parent = [], None
    for p in posts:
        if p["handle"] != h:
            parent = p
            continue
        out.append(dict(p, reply_to_author=parent["handle"] if parent else None,
                        parent_excerpt=(parent["text"][:80] if parent else None)))
    return out


# ---------------------------------------------------------------- bsk 호출기
class Bsk:
    def __init__(self, exe: str = "bsk", scope: str | None = None, timeout: float = 60, dry_run: bool = False, log=None):
        self.exe, self.scope, self.timeout, self.dry_run = exe, scope, timeout, dry_run
        self.plan: list[list[str]] = []
        self.log = log or (lambda s: print(s, file=sys.stderr))
        self.context: str | None = None
        self.opened: bool = False

    def run(self, *args: str, check: bool = True, observation: str | None = None) -> str:
        argv = list(args)
        _guard(argv)
        prefix: list[str] = []
        if self.scope and argv[0] != "status":
            prefix += ["--scope", self.scope]
        if observation:
            prefix += ["--observation", observation]
        full_cmd = [self.exe] + prefix + argv
        self.plan.append(full_cmd)
        if self.dry_run:
            return ""
        env = dict(os.environ, BSK_AUTO_START="0", PYTHONIOENCODING="utf-8")
        try:
            cp = subprocess.run(full_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=self.timeout, env=env, shell=False)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"bsk {argv[0]} 타임아웃 — 재시도하지 않습니다. `bsk sync`로 상태 확인 필요") from e
        if check and cp.returncode != 0:
            raise RuntimeError(f"bsk {argv[0]} 실패(exit {cp.returncode}): {(cp.stderr or cp.stdout)[-400:]}")
        return cp.stdout

    def sync_observation(self) -> str | None:
        if self.dry_run:
            return None
        out = self.run("sync", "--json", check=False)
        obj = parse_json_output(out)
        if isinstance(obj, dict):
            return obj.get("observation_id")
        return None

    def open(self, url: str) -> None:
        obs = self.sync_observation() if self.opened else None
        out = self.run("open", url, observation=obs)
        self.opened = True
        self.context = parse_context_id(out) or self.context

    def read(self, selector: str, fields: list[dict]) -> list[dict]:
        out = self.run("read", "--selector", selector, "--fields-json", json.dumps(fields, ensure_ascii=False))
        return iter_read_items(parse_json_output(out)) if out else []

    def read_comment_rows(self) -> list[dict]:
        """댓글창: 행 단위 text + has_follow(팔로우 오버레이). bsk read 우선, 중첩 컨테이너면 고정 READ_COMMENTS_JS 폴백."""
        try:
            return self.read(CONTAINER_SEL, COMMENT_FIELDS)
        except RuntimeError as e:
            if "multiple descendants" not in str(e):
                raise
            obs = self.sync_observation()
            out = self.run("evaluate", READ_COMMENTS_JS, observation=obs, check=False)
            res = parse_json_output(out)
            if isinstance(res, dict):
                res = res.get("result", res.get("value", []))
            return res if isinstance(res, list) else []

    def read_posts(self) -> list[dict]:
        """bsk read 우선; /replies·/post-comments처럼 한 컨테이너에 permalink가 여러 개면 고정 READ_POSTS_JS 폴백."""
        try:
            return self.read(CONTAINER_SEL, POST_FIELDS)
        except RuntimeError as e:
            if "multiple descendants" not in str(e):
                raise
            obs = self.sync_observation()
            out = self.run("evaluate", READ_POSTS_JS, observation=obs, check=False)
            res = parse_json_output(out)
            if isinstance(res, list):
                return res
            if isinstance(res, dict):
                val = res.get("result", res.get("value", []))
                return val if isinstance(val, list) else []
            return []

    def scroll(self) -> dict:
        obs = self.sync_observation()
        out = self.run("evaluate", SCROLL_JS, observation=obs, check=False)
        res = parse_json_output(out) or {}
        val = res.get("result", res.get("value", res)) if isinstance(res, dict) else {}
        if not (isinstance(val, dict) and val.get("moved")) or (isinstance(val, dict) and val.get("atEnd")):
            obs2 = self.sync_observation()
            self.run("wheel", "--delta-y", "1600", observation=obs2, check=False)
            val = dict(val or {}, fallback="wheel")
        return val if isinstance(val, dict) else {}

    def close(self) -> None:
        try:
            self.run("close", check=False)
        except Exception as e:  # noqa: BLE001 — 종료 실패는 보고만
            self.log(f"bsk close 실패: {e}")


def _wait(dry: bool, lo: float = 1.2, hi: float = 2.2) -> None:
    if not dry:
        time.sleep(random.uniform(lo, hi))


def scan_replies(b: Bsk, handle: str, max_items: int = 15, max_scrolls: int = 6) -> dict:
    h = normalize_handle(handle)
    posts: dict[str, dict] = {}
    order: list[str] = []
    b.open(f"{THREADS}/@{h}/replies")
    _wait(b.dry_run, 2.0, 3.0)
    for i in range(max_scrolls + 1):
        for p in posts_from_read(b.read_posts()):
            p.pop("is_following_hint", None)
            if p["code"] not in posts:
                posts[p["code"]] = p
                order.append(p["code"])
        reps = replies_from_posts([posts[c] for c in order], h)
        if len(reps) >= max_items or b.dry_run or i == max_scrolls:
            break
        b.scroll()
        _wait(b.dry_run)
    reps = replies_from_posts([posts[c] for c in order], h)[:max_items]
    return {"source": "replies", "handle": h, "scanned_at_kst": now_kst().isoformat(), "url": f"{THREADS}/@{h}/replies", "items": reps}


def scan_post_comments(b: Bsk, url: str, max_scrolls: int = 3, source_excerpt: str = "", reply_count: int | None = None) -> dict:
    """게시글 댓글창 수집. 팔로잉 판별은 comment_following(행 오버레이) — True로 오판해 댓글러를 버리지 않는다."""
    pc = parse_post_href(url)
    if not pc:
        raise SystemExit(f"게시글 URL 형식이 아닙니다: {url}")
    author, code = pc
    rows: dict[str, dict] = {}
    b.open(f"{THREADS}/@{author}/post/{code}")
    _wait(b.dry_run, 2.0, 3.0)
    avatar: dict[str, bool | str] = {}
    for i in range(max_scrolls + 1):
        for it in b.read_comment_rows():
            pcx = parse_post_href(it.get("permalink"))
            if pcx:
                prev = rows.get(pcx[1])
                rows[pcx[1]] = dict(it, has_follow=bool(it.get("has_follow")) or bool(prev and prev.get("has_follow")))
        for a in b.read(AVATAR_SEL, AVATAR_FIELDS):
            o = parse_avatar_label(a.get("label"), a.get("follow_svg"))
            if o and o[1] is False:                       # 댓글창 아바타는 비팔로잉 증거(False)만 채택
                avatar[o[0]] = False
        if b.dry_run or i == max_scrolls:
            break
        b.scroll()
        _wait(b.dry_run)
    excerpt = source_excerpt
    if not excerpt:
        head = [r for c, r in rows.items() if c == code]
        excerpt = clean_post_text(head[0].get("text"), author)[:80] if head else ""
    items = comments_from_rows(list(rows.values()), author, code, avatar, excerpt)
    return {"source": "post-comments", "post_url": f"{THREADS}/@{author}/post/{code}", "post_author": author, "post_code": code,
            "reply_count": reply_count, "source_excerpt": excerpt, "scanned_at_kst": now_kst().isoformat(), "items": items}


def scan_feed_comments(b: Bsk, max_posts: int = 8, min_replies: int = 3, max_feed_scrolls: int = 8, comments_scrolls: int = 3,
                       exclude_following: bool = True, self_handle: str = "", strategy: str = "magnet",
                       min_magnet_score: int = MAGNET_LOW_YIELD, query: str | None = None, now_ts: int | None = None) -> dict:
    """모드 1: 트랙 A(홈 피드, 기본) 또는 트랙 B(--query 키워드 검색)에서 글을 모아 rank_posts로 max_posts개를 고르고
    각 댓글창의 댓글러·댓글·원글 요약을 수집한다. 기본 strategy='magnet'(봇 유인도 우선, 저수율 일상·밈 글은 뒤로)."""
    posts: dict[str, dict] = {}
    warnings: list[str] = []
    scrolls, stagnant = 0, 0
    src_url = search_url(query) if query else THREADS + "/"
    b.open(src_url)
    _wait(b.dry_run, 2.0, 3.0)

    def pick():
        return rank_posts(posts.values(), strategy, min_replies, max_posts, min_magnet_score, self_handle, now_ts)

    while True:
        before = len(posts)
        for p in feed_posts_with_counts(b.read_posts()):
            if p["code"] not in posts:
                posts[p["code"]] = dict(p, seen_order=len(posts))
            else:
                posts[p["code"]]["reply_count"] = max(posts[p["code"]].get("reply_count") or 0, p.get("reply_count") or 0)
        _, _, n_good = pick()
        if n_good >= max_posts or scrolls >= max_feed_scrolls or b.dry_run:
            break
        stagnant = stagnant + 1 if len(posts) == before else 0
        if stagnant >= 4:
            warnings.append("스크롤 4회 연속 신규 글 없음 → 중단")
            break
        b.scroll()
        scrolls += 1
        _wait(b.dry_run)
    hot, rank_warn, n_good = pick()
    if not b.dry_run:
        warnings += rank_warn
        if len(hot) < max_posts:
            warnings.append(f"답글 {min_replies}개 이상 글 {len(hot)}개만 발견(목표 {max_posts}) → --min-replies 를 낮추거나 --max-feed-scrolls 를 늘리세요")
    dry_example = False
    if b.dry_run and not hot:
        dry_example = True                                 # 드라이런: 댓글창 단계 명령 계획을 보여 주기 위한 예시 1개
        ex = {"handle": "example_author", "code": "EXAMPLE", "url": f"{THREADS}/@example_author/post/EXAMPLE",
              "text": query or "", "reply_count": max(min_replies, 3), "taken_at_kst": None}
        m = score_bot_magnet(ex, now_ts)
        hot = [dict(ex, bot_magnet_score=m["score"], magnet_reasons=m["reasons"], is_low_yield=m["is_low_yield"])]
    items: list[dict] = []
    rows = []
    for p in hot:
        try:
            doc = scan_post_comments(b, p["url"], comments_scrolls, source_excerpt=(p.get("text") or "")[:80], reply_count=p.get("reply_count"))
        except RuntimeError as e:
            warnings.append(f"댓글창 수집 실패 @{p['handle']}/{p['code']}: {str(e)[:120]} → 건너뜀")
            continue
        rows.append({"author": p["handle"], "code": p["code"], "url": p["url"], "reply_count": p.get("reply_count"),
                     "bot_magnet_score": p.get("bot_magnet_score"), "magnet_reasons": p.get("magnet_reasons") or [],
                     "is_low_yield": p.get("is_low_yield"), "excerpt": (p.get("text") or "")[:80], "taken_at_kst": p.get("taken_at_kst"),
                     "comments_collected": len(doc["items"])})
        items += doc["items"]
        _wait(b.dry_run)
    seen, uniq = set(), []
    for it in items:
        if it["code"] not in seen:
            seen.add(it["code"])
            uniq.append(it)
    commenters = summarize_commenters(uniq, self_handle, exclude_following)
    unknown = sum(1 for c in commenters if c["is_following"] == "unknown")
    if unknown:
        warnings.append(f"팔로잉 판별 불가 댓글러 {unknown}명 → 'unknown'으로 포함(댓글창은 팔로잉을 True로 판정하지 않음)")
    return {"source": "feed-comments", "scanned_at_kst": now_kst().isoformat(), "url": src_url, "track": "B" if query else "A",
            "params": {"max_posts": max_posts, "min_replies": min_replies, "max_feed_scrolls": max_feed_scrolls,
                       "comments_scrolls": comments_scrolls, "exclude_following": exclude_following, "strategy": strategy,
                       "min_magnet_score": min_magnet_score, "query": query},
            "feed_scrolls": scrolls, "feed_seen": len(posts), "magnet_passed": n_good, "dry_run_example": dry_example,
            "selected_posts": rows, "posts": rows, "items": uniq,
            "commenters": commenters, "warnings": warnings}


def scan_blocked(b: Bsk, url: str = DEFAULT_BLOCKED_URL) -> dict:
    b.open(url)
    _wait(b.dry_run, 2.0, 3.0)
    seen, items = set(), []
    for it in b.read(LINK_SEL, LINK_FIELDS):
        h = parse_profile_href(it.get("href"))
        if not h or h in seen:
            continue
        seen.add(h)
        raw = re.sub(r"\s+", " ", it.get("text") or "").strip()
        name = raw[len(h):].strip() if raw.lower().startswith(h) else ""
        items.append({"handle": h, "display_name": name, "raw_ctx": raw})
    return {"source": "blocked-list", "scanned_at_kst": now_kst().isoformat(), "url": url, "items": items}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bsk_scan.py", description="bsk 읽기 전용 댓글창 수집기 (feed-comments · replies · post-comments · blocked-list)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", help="결과 JSON 경로 (생략 시 stdout)")
    common.add_argument("--bsk", default=os.environ.get("BSK_EXE", "bsk"), help="bsk 실행 파일 경로")
    common.add_argument("--scope", help="bsk 세션 scope 이름 (병렬 실행 시)")
    common.add_argument("--timeout", type=float, default=60, help="bsk 명령별 타임아웃(초). 초과 시 재시도하지 않음")
    common.add_argument("--dry-run", action="store_true", help="bsk를 실행하지 않고 계획된 명령만 출력")
    common.add_argument("--keep-open", action="store_true", help="끝나도 bsk close 하지 않음")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replies", parents=[common], help="@handle/replies 최근 답글 수집 (댓글 봇 진단 표본, 기본 15개)")
    r.add_argument("--handle", required=True)
    r.add_argument("--max-items", type=int, default=15)
    r.add_argument("--max-scrolls", type=int, default=6)
    fc = sub.add_parser("feed-comments", parents=[common], help="봇 유인도 상위 K개 글 댓글창 → 댓글러 수집 (모드 1, 트랙 A 홈 피드 / B --query)")
    fc.add_argument("--max-posts", type=int, default=8, help="댓글창을 열 게시글 수 (기본 8, 선택지 5/8/12)")
    fc.add_argument("--min-replies", type=int, default=3, help="답글 수가 이 값 이상인 글만 (기본 3)")
    fc.add_argument("--strategy", choices=("magnet", "replies"), default="magnet",
                    help="magnet=봇 유인도(AI·문제해결·창업·질문 글) 우선(기본) / replies=답글 많은 순")
    fc.add_argument("--min-magnet-score", type=int, default=MAGNET_LOW_YIELD,
                    help=f"magnet 전략에서 이 점수 미만 글(저수율 일상·밈)은 뒤로 미룸, 부족하면 폴백 (기본 {MAGNET_LOW_YIELD})")
    fc.add_argument("--query", help='트랙 B: 홈 피드 대신 키워드 검색 결과에서 선별 (예: "AI 해결", "바이브코딩")')
    fc.add_argument("--max-feed-scrolls", type=int, default=8)
    fc.add_argument("--comments-scrolls", type=int, default=3)
    fc.add_argument("--self", dest="self_handle", default=os.environ.get("THREADS_SELF_HANDLE", ""), help="본인 핸들(제외)")
    g2 = fc.add_mutually_exclusive_group()
    g2.add_argument("--exclude-following", dest="exclude_following", action="store_true", default=True, help="팔로잉 확인된 댓글러 제외(기본)")
    g2.add_argument("--include-following", dest="exclude_following", action="store_false")
    c = sub.add_parser("post-comments", parents=[common], help="게시글 댓글창 작성자·본문 수집 (모드 2)")
    c.add_argument("--url", required=True)
    c.add_argument("--max-scrolls", type=int, default=3)
    bl = sub.add_parser("blocked-list", parents=[common], help="차단 목록 읽기 전용 스냅샷 (모드 4)")
    bl.add_argument("--url", default=DEFAULT_BLOCKED_URL)
    return ap


def main(argv=None) -> int:
    setup_utf8()
    a = build_parser().parse_args(argv)
    if not a.dry_run and not shutil.which(a.bsk) and not Path(a.bsk).exists():
        print(f"bsk 실행 파일을 찾을 수 없습니다: {a.bsk} (references/bsk_notes.md 참고)", file=sys.stderr)
        return 2
    b = Bsk(a.bsk, a.scope, a.timeout, a.dry_run)
    try:
        if not a.dry_run:
            b.run("status", "--json")
        if a.cmd == "feed-comments":
            res = scan_feed_comments(b, a.max_posts, a.min_replies, a.max_feed_scrolls, a.comments_scrolls, a.exclude_following, a.self_handle,
                                     a.strategy, a.min_magnet_score, a.query)
        elif a.cmd == "replies":
            res = scan_replies(b, a.handle, a.max_items, a.max_scrolls)
        elif a.cmd == "post-comments":
            res = scan_post_comments(b, a.url, a.max_scrolls)
        else:
            res = scan_blocked(b, a.url)
    except (RuntimeError, ReadOnlyViolation) as e:
        print(f"중단: {e}", file=sys.stderr)
        if not a.keep_open:
            b.close()
        return 1
    if not a.keep_open:
        b.close()
    if a.dry_run:
        res["dry_run_plan"] = b.plan
    text = dump_json(res, a.out)
    print(text if not a.out else f"{a.cmd}: {len(res.get('items', []))}개 → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
