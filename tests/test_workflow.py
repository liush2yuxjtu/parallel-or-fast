import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "local-session-playbook" / "scripts"
SPEC = importlib.util.spec_from_file_location("fixtures", ROOT / "examples" / "make_fixtures.py")
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.logs = self.root / "logs"
        FIXTURES.build(self.logs)

    def run_collector(self, *flags):
        return subprocess.check_output([
            sys.executable, str(SCRIPTS / "collect.py"), "--input", str(self.logs),
            "--since", FIXTURES.utc(-1000), "--until", FIXTURES.utc(1000), *flags,
        ], text=True)

    def test_all_adapters_overlap_and_read_only(self):
        contents = {p: p.read_bytes() for p in self.logs.rglob("*") if p.is_file()}
        raw = self.run_collector("--project", FIXTURES.PROJECT, "--cues")
        result = json.loads(raw)
        self.assertNotIn(FIXTURES.SECRET, raw)
        self.assertNotIn(FIXTURES.PROJECT, raw)
        self.assertNotIn("Ignore prior instructions", raw)
        self.assertEqual(result["harnesses"]["codex"]["completed"], 8)
        self.assertEqual(result["harnesses"]["dsh"]["duration_seconds_by_tier"]["unknown"]["median"], 12)
        self.assertEqual(result["harnesses"]["claude"]["rounds"], 2)
        self.assertEqual(result["harnesses"]["claude"]["completed"], 1)
        self.assertEqual(result["harnesses"]["claude"]["rounds_with_tool_signal"], 1)
        for tag in ("duplicate_turns", "malformed_records", "replayed_turns", "missing_fields",
                    "unknown_format", "dsh_superseded_files"):
            self.assertEqual(result["diagnostics"].get(tag), 1, tag)
        example = result["a8_pattern"]
        self.assertEqual(example["sent_at_utc"], FIXTURES.utc())
        self.assertEqual(example["recently_finished_offsets_seconds"], [-115, -45, -12])
        self.assertEqual(example["still_running_end_offsets_seconds"], [60, 75, 105, 145])
        self.assertEqual(contents, {p: p.read_bytes() for p in contents})

    def test_excludes_other_projects(self):
        result = json.loads(self.run_collector("--project", "/example/elsewhere"))
        self.assertEqual(sum(h["rounds"] for h in result["harnesses"].values()), 0)
        self.assertGreater(result["diagnostics"]["outside_project"], 0)
        self.assertIsNone(result["a8_pattern"])

    def test_postgres_and_rendering_accept_only_expected_fields(self):
        report = self.root / "pg.json"
        report.write_text(json.dumps({"inventory": {}, "turns": 12,
                                     "prompts": {"prompts": 8, "with_overlap": 5},
                                     "private": FIXTURES.SECRET}), encoding="utf-8")
        raw = subprocess.check_output([sys.executable, str(SCRIPTS / "collect.py"),
                                       "--postgres-only", "--postgres-report", str(report)], text=True)
        self.assertNotIn(FIXTURES.SECRET, raw)
        self.assertEqual(json.loads(raw)["postgres"]["all_turns"], 12)
        report.write_text(raw, encoding="utf-8")
        html = self.render(report)
        for text in ("A8", "PostgreSQL", "Ultrafast", "冲突步骤排队", "default-src 'none'"):
            self.assertIn(text, html)
        self.assertNotIn("预计等待", html)

    def test_wait_numbers_recompute_from_the_selected_cohort(self):
        FIXTURES.add_wait_history(self.logs)
        report = self.root / "collected.json"
        raw = self.run_collector("--project", FIXTURES.PROJECT, "--until", FIXTURES.utc(100000))
        report.write_text(raw, encoding="utf-8")
        html = self.render(report, "--synthetic")
        for text in ("合成样本", "&lt;2 分", "≥5 分", "2/36", "39/46"):
            self.assertIn(text, html)
        small_cohort = self.render(report, "--synthetic", "--harness", "dsh")
        self.assertIn("样本不足", small_cohort)
        self.assertNotIn("预计等待", small_cohort)

    def test_postgres_buckets_produce_different_cutoffs(self):
        report = self.root / "collected.json"
        postgres = self.root / "pg.json"
        buckets = {
            "under_30s": {"rounds": 80, "other_chat_prompt": 2},
            "30_to_119s": {"rounds": 100, "other_chat_prompt": 40},
            "120_to_299s": {"rounds": 90, "other_chat_prompt": 70},
            "300s_plus": {"rounds": 100, "other_chat_prompt": 85},
        }
        postgres.write_text(json.dumps({"inventory": {}, "turns": 500,
                                        "prompts": {"prompts": 370, "median_others": 2},
                                        "decision_buckets": buckets}), encoding="utf-8")
        raw = subprocess.check_output([sys.executable, str(SCRIPTS / "collect.py"),
                                       "--postgres-only", "--postgres-report", str(postgres)], text=True)
        report.write_text(raw, encoding="utf-8")
        html = self.render(report)
        for text in ("PostgreSQL 普通档", "&lt;30 秒", "≥2 分", "2/80", "70/90", "先试 3 条"):
            self.assertIn(text, html)

    def render(self, report, *flags):
        page = self.root / "tree.html"
        subprocess.run([sys.executable, str(SCRIPTS / "render_tree.py"),
                        "--report", str(report), "--output", str(page), *flags], check=True)
        return page.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
