"""threads-bot-blocker 공개 배포본 스모크 테스트 (100% 합성 데이터 · 네트워크·브라우저 불필요).

실행: python -m unittest discover -s tests -v
모든 핸들·글·링크는 테스트용으로 지어낸 값(example_*, writer_*, *.example)이며 실존 계정이 아니다.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True                    # 배포 스킬 폴더에 __pycache__ 를 만들지 않는다

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "threads-bot-blocker"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import adapter  # noqa: E402
import feed_candidates  # noqa: E402
import render_report  # noqa: E402
import reply_score  # noqa: E402
import tbb_common  # noqa: E402

BASE_TS = 1795430000                              # 2026-11 무렵 고정 epoch (합성)
EXPECTED_FILES = sorted([
    "SKILL.md",
    "data/blocked_accounts.json", "data/labels.json",
    "references/blocking_safety.md", "references/browser_adapter.md", "references/bsk_notes.md", "references/examples.md",
    "references/llm_rubric.md", "references/patterns.md", "references/report_template.md", "references/scoring.md",
    "scripts/adapter.py", "scripts/bsk_scan.py", "scripts/calibrate.py", "scripts/feed_candidates.py",
    "scripts/fetch_profile.py", "scripts/render_report.py", "scripts/reply_score.py", "scripts/tbb_common.py",
])


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], capture_output=True, timeout=120, env=env, cwd=str(ROOT))


def _acc(handle, replies=(), comments=(), profile=None, n_roots=None):
    a = reply_score.new_account(handle)
    a["replies"] = [reply_score._reply_rec(r, "reply") for r in replies]
    a["comments"] = [reply_score._reply_rec(c, "comment") for c in comments]
    if profile is not None:
        a["profile"], a["profile_status"], a["n_roots"] = profile, "ok", n_roots
    return a


def copy_paste_bot():
    """20초 안에 같은 홍보 문구를 여러 글에 연속으로 붙이는 합성 복붙 봇."""
    reps = [{"code": f"CP{i}", "taken_at": BASE_TS + i * 6, "reply_to_author": f"writer_{i}", "parent_excerpt": f"오늘의 고민 {i}",
             "text": "스친님 화이팅! 우리 앱 무료 체험 중이에요 프로필 고정글 확인해 주세요 ^^"} for i in range(12)]
    prof = {"username": "example_copybot", "full_name": "Example", "biography": "", "follower_count": 12, "bio_links": []}
    return _acc("example_copybot", reps, profile=prof, n_roots=0)


def ai_parrot_bot():
    """공감 + 질문 꼬리를 정확히 62초 간격으로 다는 합성 AI 앵무새 답글러."""
    topics = ["아침 루틴", "퇴사 고민", "첫 창업", "식단 기록", "독서 습관", "주식 공부", "육아 번아웃", "부업 시작",
              "블로그 운영", "러닝 기록", "이직 준비", "새벽 기상"]
    reps = [{"code": f"AP{i}", "taken_at": BASE_TS + i * 62 + (i % 3), "reply_to_author": f"writer_{i}",
             "parent_excerpt": f"{t}에 대한 제 이야기를 써봤어요",
             "text": f"{t}에 대한 이야기 정말 공감돼요! 꾸준함이 결국 답인 것 같아요. 혹시 {t}할 때 가장 힘들었던 순간은 언제였나요?"}
            for i, t in enumerate(topics)]
    prof = {"username": "example_parrot", "full_name": "Example Parrot", "biography": "매일 소통합니다", "follower_count": 40, "bio_links": []}
    return _acc("example_parrot", reps, profile=prof, n_roots=0)


def chatty_human():
    """불규칙한 간격의 맥락 있는 대화 — 사람(🟢)이어야 한다."""
    offs = [0, 95, 260, 330, 610, 1400]
    texts = ["어제 저도 거기 갔었는데 줄이 너무 길어서 그냥 돌아왔어요 ㅠㅠ 다음엔 평일에 가봐야겠다",
             "ㅋㅋㅋ 그거 완전 우리 집 얘기네요. 강아지가 맨날 소파 차지함", "헐 대박",
             "저는 반대로 생각해요! 회사 근처라 오히려 편하던데요 ㅎㅎ 출근길에 들르기 좋음",
             "오 그 책 저도 읽다 말았는데 다시 펴봐야겠네요", "ㅇㅇ 인정"]
    reps = [{"code": f"HU{i}", "taken_at": BASE_TS + offs[i], "reply_to_author": f"friend_{i % 3}", "parent_excerpt": f"이야기 {i}",
             "text": t} for i, t in enumerate(texts)]
    return _acc("example_human", reps)


def fast_short_human():
    """10분 안에 짧은 댓글을 많이 다는 고속 단문 인간 — 오탐 0%(🟢)여야 한다."""
    words = ["ㅋㅋㅋ", "오 좋다", "헐", "맞아요", "대박", "ㅠㅠ", "인정", "와", "좋네요", "굿", "ㅎㅎ", "오호"]
    reps = [{"code": f"FS{i}", "taken_at": BASE_TS + i * 40 + (i * 7) % 13, "reply_to_author": f"friend_{i % 4}",
             "parent_excerpt": f"짧은 글 {i}", "text": w} for i, w in enumerate(words)]
    return _acc("example_fasthuman", reps)


def shop_link_drop():
    """남의 글 댓글창에 쇼핑 링크 1개 — RG5 하한 50(🟠)."""
    return _acc("example_shop", comments=[{"code": "SH1", "taken_at": BASE_TS, "source_author": "writer_pop",
                                           "source_excerpt": "요즘 너무 피곤해요",
                                           "text": "피곤할 땐 이거 추천합니다 smartstore.naver.com/example"}])


def pumasi_replier():
    reps = [{"code": f"PM{i}", "taken_at": BASE_TS + i * 300 + (i * 37) % 90, "reply_to_author": f"writer_{i}",
             "parent_excerpt": f"새 글 {i}",
             "text": ("스하리 완료~ 맞팔해요 저도 놀러와 주세요🤍" if i % 2 == 0 else f"오 {i}번째 글 재밌네요 ㅋㅋ 저도 비슷한 경험 있어요")}
            for i in range(10)]
    return _acc("example_pumasi", reps)


def scored(accounts, judgments=None):
    return reply_score.score_reply_dataset(accounts, judgments or {})


class TestScoreEngine(unittest.TestCase):
    def test_copy_paste_bot_detected(self):
        r = scored([copy_paste_bot()])[0]
        self.assertGreaterEqual(r["total"], 50, r)
        self.assertIn(r["tier"], ("🟠", "🔴"))
        self.assertGreater(r["categories"]["R1"], 0)

    def test_ai_parrot_quant_and_blind_judgment(self):
        q = scored([ai_parrot_bot()])[0]
        self.assertGreaterEqual(q["total"], 50)
        j = {"judgments": [{"handle": "example_parrot", "R3": 2,
                            "evidence": [{"pattern": "R3", "quote": "꾸준함이 결국 답인 것 같아요"}]}]}
        r = scored([ai_parrot_bot()], reply_score.index_by_handle(j))[0]
        self.assertEqual(r["tier"], "🔴")
        self.assertEqual(r["items"]["R3"], 25)

    def test_fabricated_quote_is_rejected(self):
        j = {"handle": "example_parrot", "R3": 2, "evidence": [{"pattern": "R3", "quote": "원문에 없는 지어낸 인용"}]}
        r = reply_score.score_reply_account(reply_score.compute_reply_bot_features(ai_parrot_bot()), j)
        self.assertNotIn("R3", r["items"])
        self.assertTrue(any("원문에 없음" in w for w in r["warnings"]))

    def test_humans_are_green(self):
        for acc in (chatty_human(), fast_short_human()):
            r = scored([acc])[0]
            self.assertEqual(r["tier"], "🟢", (acc["handle"], r["total"]))
            self.assertEqual(r["categories"]["R1"], 0, acc["handle"])

    def test_shop_link_floor_rg5(self):
        r = scored([shop_link_drop()])[0]
        self.assertEqual(r["total"], 50)
        self.assertIn("RG5", r["gates"])

    def test_pumasi_badge_not_bot(self):
        r = scored([pumasi_replier()])[0]
        self.assertLess(r["total"], 50)
        self.assertIn("🤝 품앗이 답글러", r["labels"])

    def test_no_replies_graceful(self):
        r = scored([reply_score.new_account("example_empty")])[0]
        self.assertIsNone(r["total"])
        self.assertEqual(r["tier"], "⚪")


class TestRenderReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        accs = [copy_paste_bot(), ai_parrot_bot(), shop_link_drop(), pumasi_replier(), chatty_human(), fast_short_human()]
        cls.doc = reply_score.build_doc(scored(accs), [], {"n_accounts": len(accs)})
        cls.cli = render_report.render(cls.doc)

    def test_header_table_evidence_guide(self):
        first = self.cli.splitlines()[0]
        self.assertRegex(first, r"^Threads 댓글 봇 스캔 \(\d{4}-\d\d-\d\d \d\d:\d\d KST\)  │  스캔 6명 · 감지 \d명$")
        self.assertIn("#  계정", self.cli)
        self.assertIn("\n[증거 댓글]\n", self.cli)
        self.assertIn("https://www.threads.com/@example_copybot", self.cli)
        self.assertIn('"1번 차단해줘"', self.cli)
        self.assertIn("확인 없이 절대 차단하지 않습니다", self.cli)

    def test_humans_hidden_and_width(self):
        for h in ("example_human", "example_fasthuman"):
            self.assertNotIn(f"@{h}", self.cli)
        self.assertNotIn("🟢", self.cli)
        for line in render_report.render(self.doc, width=80).splitlines():
            if "https://" not in line:                     # URL은 자르지 않는다
                self.assertLessEqual(render_report.display_width(line), 80, line)

    def test_markdown_and_cli_entrypoint(self):
        md = render_report.render(self.doc, fmt="markdown")
        self.assertIn("[댓글 보기](https://www.threads.com/@", md)
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "results.json"
            fp.write_text(json.dumps(self.doc, ensure_ascii=False), encoding="utf-8")
            cp = run_script("render_report.py", "--input", str(fp))
            self.assertEqual(cp.returncode, 0, cp.stderr.decode("utf-8", "replace"))
            self.assertIn("[증거 댓글]", cp.stdout.decode("utf-8"))


class TestAdapter(unittest.TestCase):
    DOC = {"source": "feed-comments",
           "posts": [{"url": "https://www.threads.com/@writer_a/post/P1", "reply_count": 12}],
           "items": [{"handle": "writer_a", "text": "감사해요", "source_post": "https://www.threads.com/@writer_a/post/P1", "code": "c0"},
                     {"url": "https://www.threads.com/@spam_x/post/c1", "source_post": "https://www.threads.com/@writer_a/post/P1",
                      "text": "이거 추천 smartstore.naver.com/example", "is_following": "false", "taken_at": "2026-11-23T17:13:18+09:00"},
                     {"handle": "spam_x", "code": "c1", "text": "중복"},
                     {"handle": "friend_y", "code": "c2", "text": "오 좋다", "is_following": True}]}

    def test_normalize_feed_comments(self):
        nd, w = adapter.normalize_doc(self.DOC, "feed-comments")
        by = {i["handle"]: i for i in nd["items"]}
        self.assertTrue(by["writer_a"]["is_post_author"])
        self.assertEqual(by["spam_x"]["source_author"], "writer_a")
        self.assertEqual(nd["posts"][0]["code"], "P1")
        self.assertTrue(any("중복 댓글 1개" in x for x in w))
        with self.assertRaises(adapter.AdapterError):
            adapter.normalize_doc(self.DOC, "replies")

    def test_candidates_exclude_author_and_following(self):
        res = feed_candidates.build_candidates([self.DOC], self_handle="")
        self.assertEqual([c["handle"] for c in res["candidates"]], ["spam_x"])

    def test_validate_cli(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "fc.json"
            fp.write_text(json.dumps(self.DOC, ensure_ascii=False), encoding="utf-8")
            cp = run_script("adapter.py", "validate", str(fp), "--expect", "feed-comments")
            self.assertEqual(cp.returncode, 0, cp.stderr.decode("utf-8", "replace"))
            self.assertIn("OK source=feed-comments", cp.stdout.decode("utf-8"))

    def test_bot_magnet_prefers_problem_posts(self):
        ai = {"text": "AI 에이전트로 반복 업무 자동화하는 방법 정리했어요. 막혔던 문제를 이렇게 해결했습니다. 여러분은 어떻게 하시나요?",
              "reply_count": 12}
        meme = {"text": "ㅋㅋ 월요일", "reply_count": 12}
        self.assertGreater(tbb_common.score_bot_magnet(ai)["score"], tbb_common.score_bot_magnet(meme)["score"])
        self.assertTrue(tbb_common.score_bot_magnet(meme)["is_low_yield"])


class TestHygiene(unittest.TestCase):
    def _files(self):
        return [p for p in SKILL.rglob("*") if p.is_file()]

    def test_exact_19_files_no_bytecode(self):
        rel = sorted(p.relative_to(SKILL).as_posix() for p in self._files())
        self.assertEqual(rel, EXPECTED_FILES)
        self.assertEqual(list(SKILL.rglob("__pycache__")), [])
        self.assertEqual(list(SKILL.rglob("*.pyc")), [])

    def test_data_templates_empty(self):
        self.assertEqual(json.loads((SKILL / "data" / "blocked_accounts.json").read_text(encoding="utf-8")), [])
        self.assertEqual(json.loads((SKILL / "data" / "labels.json").read_text(encoding="utf-8"))["labels"], [])

    def test_no_local_paths_or_personal_data(self):
        rx = re.compile(r"[A-Za-z]:[\\/]Users[\\/]|/home/[a-z]|/Users/[A-Za-z]|App[D]ata|@gmail\.com|@naver\.com", re.I)
        hits = [f"{p.relative_to(ROOT)}: {m.group(0)}" for p in ROOT.rglob("*")
                if p.is_file() and p.suffix in (".md", ".py", ".json", ".txt")
                for m in rx.finditer(p.read_text(encoding="utf-8", errors="ignore"))]
        self.assertEqual(hits, [])

    def test_no_hardcoded_self_handle(self):
        for name in ("feed_candidates.py", "reply_score.py", "bsk_scan.py"):
            src = (SCRIPTS / name).read_text(encoding="utf-8")
            for m in re.finditer(r"DEFAULT_SELF\s*=\s*(['\"])(.*?)\1", src):
                self.assertEqual(m.group(2), "", name)

    def test_stdlib_only(self):
        local = {p.stem for p in SCRIPTS.glob("*.py")}
        for py in SCRIPTS.glob("*.py"):
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                else:
                    continue
                for n in names:
                    top = n.split(".")[0]
                    self.assertTrue(top in sys.stdlib_module_names or top in local or top == "__future__", f"{py.name}: {n}")

    def test_skill_frontmatter_and_version(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: threads-bot-blocker\n"))
        self.assertIn("자동 차단 금지", text)
        self.assertEqual(tbb_common.VERSION, "3.0.0")


if __name__ == "__main__":
    unittest.main()
