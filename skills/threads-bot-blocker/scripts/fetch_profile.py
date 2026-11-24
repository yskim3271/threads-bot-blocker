#!/usr/bin/env python3
"""비로그인 공개 SSR 수집기 — https://www.threads.com/@{handle}

로그인 세션에서는 이미 차단한 계정이 "콘텐츠를 이용할 수 없음"으로 가려지므로, 프로필·게시물은 항상
비로그인 SSR HTML에 내장된 JSON(<script type="application/json">)에서 추출한다.
출력: 계정 리스트({handle, status, profile, threads}) — reply_score.py --profiles 의 R5 프로필 신호(바이오·팔로워·쇼핑 링크·자동화 키워드·본문 0개) 입력.

- 캐시: <작업 디렉터리>/cache/{handle}_{YYYYMMDD}.json (24시간 TTL, 기본 ~/.threads-bot-blocker — 스킬 폴더에 쓰지 않음)
- 요청 간 2~4초 지터, 동시 1개
- 상태: ok / private / not_found / no_posts / error

사용:
  python scripts/fetch_profile.py <handle_a> @<handle_b> --out accounts.json [--group diagnose] [--no-cache]
  python scripts/fetch_profile.py --from-html saved.html --handle <handle>     # 오프라인 파싱(테스트·디버그)
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tbb_common import CACHE_DIR, dump_json, kst_iso, load_json, normalize_handle, now_kst, profile_url, setup_utf8  # noqa: E402

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
SCRIPT_JSON = re.compile(r"<script[^>]*type=\"application/json\"[^>]*>(.*?)</script>", re.S)
CACHE_TTL_SEC = 24 * 3600


# ---------------------------------------------------------------- HTML → JSON 블롭
def extract_json_blobs(html: str) -> list:
    blobs = []
    for m in SCRIPT_JSON.finditer(html or ""):
        body = m.group(1).strip()
        if '"thread_items"' not in body and '"follower_count"' not in body and '"username"' not in body:
            continue
        try:
            blobs.append(json.loads(body))
        except json.JSONDecodeError:
            continue
    return blobs


def _walk(obj):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def find_user(blobs: list, handle: str) -> dict | None:
    best = None
    for b in blobs:
        for d in _walk(b):
            if normalize_handle(str(d.get("username", ""))) == handle and ("follower_count" in d or "biography" in d):
                if best is None or len(d) > len(best):
                    best = d
    return best


def find_threads(blobs: list) -> list[dict]:
    """thread_items 를 가진 노드들을 등장 순서·중복 제거로 반환."""
    seen, out = set(), []
    for b in blobs:
        for d in _walk(b):
            items = d.get("thread_items")
            if isinstance(items, list) and items and isinstance(items[0], dict) and "post" in items[0]:
                key = items[0]["post"].get("pk") or items[0]["post"].get("code")
                if key in seen:
                    continue
                seen.add(key)
                out.append(d)
    out.sort(key=lambda t: -(t["thread_items"][0]["post"].get("taken_at") or 0))
    return out


# ---------------------------------------------------------------- 정규화
def normalize_post(post: dict, index: int) -> dict:
    info = post.get("text_post_app_info") or {}
    user = post.get("user") or {}
    author = normalize_handle(user.get("username", ""))
    code = post.get("code")
    lp = info.get("link_preview_attachment") or {}
    frags = ((info.get("text_fragments") or {}).get("fragments")) or []
    share = info.get("share_info") or {}
    rta = info.get("reply_to_author") or {}
    carousel = post.get("carousel_media") or []
    media_type = post.get("media_type")
    taken = int(post.get("taken_at") or 0)
    return {
        "index_in_thread": index, "code": code,
        "url": f"https://www.threads.com/@{author}/post/{code}" if code else None,
        "author": author, "taken_at": taken, "taken_at_kst": kst_iso(taken) if taken else None,
        "text": (post.get("caption") or {}).get("text") or "",
        "like_count": post.get("like_count") or 0,
        "reply_count": info.get("direct_reply_count") or 0,
        "repost_count": info.get("repost_count") or 0,
        "quote_count": info.get("quote_count") or 0,
        "media_type": media_type,
        "has_image": bool(post.get("image_versions2", {}).get("candidates")) or media_type in (1, 8),
        "has_video": bool(post.get("video_versions")) or media_type == 2,
        "carousel_count": len(carousel),
        "topic_tag": ((info.get("tag_header") or {}).get("display_name")),
        "link_url": lp.get("display_url") or None,
        "link_title": lp.get("title") or None,
        "is_reply": bool(info.get("is_reply")),
        "reply_to_author": normalize_handle(rta.get("username", "")) or None,
        "has_quote": bool(share.get("quoted_post")),
        "has_repost": bool(share.get("reposted_post")),
        "text_fragments_types": [f.get("fragment_type") for f in frags] or ["plaintext"],
    }


def normalize_profile(user: dict) -> dict:
    return {
        "pk": str(user.get("pk") or user.get("id") or ""), "id": str(user.get("id") or user.get("pk") or ""),
        "username": normalize_handle(user.get("username", "")), "full_name": user.get("full_name") or "",
        "biography": user.get("biography") or "", "follower_count": user.get("follower_count") or 0,
        "is_verified": bool(user.get("is_verified")),
        "bio_links": [{"url": l.get("url"), "lynx_url": l.get("lynx_url"), "link_id": str(l.get("link_id") or ""),
                       "title": l.get("title") or "", "is_verified": bool(l.get("is_verified"))} for l in user.get("bio_links") or []],
        "text_post_app_is_private": bool(user.get("text_post_app_is_private")),
    }


def parse_profile_html(html: str, handle: str, group: str = "diagnose", display_name: str = "") -> dict:
    handle = normalize_handle(handle)
    acc = {"handle": handle, "display_name_from_list": display_name, "group": group, "fetched_at_kst": now_kst().isoformat(),
           "status": "ok", "profile": None, "threads": [], "posts_flat": [], "post_count_fetched": 0, "own_post_count_fetched": 0}
    blobs = extract_json_blobs(html)
    user = find_user(blobs, handle)
    if not user:
        acc["status"] = "not_found"
        return acc
    acc["profile"] = normalize_profile(user)
    if acc["profile"]["text_post_app_is_private"]:
        acc["status"] = "private"
        return acc
    for t in find_threads(blobs):
        posts = [normalize_post(it["post"], i) for i, it in enumerate(t["thread_items"])]
        tid = str(t.get("id") or (t["thread_items"][0]["post"].get("pk")) or "")
        acc["threads"].append({"thread_id": tid, "item_count": len(posts), "posts": posts})
        for p in posts:
            acc["posts_flat"].append(dict(p, thread_group_size=len(posts)))
    acc["post_count_fetched"] = len(acc["posts_flat"])
    acc["own_post_count_fetched"] = sum(1 for p in acc["posts_flat"] if p["author"] == handle)
    if not acc["threads"]:
        acc["status"] = "no_posts"
    return acc


# ---------------------------------------------------------------- 네트워크 + 캐시
def cache_path(handle: str, cache_dir: Path = CACHE_DIR) -> Path:
    return Path(cache_dir) / f"{normalize_handle(handle)}_{now_kst():%Y%m%d}.json"


def load_cached(handle: str, cache_dir: Path = CACHE_DIR, ttl: int = CACHE_TTL_SEC) -> dict | None:
    files = sorted(Path(cache_dir).glob(f"{normalize_handle(handle)}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        if time.time() - p.stat().st_mtime <= ttl:
            try:
                return load_json(p)
            except (OSError, json.JSONDecodeError):
                return None
    return None


def fetch_html(handle: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(
        profile_url(handle),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Fetch-Mode": "navigate",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (고정된 https 도메인)
        return resp.read().decode("utf-8", errors="replace")


def fetch_account(handle: str, group: str = "diagnose", use_cache: bool = True, cache_dir: Path = CACHE_DIR) -> dict:
    handle = normalize_handle(handle)
    if use_cache:
        cached = load_cached(handle, cache_dir)
        if cached:
            cached["from_cache"] = True
            return cached
    try:
        acc = parse_profile_html(fetch_html(handle), handle, group)
    except urllib.error.HTTPError as e:
        acc = {"handle": handle, "group": group, "status": "not_found" if e.code == 404 else "error", "error": f"HTTP {e.code}",
               "fetched_at_kst": now_kst().isoformat(), "profile": None, "threads": []}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        acc = {"handle": handle, "group": group, "status": "error", "error": str(e), "fetched_at_kst": now_kst().isoformat(),
               "profile": None, "threads": []}
    if acc.get("status") in ("ok", "private", "no_posts"):
        dump_json(acc, cache_path(handle, cache_dir))
    return acc


def fetch_many(handles: list[str], group: str = "diagnose", use_cache: bool = True, cache_dir: Path = CACHE_DIR,
               jitter: tuple[float, float] = (2.0, 4.0), log=print) -> list[dict]:
    out = []
    for i, h in enumerate(dict.fromkeys(normalize_handle(x) for x in handles if x.strip())):
        acc = fetch_account(h, group, use_cache, cache_dir)
        out.append(acc)
        log(f"[{i + 1}] @{h}: {acc.get('status')}{' (캐시)' if acc.get('from_cache') else ''} · 루트 {len(acc.get('threads') or [])}개")
        if not acc.get("from_cache") and i < len(handles) - 1:
            time.sleep(random.uniform(*jitter))
    return out


def main(argv=None) -> int:
    setup_utf8()
    ap = argparse.ArgumentParser(description="Threads 공개 SSR 프로필 수집기 (비로그인, 읽기 전용)")
    ap.add_argument("handles", nargs="*", help="@핸들 또는 프로필 URL")
    ap.add_argument("--handles-file", help="핸들 목록 JSON: feed_candidates 출력, 하네스 문서(items), adapter blocked-diff 출력(added), 문자열 리스트")
    ap.add_argument("--group", default="diagnose")
    ap.add_argument("--out", help="계정 리스트 JSON 출력 경로")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    ap.add_argument("--from-html", help="저장된 HTML 파일을 오프라인 파싱 (--handle 필요)")
    ap.add_argument("--handle", help="--from-html 대상 핸들")
    a = ap.parse_args(argv)
    if a.from_html:
        if not a.handle:
            ap.error("--from-html 에는 --handle 이 필요합니다")
        res = [parse_profile_html(Path(a.from_html).read_text(encoding="utf-8"), a.handle, a.group)]
    else:
        handles = list(a.handles)
        if a.handles_file:
            doc = load_json(a.handles_file)
            if isinstance(doc, dict):  # feed_candidates 출력({"candidates"}) 또는 하네스 문서({"items"}, blocked-list 등)
                doc = doc.get("candidates") or doc.get("items") or doc.get("added") or []
            handles += [h if isinstance(h, str) else (h.get("handle") or "") for h in doc if isinstance(h, (str, dict))]
        if not handles:
            ap.error("핸들을 1개 이상 지정하세요")
        res = fetch_many(handles, a.group, not a.no_cache, Path(a.cache_dir), log=lambda s: print(s, file=sys.stderr))
    text = dump_json(res, a.out)
    if not a.out:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
