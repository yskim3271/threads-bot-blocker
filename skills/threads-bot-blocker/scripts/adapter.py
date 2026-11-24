#!/usr/bin/env python3
"""브라우저 하네스 어댑터 — 어떤 브라우저 도구(bsk, aside, Playwright MCP, Chrome 확장 등)로 수집했든
references/browser_adapter.md 의 최소 JSON 계약으로 검증·정규화한다 (v3.0, 댓글 봇 단일 타겟).

최소 계약 (source별 필수 키):
  feed-comments {"source":"feed-comments","items":[{"handle","text"}]}      (+ code/url, taken_at, is_following,
                 source_post|post_url, source_author, source_excerpt 선택; posts:[{author,code|url,reply_count}] 선택)
                → 댓글러 단위 commenters 자동 집계, 원글 작성자 댓글 is_post_author 표시 (모드 1)
  feed          {"source":"feed","items":[{"handle"}]}  (+ code, url, text, reply_count — rank-posts 입력용 피드·검색 카드)
  replies       {"source":"replies","handle","items":[{"text"}]}           (+ taken_at, reply_to_author 선택)
  post-comments {"source":"post-comments","post_author","items":[{"handle"}]}  (+ text, code 선택)
  blocked-list  {"source":"blocked-list","items":[{"handle"}]}

관대한 입력: items 대신 리스트만 줘도 되고(--expect 필요), 항목이 문자열이면 핸들(또는 답글 본문)로 본다.
URL만 있으면 핸들·게시물 코드를 URL에서 뽑는다. 본문은 UI 크롬을 정제한다.

사용:
  python scripts/adapter.py validate feed_comments.json --expect feed-comments --out feed_comments.norm.json
  python scripts/adapter.py validate feed.json --expect feed --out feed.norm.json
  python scripts/adapter.py blocked-diff --new blocked.json --old data/blocked_accounts.json --out diff.json
  python scripts/adapter.py rank-posts --input feed.json --top 8      # 어떤 글의 댓글창을 먼저 열지(bot_magnet_score, 하네스 공용)
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tbb_common import (KST, MAGNET_LOW_YIELD, clean_ui_text, dump_json, load_json, normalize_handle, now_kst, parse_counts,  # noqa: E402
                        rank_posts, setup_utf8)

SOURCES = ("feed-comments", "replies", "post-comments", "blocked-list", "feed")
POST_URL = re.compile(r"/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)")
HANDLE_OK = re.compile(r"^[a-z0-9._]{1,30}$")
ALIASES = {"comments": "post-comments", "post_comments": "post-comments", "blocked": "blocked-list", "blocked_list": "blocked-list",
           "home": "feed", "reply": "replies", "feed_comments": "feed-comments", "home-comments": "feed-comments",
           "home_comments": "feed-comments"}
OPTIONAL_HANDLE = ("replies",)   # 항목 handle 생략 가능(문서 handle로 보완)


class AdapterError(ValueError):
    pass


def _following(v):
    if v is True or v is False:
        return v
    if isinstance(v, str):
        x = v.strip().lower()
        if x in ("true", "following", "yes", "1"):
            return True
        if x in ("false", "not_following", "no", "0"):
            return False
    return "unknown"


def parse_taken_at(it: dict) -> int | None:
    """taken_at(epoch 초/밀리초, 숫자 문자열) → taken_at_kst/datetime/time(ISO) 순으로 epoch 초를 구한다."""
    v = it.get("taken_at")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return int(v / 1000) if v > 1e11 else int(v)
    if isinstance(v, str) and v.strip().replace(".", "", 1).isdigit():
        x = float(v)
        return int(x / 1000) if x > 1e11 else int(x)
    for k in ("taken_at_kst", "datetime", "time", "taken_at"):
        iso = it.get(k)
        if isinstance(iso, str) and iso.strip():
            try:
                d = dt.datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
                return int((d if d.tzinfo else d.replace(tzinfo=KST)).timestamp())
            except ValueError:
                continue
    return None


def _norm_item(it, source: str, warnings: list, idx: int) -> dict | None:
    if isinstance(it, str):
        it = {"text": it} if source == "replies" else {"handle": it}
    if not isinstance(it, dict):
        warnings.append(f"items[{idx}] 객체/문자열 아님 → 건너뜀")
        return None
    out = dict(it)
    url = out.get("url") or out.get("permalink") or ""
    m = POST_URL.search(str(url))
    h = normalize_handle(str(out.get("handle") or out.get("username") or out.get("author") or (m.group(1) if m else "") or
                             (url if "/@" in str(url) else "")))
    if m and not out.get("code"):
        out["code"] = m.group(2)
    if source not in OPTIONAL_HANDLE:
        if not h or not HANDLE_OK.match(h):
            warnings.append(f"items[{idx}] 유효한 handle 없음 → 건너뜀")
            return None
        out["handle"] = h
    elif h:
        out["handle"] = h
    if "text" in out or source in OPTIONAL_HANDLE:
        out["text"] = clean_ui_text(out.get("text"))
        if source == "replies" and not out["text"]:
            warnings.append(f"items[{idx}] 빈 답글 → 건너뜀")
            return None
    if source in ("feed", "post-comments", "feed-comments"):
        out["is_following"] = _following(out.get("is_following", out.get("is_following_hint")))
    if source == "feed-comments":
        sp = str(out.get("source_post") or out.get("post_url") or "")
        mp = POST_URL.search(sp)
        if mp:
            out["source_post"] = f"https://www.threads.com/@{normalize_handle(mp.group(1))}/post/{mp.group(2)}"
            out.setdefault("source_author", mp.group(1))
        if out.get("source_author"):
            out["source_author"] = normalize_handle(str(out["source_author"]))
        out["is_post_author"] = bool(out.get("source_author")) and out["source_author"] == out["handle"]
        tk = parse_taken_at(out)
        if tk:
            out["taken_at"] = tk
            out.setdefault("taken_at_kst", dt.datetime.fromtimestamp(tk, KST).isoformat())
        if not out.get("text"):
            warnings.append(f"items[{idx}] 빈 댓글 → 건너뜀")
            return None
    if out.get("reply_to_author"):
        out["reply_to_author"] = normalize_handle(str(out["reply_to_author"]))
    return out


def normalize_doc(doc, expect: str | None = None) -> tuple[dict, list[str]]:
    """하네스 출력 → 표준 문서. 치명적 오류는 AdapterError, 경미한 문제는 warnings로 반환."""
    warnings: list[str] = []
    if isinstance(doc, list):
        if not expect:
            raise AdapterError("리스트 입력은 --expect(source)가 필요합니다")
        doc = {"source": expect, "items": doc}
    if not isinstance(doc, dict):
        raise AdapterError("JSON 최상위는 객체 또는 리스트여야 합니다")
    source = ALIASES.get(str(doc.get("source") or expect or "").strip(), str(doc.get("source") or expect or "").strip())
    if source not in SOURCES:
        raise AdapterError(f"source 를 알 수 없음: {doc.get('source')!r} (허용: {', '.join(SOURCES)})")
    if expect and ALIASES.get(expect, expect) != source:
        raise AdapterError(f"기대한 source={expect} 인데 입력은 {source}")
    raw = doc.get("items")
    if raw is None:
        raw = doc.get("candidates") or doc.get("comments") or doc.get("replies") or []
    if not isinstance(raw, list):
        raise AdapterError("items 는 리스트여야 합니다")
    items = [x for x in (_norm_item(it, source, warnings, i) for i, it in enumerate(raw)) if x]
    out = {k: v for k, v in doc.items() if k not in ("items", "candidates", "comments", "replies")}
    out.update(source=source, items=items)
    if source == "replies":
        h = normalize_handle(str(doc.get("handle") or ""))
        if not h:
            hs = {i.get("handle") for i in items if i.get("handle")}
            h = hs.pop() if len(hs) == 1 else ""
        if not h:
            raise AdapterError("replies 문서에는 handle(답글 작성자)이 필요합니다")
        out["handle"] = h
    if source == "post-comments":
        pa = normalize_handle(str(doc.get("post_author") or ""))
        m = POST_URL.search(str(doc.get("post_url") or ""))
        if not pa and m:
            pa = normalize_handle(m.group(1))
        if not pa:
            warnings.append("post_author 없음 → 원글 작성자 제외를 건너뜀")
        out["post_author"] = pa
    if source == "feed-comments":
        kept, dup = {}, 0
        for i in items:
            key = i.get("code") or f"{i['handle']}:{i.get('source_post')}:{i['text'][:40]}"
            if key in kept:
                dup += 1
                continue
            kept[key] = i
        items = list(kept.values())
        out["items"] = items
        if dup:
            warnings.append(f"중복 댓글 {dup}개 제외")
        fol_true = sum(1 for i in items if i.get("is_following") is True)
        if fol_true:
            warnings.append(f"is_following=True 댓글 {fol_true}개: 댓글창 DOM 추론이면 오판일 수 있음(텍스트가 핸들로 시작 ≠ 팔로잉) "
                            "→ 확실하지 않으면 \"unknown\"으로 수집하세요")
        posts = []
        for pst in doc.get("posts") or []:
            if not isinstance(pst, dict):
                continue
            mp = POST_URL.search(str(pst.get("url") or ""))
            a = normalize_handle(str(pst.get("author") or (mp.group(1) if mp else "")))
            c = pst.get("code") or (mp.group(2) if mp else None)
            if a and c:
                posts.append(dict(pst, author=a, code=c, url=pst.get("url") or f"https://www.threads.com/@{a}/post/{c}"))
        out["posts"] = posts
        by: dict[str, dict] = {}
        for order, i in enumerate(items):
            if i.get("is_post_author"):
                continue
            c = by.setdefault(i["handle"], {"handle": i["handle"], "is_following": "unknown", "n_comments": 0, "posts": [], "first_seen": order})
            c["n_comments"] += 1
            if i.get("source_post") and i["source_post"] not in c["posts"]:
                c["posts"].append(i["source_post"])
            if i.get("is_following") is False:
                c["is_following"] = False
            elif i.get("is_following") is True and c["is_following"] == "unknown":
                c["is_following"] = True
        for c in by.values():
            c["n_posts"] = len(c["posts"])
        out["commenters"] = sorted(by.values(), key=lambda c: (c["is_following"] is not False, -c["n_posts"], -c["n_comments"], c["first_seen"]))
    if not items:
        warnings.append("유효한 항목 0개")
    return out, warnings


def posts_for_ranking(doc) -> tuple[list[dict], list[str]]:
    """rank-posts 입력: feed 문서({"items":[...]}), feed-comments 문서({"posts" 또는 "selected_posts"}), 또는 게시글 리스트.
    각 항목: handle(또는 url/author), code(또는 url), text(원문 OK — 반응 수 꼬리에서 reply_count 추출), reply_count, taken_at(선택)."""
    warnings: list[str] = []
    if isinstance(doc, dict):
        raw = doc.get("items") or doc.get("selected_posts") or doc.get("posts") or doc.get("candidates") or []
    else:
        raw = doc if isinstance(doc, list) else []
    out = []
    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            warnings.append(f"items[{i}] 객체 아님 → 건너뜀")
            continue
        url = str(it.get("url") or it.get("permalink") or "")
        m = POST_URL.search(url)
        h = normalize_handle(str(it.get("handle") or it.get("author") or (m.group(1) if m else "")))
        code = it.get("code") or (m.group(2) if m else None)
        if not h or not code:
            warnings.append(f"items[{i}] handle/code(url) 없음 → 건너뜀")
            continue
        raw_text = str(it.get("text") or it.get("excerpt") or "")
        rc = it.get("reply_count")
        if rc in (None, ""):
            rc = (parse_counts(raw_text) or {}).get("reply", 0)
        try:
            rc = int(rc)
        except (TypeError, ValueError):
            rc = 0
        text = clean_ui_text(raw_text)
        if text.lower().startswith(h):                   # 카드 textContent 앞의 '{handle} 1시간' 접두 제거
            text = re.sub(r"^\s*\d+\s*(?:초|분|시간|일|주)\s*", "", text[len(h):]).strip()
        out.append({"handle": h, "code": code, "url": url or f"https://www.threads.com/@{h}/post/{code}", "text": text,
                    "reply_count": rc, "taken_at": parse_taken_at(it), "seen_order": i})
    return out, warnings


def blocked_diff(new_doc, old_doc) -> dict:
    new, _ = normalize_doc(new_doc, "blocked-list")
    old_items = old_doc if isinstance(old_doc, list) else (old_doc or {}).get("items", [])
    old = {normalize_handle(x if isinstance(x, str) else x.get("handle", "")) for x in old_items} - {""}
    cur = [i["handle"] for i in new["items"]]
    return {"generated_at_kst": now_kst().isoformat(), "added": [h for h in cur if h not in old],
            "removed": sorted(old - set(cur)), "total_now": len(cur)}


def main(argv=None) -> int:
    setup_utf8()
    ap = argparse.ArgumentParser(description="브라우저 하네스 출력 검증·정규화 (browser_adapter.md 계약)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="feed-comments/replies/post-comments/blocked-list/feed JSON 검증·정규화")
    v.add_argument("input")
    v.add_argument("--expect", choices=SOURCES)
    v.add_argument("--out")
    b = sub.add_parser("blocked-diff", help="새 차단 목록과 스냅샷 비교(모드 4)")
    b.add_argument("--new", required=True)
    b.add_argument("--old", required=True)
    b.add_argument("--out")
    rk = sub.add_parser("rank-posts", help="봇 유인도(bot_magnet_score)로 댓글창을 열 게시글 선별 (bsk_scan feed-comments와 같은 엔진)")
    rk.add_argument("--input", required=True, help="feed.json(홈 피드·검색 결과 카드) 또는 게시글 리스트")
    rk.add_argument("--top", type=int, default=8, help="고를 게시글 수 (기본 8)")
    rk.add_argument("--strategy", choices=("magnet", "replies"), default="magnet")
    rk.add_argument("--min-replies", type=int, default=3)
    rk.add_argument("--min-magnet-score", type=int, default=MAGNET_LOW_YIELD)
    rk.add_argument("--self", dest="self_handle", default="")
    rk.add_argument("--now", type=int, help="기준 시각 epoch 초(신선도 계산, 기본 현재)")
    rk.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "validate":
            doc, warns = normalize_doc(load_json(a.input), a.expect)
            dump_json(doc, a.out)
            extra = f" commenters={len(doc['commenters'])}" if doc["source"] == "feed-comments" else ""
            print(f"OK source={doc['source']} items={len(doc['items'])}{extra}" + (f" → {a.out}" if a.out else ""))
            for w in warns:
                print(f"  ⚠️ {w}")
        elif a.cmd == "rank-posts":
            posts, warns = posts_for_ranking(load_json(a.input))
            sel, rw, n_good = rank_posts(posts, a.strategy, a.min_replies, a.top, a.min_magnet_score, a.self_handle, a.now)
            res = {"source": "rank-posts", "generated_at_kst": now_kst().isoformat(),
                   "params": {"top": a.top, "strategy": a.strategy, "min_replies": a.min_replies, "min_magnet_score": a.min_magnet_score},
                   "n_input": len(posts), "magnet_passed": n_good,
                   "selected_posts": [{k: p.get(k) for k in ("handle", "code", "url", "reply_count", "bot_magnet_score", "magnet_reasons",
                                                            "is_low_yield")} | {"excerpt": (p.get("text") or "")[:80]} for p in sel],
                   "warnings": warns + rw}
            dump_json(res, a.out)
            print(f"선별 {len(sel)}개 / 입력 {len(posts)}개 (strategy={a.strategy}, 유인도 {a.min_magnet_score}+ {n_good}개)" + (f" → {a.out}" if a.out else ""))
            for i, p in enumerate(res["selected_posts"], 1):
                print(f"  {i}. {p['bot_magnet_score']:>4} · 답글 {p['reply_count']:>3} · {p['url']}  [{','.join(p['magnet_reasons'])}]")
            for w in res["warnings"]:
                print(f"  ⚠️ {w}")
        else:
            old = load_json(a.old) if Path(a.old).exists() else []
            res = blocked_diff(load_json(a.new), old)
            dump_json(res, a.out)
            print(f"추가 {len(res['added'])} · 해제 {len(res['removed'])} · 현재 {res['total_now']}" + (f" → {a.out}" if a.out else ""))
    except (AdapterError, ValueError) as e:
        print(f"입력 오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
