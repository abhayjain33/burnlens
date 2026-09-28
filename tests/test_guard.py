import json
import os
import shutil
import tempfile
import unittest

from burnlens import guard


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._saved = (guard.BASE, guard.CONFIG, guard.STATE_DIR, guard.SPEND_CACHE)
        guard.BASE = os.path.join(self.home, ".burnlens")
        guard.CONFIG = os.path.join(guard.BASE, "guard.json")
        guard.STATE_DIR = os.path.join(guard.BASE, "guard-state")
        guard.SPEND_CACHE = os.path.join(guard.BASE, "spend-cache.json")
        self.cfg = dict(guard.DEFAULTS)

    def tearDown(self):
        guard.BASE, guard.CONFIG, guard.STATE_DIR, guard.SPEND_CACHE = self._saved
        shutil.rmtree(self.home)

    def file(self, name, tokens):
        p = os.path.join(self.home, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("x" * int(tokens * guard.BYTES_PER_TOKEN))
        return p

    # read guard
    def test_generated_file_is_questioned(self):
        d, why = guard.check_read({"file_path": self.file("yarn.lock", 5000)}, self.cfg)
        self.assertEqual(d, "ask")
        self.assertIn("generated", why)

    def test_small_generated_file_is_fine(self):
        self.assertIsNone(guard.check_read({"file_path": self.file("yarn.lock", 500)}, self.cfg))

    def test_large_source_file_is_questioned_but_ranged_read_is_not(self):
        p = self.file("src/big.py", 30000)
        self.assertEqual(guard.check_read({"file_path": p}, self.cfg)[0], "ask")
        self.assertIsNone(guard.check_read({"file_path": p, "offset": 100, "limit": 80}, self.cfg))

    def test_read_guard_modes(self):
        p = self.file("dist/app.min.js", 9000)
        self.assertEqual(guard.check_read({"file_path": p}, dict(self.cfg, read_guard="deny"))[0], "deny")
        self.assertIsNone(guard.check_read({"file_path": p}, dict(self.cfg, read_guard="off")))

    def test_missing_file_is_left_to_claude_code(self):
        self.assertIsNone(guard.check_read({"file_path": "/nope/x"}, self.cfg))

    # loop guard
    def test_loop_guard_fires_once_at_threshold(self):
        ev = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "pytest"}, "tool_response": {"stdout": "1 failed"}}
        out = [guard.post_tool(ev, self.cfg) for _ in range(5)]
        self.assertEqual([o is not None for o in out], [False, False, True, False, False])

    def test_loop_guard_resets_when_output_changes(self):
        ev = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "pytest"}}
        for out in ("1 failed", "1 failed", "0 failed", "0 failed"):
            self.assertIsNone(guard.post_tool(dict(ev, tool_response={"stdout": out}), self.cfg))

    # context alert
    def transcript(self, *contexts):
        p = os.path.join(self.home, "t.jsonl")
        with open(p, "w") as f:
            for i, ctx in enumerate(contexts):
                f.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
                    "input_tokens": 0, "cache_read_input_tokens": ctx, "cache_creation_input_tokens": 0}}}) + "\n")
            f.write(json.dumps({"type": "assistant", "isSidechain": True, "message": {"model": "claude-haiku-4-5", "usage": {
                "input_tokens": 0, "cache_read_input_tokens": 999999, "cache_creation_input_tokens": 0}}}) + "\n")
        return p

    def test_context_alert_levels_and_ignores_subagents(self):
        state = {}
        ev = {"transcript_path": self.transcript(90000)}
        self.assertIsNone(guard.context_alert(ev, self.cfg, state))  # the 999k subagent line doesn't count
        ev = {"transcript_path": self.transcript(160000)}
        self.assertIn("160k", guard.context_alert(ev, self.cfg, state))
        self.assertIsNone(guard.context_alert({"transcript_path": self.transcript(190000)}, self.cfg, state))
        self.assertIn("210k", guard.context_alert({"transcript_path": self.transcript(210000)}, self.cfg, state))

    # budget
    def test_budget_warns_at_80_percent_once(self):
        cfg, state = dict(self.cfg, daily_usd=10.0), {}
        _, msg = guard.budget_check(cfg, state, spent={"today": 8.5, "month": 8.5})
        self.assertIn("85%", msg)
        self.assertEqual(guard.budget_check(cfg, state, spent={"today": 8.6, "month": 8.6}), (None, None))

    def test_budget_block_only_when_chosen(self):
        spent = {"today": 12.0, "month": 12.0}
        blocked, msg = guard.budget_check(dict(self.cfg, daily_usd=10.0), {}, spent)
        self.assertIsNone(blocked)
        self.assertIn("over your daily limit", msg)
        blocked, _ = guard.budget_check(dict(self.cfg, daily_usd=10.0, budget_action="block"), {}, spent)
        self.assertIn("paused", blocked)

    def test_no_budget_no_parsing(self):
        self.assertEqual(guard.budget_check(self.cfg, {}), (None, None))

    # status line and entry point
    def test_statusline(self):
        line = guard.statusline({"cost": {"total_cost_usd": 2.5}, "context_window": {
            "total_input_tokens": 50000, "used_percentage": 25,
            "current_usage": {"input_tokens": 0, "cache_read_input_tokens": 45000, "cache_creation_input_tokens": 5000}}},
            self.cfg)
        self.assertEqual(line, "🔥 $2.50 · ctx 50k (25%) · cache 90%")

    def test_config_ignores_unknown_and_bad_files(self):
        os.makedirs(guard.BASE)
        with open(guard.CONFIG, "w") as f:
            f.write('{"read_guard": "deny", "evil": 1}')
        cfg = guard.config()
        self.assertEqual(cfg["read_guard"], "deny")
        self.assertNotIn("evil", cfg)
        with open(guard.CONFIG, "w") as f:
            f.write("not json")
        self.assertEqual(guard.config(), guard.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
