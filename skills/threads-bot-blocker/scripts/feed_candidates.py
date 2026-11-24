#!/usr/bin/env python3
"""댓글창 수집 결과(feed-comments / post-comments) → 댓글 봇 진단 후보(댓글러) 목록 (v3.0).

처리 순서: 핸들 정규화 → 본인 제외 → 원글 작성자 제외 → 팔로잉 제외(is_following == True)
          → 같은 댓글 중복 제거 → 최근 진단 캐시 제외 → 댓글러 단위 집계
          → 정렬(비팔로잉 확인 > 여러 글에 등장 > 댓글 수) → --max-commenters(기본 20)명
이 후보로 `bsk_scan.py replies --handle <h> --max-items 15` 와 `fetch_profile.py --handles-file` 을 돌린다.

is_following 값: True(팔로잉) / False(비팔로잉, 행에 '팔로우' 오버레이) / "unknown"(판별 불가 — 기본 포함)

사용:
  python scripts/feed_candidates.py --input feed_comments.json --self <your_handle> --out candidates.json
  python scripts/feed_candidates.py --input comments.json --max-commenters 10      # THREADS_SELF_HANDLE 환경변수도 가능
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapter import AdapterError, normalize_doc  # noqa: E402
from tbb_common import CACHE_DIR, dump_json, load_json, normalize_handle, now_kst, profile_url, self_handle_default, setup_utf8  # noqa: E402

DEFAULT_MAX_COMMENTERS = 20
DEFAULT_SELF = ""  # 하드코딩 금지: --self 또는 THREADS_SELF_HANDLE (없으면 본인 제외 생략)
COMMENT_SOURCES = ("feed-comments", "post-comments")


def _following_value(v):
    if v is True or v is False:
        return v
    if isinstance(v, str) and v.lower() in ("true", "following", "yes"):
        return True
    if isinstance(v, str) and v.lower() in ("false", "not_following", "no"):
        return False
    return "unknown"


def recently_cached(handle: str, cache_dir: Path, days: float) -> bool:
    if days <= 0 or not Path(cache_dir).exists():
        return False
    limit = time.time() - days * 86400
    return any(p.stat().st_mtime >= limit for p in Path(cache_dir).glob(f"{handle}_*.json"))


def build_candidates(docs: list[dict], self_handle: str | None = None, max_commenters: int = DEFAULT_MAX_COMMENTERS,
                     exclude_following: bool = True, include_unknown: bool = True, cache_dir: Path | None = None,
                     cache_days: float = 0) -> dict:
    me = normalize_handle(self_handle if self_handle is not None else self_handle_default())
    warnings: list[str] = []
    norm = []
    for i, d in enumerate(docs):
        try:
            nd, w = normalize_doc(d, None if isinstance(d, dict) and d.get("source") else "post-comments")
            warnings += [f"입력{i + 1}: {x}" for x in w]
            if nd["source"] not in COMMENT_SOURCES:
                warnings.append(f"입력{i + 1} 건너뜀: 댓글창 문서(feed-comments/post-comments)가 아님({nd['source']})")
                continue
            norm.append(nd)
        except AdapterError as e:
            warnings.append(f"입력{i + 1} 건너뜀: {e}")
    excluded = {"self": 0, "post_author": 0, "following": 0, "unknown": 0, "duplicate": 0, "cached": 0, "over_limit": 0, "invalid": 0}
    seen: set[str] = set()
    comments: list[dict] = []
    for doc in norm:
        source = doc["source"]
        post_author = normalize_handle(doc.get("post_author") or "")
        for it in doc.get("items", []):
            h = normalize_handle(it.get("handle") or "")
            if not h:
                excluded["invalid"] += 1
                continue
            if h == me:
                excluded["self"] += 1
                continue
            if it.get("is_post_author") or normalize_handle(it.get("source_author") or "") == h or (post_author and h == post_author):
                excluded["post_author"] += 1
                continue
            fol = _following_value(it.get("is_following"))
            if exclude_following and fol is True:
                excluded["following"] += 1
                continue
            if fol == "unknown" and not include_unknown:
                excluded["unknown"] += 1
                continue
            key = it.get("code") or it.get("url") or f"{h}:{(it.get('text') or '')[:40]}"
            if key in seen:
                excluded["duplicate"] += 1
                continue
            seen.add(key)
            if cache_dir and recently_cached(h, cache_dir, cache_days):
                excluded["cached"] += 1
                continue
            comments.append(dict(it, handle=h, is_following=fol, source=source,
                                 source_post=it.get("source_post") or doc.get("post_url")))
    by: dict[str, dict] = {}
    for order, it in enumerate(comments):
        c = by.setdefault(it["handle"], {"handle": it["handle"], "profile_url": profile_url(it["handle"]), "is_following": it["is_following"],
                                         "source": it["source"], "first_seen": order, "posts": [], "source_posts": [], "n_comments": 0,
                                         "comment_samples": []})
        c["n_comments"] += 1
        if it.get("code"):
            c["posts"].append(it["code"])
        if it.get("source_post") and it["source_post"] not in c["source_posts"]:
            c["source_posts"].append(it["source_post"])
        if it.get("text"):
            c["comment_samples"].append({"text": it["text"][:200], "taken_at_kst": it.get("taken_at_kst")})
        if c["is_following"] == "unknown" and it["is_following"] is False:
            c["is_following"] = False
    cands = sorted(by.values(), key=lambda c: (c["is_following"] is not False, -len(c["source_posts"]), -c["n_comments"], c["first_seen"]))
    limit = max_commenters or DEFAULT_MAX_COMMENTERS
    if len(cands) > limit:
        excluded["over_limit"] = len(cands) - limit
        cands = cands[:limit]
    return {"generated_at_kst": now_kst().isoformat(),
            "params": {"self": me, "max_commenters": limit, "exclude_following": exclude_following, "include_unknown": include_unknown,
                       "cache_days": cache_days},
            "comments_used": len(comments), "candidates": cands, "excluded": excluded, "warnings": warnings, "self_excluded": bool(me)}


def main(argv=None) -> int:
    setup_utf8()
    ap = argparse.ArgumentParser(description="댓글창 수집 결과 → 댓글 봇 진단 후보(댓글러)")
    ap.add_argument("--input", nargs="+", required=True, help="bsk_scan.py feed-comments / post-comments 출력 JSON")
    ap.add_argument("--self", dest="self_handle", default=None,
                    help="본인 핸들(제외). 생략 시 THREADS_SELF_HANDLE 환경변수, 그것도 없으면 본인 제외 생략")
    ap.add_argument("--max-commenters", "--max-handles", dest="max_commenters", type=int, default=DEFAULT_MAX_COMMENTERS,
                    help=f"후보 댓글러 수 상한 (기본 {DEFAULT_MAX_COMMENTERS})")
    ap.add_argument("--include-following", action="store_true", help="팔로잉 계정도 포함 (기본: 제외)")
    ap.add_argument("--exclude-unknown", action="store_true", help="팔로잉 여부 판별 불가 계정 제외")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    ap.add_argument("--cache-days", type=float, default=7, help="이 기간 내 진단한 계정 제외 (0이면 미적용)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    res = build_candidates([load_json(p) for p in a.input], a.self_handle, a.max_commenters, not a.include_following,
                           not a.exclude_unknown, Path(a.cache_dir), a.cache_days)
    text = dump_json(res, a.out)
    if a.out:
        print(f"후보 {len(res['candidates'])}명 (댓글 {res['comments_used']}개) · 제외 {res['excluded']} → {a.out}")
        if not res["self_excluded"]:
            print("  본인 핸들 미지정 → 본인 제외 생략 (--self 또는 THREADS_SELF_HANDLE)")
        for w in res["warnings"]:
            print(f"  ⚠️ {w}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
