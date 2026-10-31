# -*- coding: utf-8 -*-
"""日迹 · 验收测试.

    python3 tests/test_journal.py

不依赖第三方库。跑的是真实的命令行入口，不是 mock。
"""
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "journal.py"
sys.path.insert(0, str(ROOT))

import journal  # noqa: E402


def run(*args, **kwargs):
    return subprocess.run(
        [sys.executable, str(CLI)] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **kwargs)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="riji-test-"))
        self.data = self.tmp / "data"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, date, body, *extra):
        return run("--data", str(self.data), "add", "--date", date,
                   "--body", body, *extra)


class TestAdd(Base):
    def test_add_creates_one_file_per_day(self):
        res = self.add("2026-10-22", "## 发生了什么\n今天下雨。")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        path = self.data / "days" / "2026-10" / "2026-10-22.md"
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"))
        self.assertIn("date: 2026-10-22", text)
        self.assertIn("今天下雨。", text)

    def test_empty_body_is_rejected(self):
        res = self.add("2026-10-22", "   ")
        self.assertEqual(res.returncode, 2)
        self.assertIn("拒绝创建空日记", res.stdout)
        self.assertFalse((self.data / "days" / "2026-10" / "2026-10-22.md").exists())

    def test_append_keeps_old_text(self):
        self.add("2026-10-22", "上午写了方案。", "--mood", "平静")
        self.add("2026-10-22", "晚上去散步。", "--append")
        text = (self.data / "days" / "2026-10" / "2026-10-22.md").read_text(encoding="utf-8")
        self.assertIn("上午写了方案。", text)
        self.assertIn("晚上去散步。", text)
        # 心情不应该被追加覆盖成空
        self.assertIn("mood: 平静", text)

    def test_bad_date_is_rejected(self):
        res = self.add("昨天下午", "x")
        self.assertNotEqual(res.returncode, 0)


class TestParse(Base):
    def test_excerpt_skips_headings(self):
        self.add("2026-10-22", "## 发生了什么\n闹钟没响，醒来看见窗帘缝里一条很亮的白。", "--mood", "平静")
        item = journal.parse_entry(self.data / "days" / "2026-10" / "2026-10-22.md")
        self.assertEqual(item["mood"], "平静")
        self.assertFalse(item["excerpt"].startswith("发生了什么"))
        self.assertIn("闹钟没响", item["excerpt"])

    def test_markdown_rendering(self):
        html = journal.md_to_html("## 发生了什么\n**今天**很好。\n\n- 一条\n- 两条")
        self.assertIn("<h4>发生了什么</h4>", html)
        self.assertIn("<strong>今天</strong>", html)
        self.assertEqual(html.count("<li>"), 2)

    def test_html_is_escaped(self):
        out = journal.inline("<script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)


class TestTopicTimes(Base):
    def test_topic_times_round_trip(self):
        path = self.data / "days" / "2026-10" / "2026-10-22.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = journal.dump_entry(
            dt.date(2026, 10, 22), "", "", [], "",
            "## 上午\n面条。\n\n## 下午\n跑步。",
            "2026-10-22T09:00:00",
            extra={"topic_times": ["2026-10-22 09:12:00", "2026-10-22 20:41:00"]},
        )
        path.write_text(text, encoding="utf-8")
        item = journal.parse_entry(path)
        self.assertEqual(
            json.loads(item["topic_times"]),
            ["2026-10-22 09:12:00", "2026-10-22 20:41:00"],
        )

    def test_missing_topic_times_is_empty(self):
        self.add("2026-10-22", "没有时间的老日记。")
        item = journal.parse_entry(self.data / "days" / "2026-10" / "2026-10-22.md")
        self.assertEqual(item["topic_times"], "")


class TestBuild(Base):
    def test_build_inlines_all_entries(self):
        self.add("2026-10-01", "国庆。")
        self.add("2026-10-02", "去看了海。", "--mood", "兴奋")
        out = self.tmp / "index.html"
        res = run("--data", str(self.data), "build", "--out", str(out))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        html = out.read_text(encoding="utf-8")
        self.assertIn("去看了海", html)
        self.assertNotIn("__RIJI_DATA__", html)
        self.assertIn("<!doctype html>", html.lower())
        # 必须是自包含的：不引用外部脚本 / 样式
        self.assertNotIn("<script src=", html)
        self.assertNotIn("<link rel=\"stylesheet\"", html)

    def test_empty_data_still_builds(self):
        out = self.tmp / "index.html"
        res = run("--data", str(self.data), "build", "--out", str(out))
        self.assertEqual(res.returncode, 0)
        self.assertTrue(out.exists())


class TestStats(Base):
    def test_stats_and_export(self):
        for d in ("2026-10-01", "2026-10-02", "2026-10-05"):
            self.add(d, "记一笔。" + d)
        res = run("--data", str(self.data), "stats")
        self.assertEqual(res.returncode, 0)
        self.assertIn("总天数   3 天", res.stdout)

        out = self.tmp / "year.md"
        res = run("--data", str(self.data), "export", "--year", "2026", "--out", str(out))
        self.assertEqual(res.returncode, 0)
        text = out.read_text(encoding="utf-8")
        self.assertEqual(text.count("### 2026-10-"), 3)

    def test_streak_counts(self):
        days = {journal.dt.date(2026, 10, d) for d in (1, 2, 3, 7)}
        current, best = journal.streaks(days, journal.dt.date(2026, 10, 4))
        self.assertEqual(best, 3)
        self.assertEqual(current, 3)


class TestSeal(unittest.TestCase):
    def _entry(self, **kw):
        e = {"body_md": "秘密", "html": "<p>秘密</p>", "excerpt": "秘密", "text": "秘密", "words": "12"}
        e.update(kw)
        return e

    def test_letter_is_hidden_until_delivery(self):
        e = self._entry(deliver="2099-01-01")
        journal.seal_entries([e], journal.dt.date(2026, 10, 22))
        self.assertEqual(e["sealed"], "letter")
        self.assertEqual(e["body_md"], "")
        self.assertEqual(e["words"], "12")  # 字数不是秘密，热力图还要用

    def test_lock_is_hidden_until_unlock(self):
        e = self._entry(lock_hash=journal.hash_password("123456"))
        journal.seal_entries([e], journal.dt.date(2026, 10, 22))
        self.assertEqual(e["sealed"], "lock")
        self.assertEqual(e["html"], "")

    def test_password_lock(self):
        h = journal.hash_password("123456")
        e = {"lock_hash": h, "body_md": "秘密", "html": "<p>秘密</p>",
             "excerpt": "秘密", "text": "秘密", "words": "1"}
        journal.seal_entries([e], journal.dt.date(2026, 10, 22))
        self.assertEqual(e["sealed"], "lock")
        self.assertEqual(e["body_md"], "")
        self.assertTrue(journal.check_password({"lock_hash": h}, "123456"))
        self.assertFalse(journal.check_password({"lock_hash": h}, "000000"))
        self.assertFalse(journal.check_password({"lock_hash": h}, ""))
        self.assertFalse(journal.check_password({}, "123456"))

    def test_expired_seal_shows_content(self):
        e = self._entry(lock_until="2020-01-01")
        journal.seal_entries([e], journal.dt.date(2026, 10, 22))
        self.assertEqual(e["sealed"], "")
        self.assertEqual(e["body_md"], "秘密")


class TestPayload(unittest.TestCase):
    def test_skill_manifest_is_valid(self):
        path = ROOT.parent / "skill" / "SKILL.md"
        if not path.exists():
            self.skipTest("skill 还没打包")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"))
        fm = text.split("---")[1]
        self.assertIn("name: riji", fm)
        self.assertIn("description:", fm)
        body = fm.split("description:", 1)[1].splitlines()[0]
        self.assertLess(len(body), 600, "description 太长，部分平台会截断")

    def test_file_budget(self):
        files = [p for p in (ROOT.parent / "skill").rglob("*") if p.is_file()]
        self.assertLessEqual(len(files), 30, "Skill 文件数超过平台上限 (30)")
        total = sum(p.stat().st_size for p in files)
        self.assertLess(total, 40 * 1024 * 1024, "Skill 体积超过平台上限 (40MB)")


class TestHistory(Base):
    def _days(self):
        self.add("2024-06-03", "## 跑步 #跑步\n跑了三公里。")
        self.add("2025-10-26", "## 跑步 #跑步\n今天也跑了。")
        self.add("2026-04-08", "## 写作 #小说\n写了两千字。")

    def test_tag_stats_groups_across_years(self):
        self._days()
        st = journal.tag_stats(self.data)
        names = [t["name"] for t in st["items"]]
        self.assertEqual(sorted(names), ["小说", "跑步"])
        run_tag = [t for t in st["items"] if t["name"] == "跑步"][0]
        self.assertEqual(run_tag["days"], 2)
        self.assertEqual(run_tag["first"], "2024-06-03")
        self.assertEqual(run_tag["last"], "2025-10-26")
        self.assertEqual(run_tag["years"], {"2024": 1, "2025": 1})

    def test_tag_snapshot_keeps_history(self):
        self._days()
        a = journal.save_tag_snapshot(self.data)
        self.assertFalse(a["unchanged"])
        self.assertEqual(sorted(a["added"]), ["小说", "跑步"])
        b = journal.save_tag_snapshot(self.data)
        self.assertTrue(b["unchanged"])
        self.assertEqual(len(journal.tag_snapshots(self.data)), 1)
        self.add("2026-10-26", "## 健身 #健身\n练了一小时。")
        c = journal.save_tag_snapshot(self.data)
        self.assertFalse(c["unchanged"])
        self.assertEqual(c["added"], ["健身"])
        self.assertEqual(c["gone"], [])
        self.assertEqual(len(journal.tag_snapshots(self.data)), 2)

    def test_onthisday_finds_same_day(self):
        self._days()
        items = journal.onthisday(self.data, dt.date(2026, 10, 26))
        self.assertEqual([i["date"] for i in items], ["2025-10-26"])
        self.assertEqual(items[0]["years"], 1)

    def test_search_first_returns_earliest(self):
        self._days()
        self.add("2025-01-05", "## 随手\n想过辞职。")
        hits = journal.search_entries(self.data, ["辞职"], "first")
        self.assertEqual(hits[0]["date"], "2025-01-05")
        self.assertIn("辞职", hits[0]["snippet"])

    def test_search_matches_inside_body(self):
        self._days()
        hits = journal.search_entries(self.data, ["两千字"])
        self.assertEqual(hits[0]["date"], "2026-04-08")


if __name__ == "__main__":
    unittest.main(verbosity=2)
