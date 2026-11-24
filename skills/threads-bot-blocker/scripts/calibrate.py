#!/usr/bin/env python3
"""댓글 봇 라벨셋 기준 재채점 + 오탐/미탐/🤝 누락 리포트 (모드 4 차단 목록 학습/보정).

가중치를 자동으로 바꾸지 않는다. 위반 항목과 '점수 차이'만 보고하고, 변경은 사용자가 승인한 뒤
references/scoring.md 와 reply_score.py 상수에 함께 반영한다.

라벨: reply_bot(≥75) / reply_sales(50~74) / reply_template(<50) / pumasi(<50 + 🤝) / human(<25) / human_fast(<25)
배포 스킬에는 개인 라벨·데이터가 없다(data/labels.json 은 빈 스키마). 라벨은 모드 4에서 사용자가 쌓는다.
저장소 회귀 기준셋(실측 댓글러 23명)은 저장소 tests/fixtures/ 에 있다.

사용:
  python scripts/calibrate.py --dataset reply_dataset.json --judgments reply_judgments.json --labels data/labels.json --strict
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reply_score import index_by_handle, load_reply_accounts, score_reply_dataset  # noqa: E402
from tbb_common import DATA_DIR, dump_json, load_json, normalize_handle, setup_utf8  # noqa: E402

DEFAULT_LABELS = DATA_DIR / "labels.json"
EXPECT = {  # label → (최소, 최대, 🤝 품앗이 라벨 기대)
    "reply_bot": (75, 100, None),
    "reply_sales": (50, 74, None),
    "reply_template": (0, 49, None),
    "pumasi": (0, 49, True),
    "human": (0, 24, False),
    "human_fast": (0, 24, False),
}
PUMASI_LABEL = "🤝 품앗이 답글러"


def evaluate(results: list[dict], labels: list[dict]) -> dict:
    by = {r["handle"]: r for r in results}
    rows, violations = [], []
    for lab in labels:
        h = normalize_handle(lab["handle"])
        r = by.get(h)
        if lab.get("label") not in EXPECT:
            violations.append({"handle": h, "type": "unknown_label", "detail": f"알 수 없는 라벨 {lab.get('label')!r}"})
            continue
        if not r or r.get("total") is None:
            violations.append({"handle": h, "type": "missing", "detail": "결과 없음/답글 미수집"})
            continue
        lo, hi, pum = EXPECT[lab["label"]]
        t = r["total"]
        flag = PUMASI_LABEL in (r.get("labels") or [])
        kind = None
        if not lo <= t <= hi:
            if lab["label"] in ("human", "human_fast", "pumasi", "reply_template") and t > hi:
                kind = "false_positive"
            elif lab["label"] in ("reply_bot", "reply_sales") and t < lo:
                kind = "false_negative"
            else:
                kind = "tier_drift"
        if pum is not None and flag != pum:
            kind = (kind + "+" if kind else "") + ("pumasi_miss" if pum else "pumasi_false_flag")
        rows.append({"handle": h, "label": lab["label"], "total": t, "tier": r["tier"], "expected": f"{lo}~{hi}", "pumasi": flag,
                     "boundary": r.get("boundary"), "gates": r.get("gates"), "status": kind or "OK",
                     "b10": (r.get("metrics") or {}).get("b10"), "categories": r.get("categories")})
        if kind:
            violations.append({"handle": h, "type": kind, "detail": f"{t}점 (기대 {lo}~{hi}), 🤝={flag}",
                               "top_items": sorted((r.get("items") or {}).items(), key=lambda kv: -abs(kv[1]))[:5]})
    humans = [x for x in rows if x["label"] in ("human", "human_fast")]
    bots = [x for x in rows if x["label"] in ("reply_bot", "reply_sales")]
    summary = {"n": len(rows), "n_human": len(humans), "human_ge25": sum(1 for x in humans if x["total"] >= 25),
               "human_ge50": sum(1 for x in humans if x["total"] >= 50),
               "n_human_fast": sum(1 for x in humans if x["label"] == "human_fast"),
               "human_fast_ge25": sum(1 for x in humans if x["label"] == "human_fast" and x["total"] >= 25),
               "n_bot": len(bots), "bot_detected_ge50": sum(1 for x in bots if x["total"] >= 50),
               "boundary": [x["handle"] for x in rows if x["boundary"]], "violations": len(violations)}
    return {"summary": summary, "rows": rows, "violations": violations}


def to_markdown(ev: dict) -> str:
    s = ev["summary"]
    L = ["## 댓글 봇 캘리브레이션 리포트", "",
         f"- 사람 오탐(≥25): **{s['human_ge25']}/{s['n_human']}** (≥50: {s['human_ge50']}) · 고속 단문 인간 오탐: **{s['human_fast_ge25']}/{s['n_human_fast']}**",
         f"- 댓글 봇·영업 탐지(≥50): **{s['bot_detected_ge50']}/{s['n_bot']}**",
         f"- 경계(±3) 계정: {', '.join('@' + h for h in s['boundary']) or '없음'}", f"- 위반: **{s['violations']}건**", "",
         "| 계정 | 라벨 | 총점 | 등급 | 기대 | 🤝 | 10분 최대 | 게이트 | 판정 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(ev["rows"], key=lambda x: -x["total"]):
        L.append(f"| @{r['handle']} | {r['label']} | {r['total']:g}{' ⚠️' if r['boundary'] else ''} | {r['tier']} | {r['expected']} | "
                 f"{'✅' if r['pumasi'] else '–'} | {r['b10']} | {','.join(r['gates'] or []) or '–'} | {r['status']} |")
    if ev["violations"]:
        L += ["", "### 위반 상세 (가중치 변경은 사용자 승인 후 scoring.md·reply_score.py에 함께 반영)"]
        L += [f"- @{v['handle']} `{v['type']}`: {v['detail']} · 주요 항목 {v.get('top_items')}" for v in ev["violations"]]
    return "\n".join(L)


def main(argv=None) -> int:
    setup_utf8()
    ap = argparse.ArgumentParser(description="댓글 봇 라벨셋 기준 재채점·오탐/미탐 리포트")
    ap.add_argument("--dataset", nargs="+", required=True, help="댓글러 데이터셋(실측 데이터셋·계정 리스트·replies 문서)")
    ap.add_argument("--judgments", help="댓글 봇 정성 판정 JSON (생략 시 정량 기준)")
    ap.add_argument("--labels", default=str(DEFAULT_LABELS))
    ap.add_argument("--out", help=".md 또는 .json")
    ap.add_argument("--strict", action="store_true", help="위반이 있으면 종료 코드 1")
    a = ap.parse_args(argv)
    labels = load_json(a.labels)
    labels = labels.get("labels", []) if isinstance(labels, dict) else labels
    if not labels:
        print(f"라벨이 비어 있습니다({a.labels}). 모드 4에서 차단 사유 라벨을 추가한 뒤 다시 실행하세요.")
        return 0
    accs, _ = load_reply_accounts([load_json(p) for p in a.dataset])
    judg = index_by_handle(load_json(a.judgments), "judgments") if a.judgments else {}
    ev = evaluate(score_reply_dataset(accs, judg), labels)
    md = to_markdown(ev)
    if a.out and a.out.endswith(".json"):
        dump_json(ev, a.out)
    elif a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    print(md)
    return 1 if (a.strict and ev["violations"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
