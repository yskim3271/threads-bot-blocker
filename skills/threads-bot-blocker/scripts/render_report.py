#!/usr/bin/env python3
"""reply_score.py(댓글 봇) 결과 JSON → 리포트 (threads-bot-blocker v4.0, 시안 B: 요약표 + 증거 댓글).

기본 `--format cli`: 80~120컬럼 터미널용 텍스트(넓은 마크다운 표·HTML 없음, 한글·이모지 2칸 계산).
  1. 헤더 1줄   `Threads 댓글 봇 스캔 (YYYY-MM-DD HH:MM KST)  │  스캔 N명 · 감지 M명`
  2. 요약표     `#  계정  점수  판정  핵심 사유` — 공백 칼정렬(pad_right/pad_left, display_width 기준)
  3. [증거 댓글] 계정별 `n. @handle  (프로필)` + 증거 3줄(댓글 원문 → 출처·쉬운 설명 → 직접 링크, URL은 자르지 않음)
     + 필요할 때만 `참고:`. 사유·설명은 기호·분수·내부 용어 없이 일상 한국어로 쓴다.
  4. 차단 가이드 2줄
  정상 계정 관련 문구는 기본 출력에 전혀 없다. --verbose 에서만 세부 점수(세부·합계·지표)와 🟢 정상 목록을 덧붙인다.
  v4.0 종합 판정(judgment_mode=llm_v4) 계정은 핵심 사유에 LLM이 쓴 summary_reasons를, [증거 댓글]에 원문 대조를 통과한
  verified_evidence(답글·프로필 게시글·프로필 소개)를 쓴다. 그 밖(llm_v3·quant_only·quant_fallback)은 v3.0 표시 그대로.
`--format markdown`: 같은 구조(5열 표 + [증거 댓글] + `[댓글 보기](url)`).

사용:
  python scripts/render_report.py --input reply_results.json                 # CLI 리포트를 stdout으로
  python scripts/render_report.py --input reply_results.json --verbose --width 120
  python scripts/render_report.py --input reply_results.json --format markdown --out report.md
에이전트는 이 출력을 재작성·요약하지 말고 그대로 사용자에게 보여 준다(SKILL.md 규칙).
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tbb_common import load_json, now_kst, setup_utf8  # noqa: E402

CONF_ICON = {"높음": "●●●", "보통": "●●○", "낮음": "●○○"}
MAX_BLOCK_PER_TURN = 5
DEFAULT_WIDTH = 100


# ================================================================ 표시 폭 유틸 (터미널 컬럼 기준)
def char_width(ch: str) -> int:
    o = ord(ch)
    if o in (0x200D, 0xFE0F, 0xFE0E) or unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F") or 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF:
        return 2
    return 1


def display_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def truncate(s: str, cols: int) -> str:
    s = str(s or "")
    lead = s[: len(s) - len(s.lstrip(" "))]            # 들여쓰기는 보존
    s = lead + " ".join(s.split())
    if display_width(s) <= cols:
        return s
    out, w = [], 0
    for c in s:
        cw = char_width(c)
        if w + cw > cols - 1:
            break
        out.append(c)
        w += cw
    return "".join(out).rstrip() + "…"


def fit(line: str, width: int) -> str:
    return line if display_width(line) <= width else truncate(line, width)


def wrap_tokens(prefix: str, tokens: list[str], width: int, sep: str = " · ", cont: str | None = None) -> list[str]:
    cont = cont if cont is not None else " " * display_width(prefix)
    lines, cur = [], prefix
    first = True
    for tok in tokens:
        cand = cur + ("" if first else (" " if tok.startswith("│") else sep)) + tok
        if display_width(cand) <= width or first:
            cur, first = cand, False
        else:
            lines.append(cur)
            cur = cont + tok
    lines.append(cur)
    return [fit(x, width) for x in lines]


def pad_right(s: str, col_w: int) -> str:
    """표시 폭(한글·이모지 2칸) 기준 왼쪽 정렬."""
    return s + " " * max(0, col_w - display_width(s))


def pad_left(s: str, col_w: int) -> str:
    """표시 폭 기준 오른쪽 정렬."""
    return " " * max(0, col_w - display_width(s)) + s


# ================================================================ 공통 해석
def _fmt(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def _results(doc) -> tuple[list[dict], str | None]:
    if isinstance(doc, dict):
        return [r for r in doc.get("results", []) if isinstance(r, dict)], doc.get("generated_at_kst")
    return [r for r in (doc or []) if isinstance(r, dict)], None


def _pending(r: dict) -> bool:
    return r.get("llm_note") == "정성 미적용(정량 기준)" or (r.get("llm_status") == "pending" and r.get("needs_llm"))


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


# ================================================================ 댓글 봇 리포트
R_NAMES = {
    "R1_dup_burst": "20초 내 연속 도배", "R1_fast_long": "비정상 속도의 긴 댓글", "R1_long_burst": "10분 내 긴 댓글 연속 작성",
    "R1_rhythm": "일정 간격 자동 작성", "R2_dup": "같은 댓글 반복", "R2_signature": "끝맺음 표현 반복", "R2_template": "비슷한 말투 반복",
    "R2_question": "매번 질문으로 끝맺음", "R3": "AI 생성 의심 말투", "R4_shop_link": "쇼핑 링크", "R4_own_link": "자사 링크",
    "R4_link": "외부 링크", "R4_promo": "홍보·DM 유도", "R4_pumasi": "맞팔·품앗이 댓글", "R5_kw": "프로필 자동화 문구",
    "R5_kw_exempted": "프로필 자동화 문구(감면)", "R5_bio_pumasi": "프로필 품앗이 문구", "R5_bio_shop": "프로필 쇼핑 링크",
    "R5_empty": "빈 프로필", "R5_low_follow": "팔로워 30명 미만", "R5_reply_only": "글 없이 댓글만 활동",
    "RH1": "오타·구어체", "RH2": "원글 내용 반영", "RH3_short": "짧은 즉흥 댓글", "RH3_colloquial": "자연스러운 구어체",
}
R_SHORT = {"R1_long_burst": "긴 댓글 연속 작성"}
VERDICT = {"🔴": "🔴 차단권장", "🟠": "🟠 검토필요", "🟡": "🟡 관찰"}
PUMASI_VERDICT = "🤝 뮤트권장"
STRONG_TAGS = ("링크", "동일 답글", "품앗이", "영업문구")
EVIDENCE_CAP = {"hot": 3, "watch": 2, "pumasi": 1}
NA_HINT = "↳ 답글 검증 권장: scripts/bsk_scan.py replies --handle <h> --max-items 15 → reply_score.py 재채점"
GUIDE = (f"→ 차단 요청: \"1번 차단해줘\" (명시 승인 시 1개씩 확인 후 진행 · 최대 {MAX_BLOCK_PER_TURN}개)",
         "→ 직접 차단: 위 프로필 링크 → ⋯ → 차단 (확인 없이 절대 차단하지 않습니다)")
NONE_FOUND = "감지된 댓글 봇·영업·품앗이 계정이 없습니다."


def _r_items(r: dict, positive_only: bool = False) -> list[tuple[str, float]]:
    it = [(k, v) for k, v in (r.get("items") or {}).items() if isinstance(v, (int, float)) and k != "R5_kw_exempted"]
    if positive_only:
        it = [(k, v) for k, v in it if v > 0]
    return sorted(it, key=lambda kv: -kv[1])


def _r_name(k: str, r: dict) -> str:
    """일반 사용자가 바로 이해하는 한국어 사유(기호·분수·내부 용어 없음)."""
    m = r.get("metrics") or {}
    n = m.get("n_replies")
    if k == "R2_dup":
        return f"{n}개 중 {m['dup_count']}개 같은 댓글" if n and m.get("dup_count") else R_NAMES[k]
    if k == "R2_signature":
        return f"{n}개 중 {m['signature_n']}개 끝에 '{m['signature']}' 반복" if n and m.get("signature_n") and m.get("signature") else R_NAMES[k]
    if k == "R4_pumasi":
        return f"{n}개 중 {m['pumasi_n']}개 품앗이" if n and m.get("pumasi_n") else R_NAMES[k]
    return R_NAMES.get(k, k)


def _r_short(k: str, r: dict) -> str:
    """요약표용 사유 이름(긴 이름만 줄임)."""
    return R_SHORT.get(k) or _r_name(k, r)


def _is_v4(r: dict) -> bool:
    return r.get("judgment_mode") == "llm_v4"


def _mode(r: dict) -> str:
    m = r.get("judgment_mode")
    if not m:
        m = "llm_v3" if r.get("llm_status") == "judged" else "quant_only"
    return f"{m}({r['category']})" if m == "llm_v4" and r.get("category") else m


def _reasons(r: dict) -> list[str]:
    """핵심 사유: v4.0 판정이면 LLM이 쓴 쉬운 사유(최대 3개). 아니면 +5 이상 강한 신호 최대 3개, 모자라면 약한 신호로 2개까지."""
    if _is_v4(r) and r.get("summary_reasons"):
        return [str(x) for x in r["summary_reasons"][:3]]
    pos = _r_items(r, True)
    pick = [k for k, v in pos if v >= 5][:3]
    for k, _ in pos:
        if len(pick) >= 2:
            break
        if k not in pick:
            pick.append(k)
    return [_r_short(k, r) for k in pick]


def classify_reply(results: list[dict]) -> dict:
    scored = [r for r in results if isinstance(r.get("total"), (int, float))]
    hot = sorted([r for r in scored if r["total"] >= 50], key=lambda r: -r["total"])
    pumasi = sorted([r for r in scored if r.get("pumasi_section") and r["total"] < 50], key=lambda r: -(r.get("metrics") or {}).get("pumasi_ratio", 0))
    watch = sorted([r for r in scored if 25 <= r["total"] < 50 and r not in pumasi], key=lambda r: -r["total"])
    green = [r for r in scored if r["total"] < 25 and r not in pumasi]
    na = [r for r in results if not isinstance(r.get("total"), (int, float))]
    numbered = [("hot", r) for r in hot] + [("watch", r) for r in watch] + [("pumasi", r) for r in pumasi]
    return {"hot": hot, "watch": watch, "pumasi": pumasi, "green": green, "na": na, "numbered": numbered,
            "num": {r["handle"]: i + 1 for i, (_, r) in enumerate(numbered)}}


def _verdict(kind: str, r: dict) -> str:
    if kind == "pumasi":
        return PUMASI_VERDICT
    return VERDICT.get(r.get("tier"), VERDICT["🔴"] if r["total"] >= 75 else VERDICT["🟠"] if r["total"] >= 50 else VERDICT["🟡"])


def _strong(s: dict) -> bool:
    return any(k in t for t in (s.get("tags") or []) for k in STRONG_TAGS)


def _evidence(kind: str, r: dict) -> list[dict]:
    """증거 댓글 선택: 링크·복붙·품앗이 태그 우선(최대 cap), 모자라면 정보량 많은(긴) 답글로 2건까지."""
    ss = [s for s in (r.get("samples") or []) if (s.get("text") or "").strip()]
    cap = EVIDENCE_CAP[kind]
    if kind == "pumasi":
        return ([s for s in ss if "품앗이" in (s.get("tags") or [])] or ss)[:1]
    strong = [s for s in ss if _strong(s)][:cap]
    rest = sorted([s for s in ss if s not in strong], key=lambda s: -display_width(s.get("text") or ""))
    while len(strong) < min(2, cap) and rest:
        strong.append(rest.pop(0))
    return strong


SOURCE_LABEL = {"root_post": "프로필 게시글", "bio": "프로필 소개"}
LINK_LABEL = {"reply": "댓글 보기", "root_post": "게시글 보기", "bio": "프로필 보기"}


def _v4_items(kind: str, r: dict) -> list[dict]:
    """v4.0 증거: 원문 대조를 통과한 verified_evidence 최대 cap건. 클릭할 댓글 링크가 하나도 없으면
    정량 표본(samples)의 강한 댓글로 최대 2건(🤝 1건) 보충한다 → 언제나 의심 댓글 직접 링크가 있다."""
    cap = EVIDENCE_CAP[kind]
    items = [dict(v, _v4=True) for v in r.get("verified_evidence") or [] if isinstance(v, dict) and (v.get("quote") or v.get("text"))][:cap]
    if not any(i.get("source_type") == "reply" and "/post/" in str(i.get("url") or "") for i in items):
        have = {i.get("url") for i in items}
        supp = [s for s in _evidence(kind, r) if s.get("url") and "/post/" in s["url"] and s["url"] not in have]
        n = min(1 if kind == "pumasi" else 2, len(supp))
        if n:
            items = items[:max(1, cap - n)] + supp[:n]
    return items


def _v4_text(item: dict, room: int) -> str:
    """증거 1줄: 답글은 원문(길면 인용 부분이 보이도록), 프로필 게시글·소개는 인용."""
    text, quote = " ".join(str(item.get("text") or "").split()), " ".join(str(item.get("quote") or "").split())
    if item.get("source_type") != "reply" or not text:
        return truncate(quote or text, room)
    shown = truncate(text, room)
    if not quote or quote.rstrip("…") in shown.rstrip("…") or display_width(text) <= room:
        return shown
    return truncate(quote if text.startswith(quote) else "…" + quote, room)


def _v4_explain(r: dict, item: dict) -> str:
    """증거 2줄: '출처 · 이유' (대괄호 없음)."""
    st = item.get("source_type")
    why = str(item.get("why") or "").strip()
    if st == "reply":
        own = item.get("own_thread") or (item.get("reply_to") and item.get("reply_to") == r.get("handle"))
        src = "본인 글 답글" if own else (f"@{item['reply_to']} 글 댓글" if item.get("reply_to") else "")
    else:
        src = SOURCE_LABEL.get(st, "")
    return " · ".join(x for x in (src, why) if x)


def _v4_url(r: dict, item: dict) -> str:
    return item.get("url") or r.get("profile_url") or f"https://www.threads.com/@{r['handle']}"


def _batches_text(r: dict) -> str:
    bs = (r.get("metrics") or {}).get("burst_batches") or []
    return " · ".join(f"{b.get('count')}건({b.get('gap_min')}~{b.get('gap_max')}초, 평균{_fmt(round(b.get('mean_len') or 0))}자)" for b in bs)


def _span_text(sec: int) -> str:
    if sec < 60:
        return f"{sec}초"
    return f"{sec // 60}분" + (f" {sec % 60}초" if sec % 60 else "")


def _tag_desc(kind: str, r: dict, tag: str) -> str:
    m = r.get("metrics") or {}
    n = m.get("n_replies")
    mt = re.fullmatch(r"동일 답글 (\d+)회/(\d+)초", tag)
    if mt:
        return f"{_span_text(int(mt.group(2)))} 만에 같은 댓글 {mt.group(1)}번 반복"
    mt = re.fullmatch(r"동일 답글 (\d+)회", tag)
    if mt:
        return f"같은 내용의 댓글 {mt.group(1)}번 반복"
    if tag.startswith("시그니처 "):
        sig = tag[len("시그니처 "):]
        if n and m.get("signature_n") and m.get("signature") == sig:
            return f"{n}개 중 {m['signature_n']}개 댓글 끝에 '{sig}' 표현 반복"
        return f"여러 댓글 끝에 '{sig}' 표현 반복"
    if tag == "품앗이":
        return "맞팔·품앗이 유도 댓글"
    return TAG_DESC.get(tag, tag)


TAG_DESC = {"≤20초 복붙": "20초 안에 같은 댓글 연속 작성", "복붙": "다른 글에 같은 댓글 반복", "쇼핑링크": "남의 글에 쇼핑몰 링크 홍보",
            "자사링크": "남의 글에 본인 사이트 링크 홍보", "링크": "남의 글에 외부 링크 삽입", "영업문구": "댓글에서 프로필·DM·구매 유도",
            "초고속 장문": "사람이 치기 어려운 속도로 긴 댓글 작성", "내 글 답글": "본인 글에 단 답글"}


def _ev_explain(kind: str, r: dict, s: dict) -> str:
    """증거 댓글 아래 줄: '출처 · 쉬운 설명' (대괄호·내부 태그 없음)."""
    tags = list(s.get("tags") or [])
    own = "내 글 답글" in tags or (s.get("reply_to") and s.get("reply_to") == r.get("handle"))
    src = "본인 글 답글" if own else (f"@{s['reply_to']} 글 댓글" if s.get("reply_to") else "")
    core = [t for t in tags if t != "내 글 답글"]
    m = r.get("metrics") or {}
    if kind == "pumasi":
        n, pn = m.get("n_replies"), m.get("pumasi_n")
        desc = (f"{n}개 중 {pn}개가 맞팔·품앗이 댓글" if n and pn else "맞팔·품앗이 댓글 위주") + " (차단보다 뮤트 권장)"
    elif core:
        desc = _tag_desc(kind, r, core[0])
    elif tags:                                                    # '내 글 답글' 단독
        desc, src = TAG_DESC["내 글 답글"], ""
    else:
        desc = "AI가 쓴 듯한 정형화된 댓글 의심" if (r.get("items") or {}).get("R3") else ""
    return " · ".join(x for x in (src, desc) if x)


def _note(r: dict) -> str:
    """참고 1줄: 실질적 반증·판단 보류 맥락이 있을 때만('반증 없음…' 류 단순 부정은 생략)."""
    hce = " ".join((r.get("human_counter_evidence") or "").split())
    if not hce or hce.lstrip("※ ").startswith(("반증 없음", "뚜렷한 반증 없음", "없음")):
        return ""
    return hce


def wrap_text(prefix: str, text: str, width: int, cont: str | None = None) -> list[str]:
    return wrap_tokens(prefix, (text or "").split(), width, sep=" ", cont=cont)


def _url_line(url: str, width: int, indent: int = 7) -> str:
    """직접 링크 줄 — URL은 절대 자르지 않고, 폭이 모자라면 들여쓰기만 줄인다."""
    return " " * max(0, min(indent, width - len(url))) + url


def _reply_verbose(r: dict, width: int, ind: str = "     ") -> list[str]:
    m = r.get("metrics") or {}
    head = [_mode(r)]
    if _is_v4(r):
        head += [f"LLM {_fmt(r.get('llm_score'))}점 → {_fmt(r.get('total'))}점", f"정량 참고 {_fmt(r.get('quant_total'))}점",
                 f"검증 인용: {len(r.get('verified_evidence') or [])}건"]
    L = wrap_tokens(f"{ind}판정방식: ", head, width)
    if _batches_text(r):
        L += wrap_tokens(f"{ind}배치: ", _batches_text(r).split(" · "), width)
    for sig in (m.get("daily_schedule_hint") or {}).get("signals") or []:
        L.append(fit(f"{ind}일정 힌트: {sig}", width))
    L += wrap_tokens(f"{ind}{'정량 세부' if _is_v4(r) else '세부'}: ", [f"{k}{v:+g}" for k, v in (r.get("items") or {}).items() if isinstance(v, (int, float))], width, sep=" ")
    cats = r.get("categories") or {}
    L += wrap_tokens(f"{ind}합계: ", [" ".join(f"{c}={_fmt(v)}" for c, v in cats.items()), f"raw {_fmt(r.get('raw'))} → {_fmt(r.get('total'))}"]
                     + ([f"게이트 {','.join(r['gates'])}"] if r.get("gates") else [])
                     + [f"신뢰도 {CONF_ICON.get(r.get('confidence'), '-')} · 답글 {r.get('n_replies') or 0}개"]
                     + (["경계(±3점)"] if r.get("boundary") else []) + (["정성 미적용"] if _pending(r) else []), width, sep=" │ ")
    L += wrap_tokens(f"{ind}지표: ", [f"답글 {m.get('n_replies')}", f"대상 {m.get('n_targets')}명", f"평균 {m.get('len_mean')}자",
                                     f"장문 {m.get('n_long')}", f"10분 최대 {m.get('b10')}", f"복붙연사 {m.get('dup_burst')}",
                                     f"중복 {m.get('dup_share')}", f"품앗이 {m.get('pumasi_ratio')}", f"단문 {m.get('short_ratio')}",
                                     f"구어 {m.get('colloquial_ratio')}"], width)
    for e in r.get("evidence") or []:
        if isinstance(e, dict):
            L.append(fit(f"{ind}  {e.get('pattern')}: “{e.get('quote')}”", width))
    for w in r.get("warnings") or []:
        L.append(fit(f"{ind}  ⚠️ {w}", width))
    return L


def _green_tokens(green: list[dict]) -> list[str]:
    toks = []
    for r in green:
        m = r.get("metrics") or {}
        note = (["고속 단문"] if (m.get("b10") or 0) >= 8 and (m.get("short_ratio") or 0) >= 0.4 else []) + (["정성 미적용"] if _pending(r) else [])
        toks.append(f"@{r['handle']}" + (f"({', '.join(note)})" if note else ""))
    return toks


FAST_SHORT_NOTE = "※ 고속 단문 = 10분 안 짧은 댓글 8개 이상이지만 즉흥·구어 반응이라 봇 점수에 넣지 않음"


def _summary_table(c: dict, width: int) -> list[str]:
    """상단 요약표: 순수 공백 칼정렬(표시 폭 기준, 파이프·HTML 없음)."""
    rows = []
    for kind, r in c["numbered"]:
        rows.append((str(c["num"][r["handle"]]), f"@{r['handle']}", _fmt(r["total"]), _verdict(kind, r), _reasons(r)))
    gap = "  "
    nw = max(display_width("#"), *(display_width(x[0]) for x in rows))
    aw = min(24 if width >= 100 else 18, max(display_width("계정"), *(display_width(x[1]) for x in rows)))
    sv = max(display_width(x[2]) for x in rows)
    sw = max(display_width("점수"), sv)
    vw = max(display_width("판정"), *(display_width(x[3]) for x in rows))

    def left(n, a, s, v):
        return "  " + pad_left(n, nw) + gap + pad_right(truncate(a, aw), aw) + gap + pad_right(s, sw) + gap + pad_right(v, vw) + gap

    out = [left("#", "계정", "점수", "판정") + "핵심 사유"]
    for n, a, s, v, toks in rows:
        head = left(n, a, pad_left(s, sv), v)
        room = width - display_width(head)
        why = ""
        for t in toks:
            cand = why + (" · " if why else "") + t
            if display_width(cand) <= room:
                why = cand
        if not why and toks:
            why = truncate(toks[0], max(4, room))
        out.append(fit((head + why).rstrip(), width))
    return out


def _evidence_block(kind: str, r: dict, n: int, width: int, verbose: bool) -> list[str]:
    url = r.get("profile_url") or f"https://www.threads.com/@{r['handle']}"
    head = f"  {n}. @{r['handle']}  ({url})"
    L = [head] if display_width(head) <= width else [fit(f"  {n}. @{r['handle']}", width), _url_line(url, width, 5)]
    pre, ind = "     • ", " " * 7
    room = max(12, width - display_width(pre) - 2)
    if _is_v4(r) and r.get("verified_evidence"):
        for it in _v4_items(kind, r):
            if it.get("_v4"):
                L.append(fit(f"{pre}\"{_v4_text(it, room)}\"", width))             # 1줄: 댓글 원문/인용
                why = _v4_explain(r, it)
                url = _v4_url(r, it)
            else:                                                                   # 링크 보충(정량 표본)
                L.append(fit(f"{pre}\"{truncate(it.get('text') or '', room)}\"", width))
                why, url = _ev_explain(kind, r, it), it.get("url")
            if why:
                L += wrap_text(ind, why, width, cont=ind)                           # 2줄: 출처 · 쉬운 설명
            if url:
                L.append(_url_line(url, width))                                     # 3줄: 직접 링크(자르지 않음)
    for s in ([] if _is_v4(r) and r.get("verified_evidence") else _evidence(kind, r)):
        L.append(fit(f"{pre}\"{truncate(s.get('text') or '', room)}\"", width))       # 1줄: 댓글 원문
        why = _ev_explain(kind, r, s)
        if why:
            L += wrap_text(ind, why, width, cont=ind)                               # 2줄: 출처 · 쉬운 설명
        if s.get("url"):
            L.append(_url_line(s["url"], width))                                    # 3줄: 직접 링크(자르지 않음)
    note = _note(r) if kind != "pumasi" else ""
    if note:
        L += wrap_text("     • 참고: ", note, width, cont=" " * 7)
    if verbose:
        L += _reply_verbose(r, width)
    return L


def render_reply_cli(doc, title: str | None = None, width: int = DEFAULT_WIDTH, verbose: bool = False) -> str:
    width = max(60, int(width))
    results, ts = _results(doc)
    c = classify_reply(results)
    stamp = (ts or now_kst().isoformat())[:16].replace("T", " ")
    head, summ = f"{title or 'Threads 댓글 봇 스캔'} ({stamp} KST)", f"스캔 {len(results)}명 · 감지 {len(c['numbered'])}명"
    L = [head + "  │  " + summ] if display_width(head + "  │  " + summ) <= width else [fit(head, width), fit(summ, width)]
    meta = (doc.get("meta") or {}) if isinstance(doc, dict) else {}
    if verbose and meta.get("scope"):
        L.append(fit(f"표본: {meta['scope']}", width))
    pend = [r for r in results if _pending(r)]
    if pend:
        L.append(fit(f"※ 정성 미적용(정량 기준) {len(pend)}명", width))
    L.append("")
    if c["numbered"]:
        L += _summary_table(c, width) + ["", "[증거 댓글]"]
        for kind, r in c["numbered"]:
            L += [""] + _evidence_block(kind, r, c["num"][r["handle"]], width, verbose)
    else:
        L.append(f"  {NONE_FOUND}")
    if c["na"]:
        L += [""] + wrap_tokens(f"  ⚪ 답글 미수집 {len(c['na'])}명: ", [f"@{r.get('handle')}" for r in c["na"]], width, sep=", ", cont="    ")
        L.append(fit(f"    {NA_HINT}", width))
    if verbose and c["green"]:
        toks = _green_tokens(c["green"])
        L += ["", f"[🟢 정상 {len(c['green'])}명]"] + wrap_tokens("  ", toks, width, sep=", ", cont="  ")
        if any("고속 단문" in t for t in toks):
            L.append(fit(f"  {FAST_SHORT_NOTE}", width))
    if c["numbered"]:
        L += [""] + [fit(g, width) for g in GUIDE]
    return "\n".join(L)


def render_reply_markdown(doc, title: str | None = None, verbose: bool = False) -> str:
    results, ts = _results(doc)
    c = classify_reply(results)
    num = c["num"]
    stamp = (ts or now_kst().isoformat())[:16].replace("T", " ")
    L = [f"## {title or 'Threads 댓글 봇 스캔'} — {stamp} KST", "", f"스캔 {len(results)}명 · 감지 {len(c['numbered'])}명"]
    meta = (doc.get("meta") or {}) if isinstance(doc, dict) else {}
    if verbose and meta.get("scope"):
        L.append(f"표본: {meta['scope']}")
    pend = [r for r in results if _pending(r)]
    if pend:
        L.append(f"※ 정성 미적용(정량 기준) {len(pend)}명")
    L.append("")
    if not c["numbered"]:
        L.append(NONE_FOUND)
    else:
        L += ["| # | 계정 | 점수 | 판정 | 핵심 사유 |", "|---|---|---|---|---|"]
        for kind, r in c["numbered"]:
            L.append(f"| {num[r['handle']]} | [@{r['handle']}]({r.get('profile_url', '')}) | {_fmt(r['total'])} | {_verdict(kind, r)} | "
                     f"{_cell(' · '.join(_reasons(r)) or '–')} |")
        L += ["", "### [증거 댓글]"]
        for kind, r in c["numbered"]:
            L += ["", f"**{num[r['handle']]}. [@{r['handle']}]({r.get('profile_url', '')})**", ""]
            v4 = _is_v4(r) and r.get("verified_evidence")
            for s in (_v4_items(kind, r) if v4 else _evidence(kind, r)):
                if s.get("_v4"):
                    text, why = _v4_text(s, 120), _v4_explain(r, s)
                    link = f" — [{LINK_LABEL.get(s.get('source_type'), '댓글 보기')}]({_v4_url(r, s)})"
                else:
                    text, why = truncate(s.get("text") or "", 120), _ev_explain(kind, r, s)
                    link = f" — [댓글 보기]({s['url']})" if s.get("url") else ""
                L.append(f"- \"{_cell(text)}\"{link}" + ("  " if why else ""))
                if why:
                    L.append(f"  {_cell(why)}")
            note = _note(r) if kind != "pumasi" else ""
            if note:
                L.append(f"- 참고: {_cell(note)}")
            if verbose:
                L += [f"- `{x.strip()}`" for x in _reply_verbose(r, 10_000, "")]
    if c["na"]:
        L += ["", f"⚪ 답글 미수집 {len(c['na'])}명: " + ", ".join(f"@{r.get('handle')}" for r in c["na"]), f"{NA_HINT}"]
    if verbose and c["green"]:
        L += ["", f"🟢 정상 {len(c['green'])}명: " + ", ".join(_green_tokens(c["green"]))]
    if c["numbered"]:
        L += ["", GUIDE[0] + "  ", GUIDE[1]]                      # 마크다운 강제 줄바꿈(공백 2칸)
    L.append("")
    return "\n".join(L)


def render(doc, title: str | None = None, fmt: str = "cli", width: int = DEFAULT_WIDTH, verbose: bool = False) -> str:
    return render_reply_markdown(doc, title, verbose) if fmt == "markdown" else render_reply_cli(doc, title, width, verbose)


def main(argv=None) -> int:
    setup_utf8()
    ap = argparse.ArgumentParser(description="threads-bot-blocker 댓글 봇 리포트 렌더러 (기본: CLI 텍스트)")
    ap.add_argument("--input", required=True, help="reply_score.py 결과 JSON")
    ap.add_argument("--format", choices=("cli", "markdown"), default="cli")
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="CLI 표시 폭(컬럼, 기본 100, 최소 60)")
    ap.add_argument("--verbose", action="store_true", help="세부 점수(세부·합계·지표)·🟢 정상 목록 표시")
    ap.add_argument("--out", help="파일로도 저장 (생략 시 stdout만)")
    ap.add_argument("--title")
    a = ap.parse_args(argv)
    text = render(load_json(a.input), a.title, a.format, a.width, a.verbose)
    print(text)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
