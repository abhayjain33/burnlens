import json
import os
import shutil
import tempfile
import unittest

from burnlens import guard


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._saved = (guard.BASE, guard.CONFIG, guard.STATE_DIR, guard.EVENTS, guard.SESSIONS)
        self.real_home_files = self.snapshot_real()
        guard.BASE = os.path.join(self.home, ".burnlens")
        guard.CONFIG = os.path.join(guard.BASE, "guard.json")
        guard.STATE_DIR = os.path.join(guard.BASE, "guard-state")
        guard.EVENTS = os.path.join(guard.BASE, "guard-events.jsonl")
        guard.SESSIONS = os.path.join(guard.BASE, "guard-sessions.json")
        self.cfg = dict(guard.DEFAULTS)

    def tearDown(self):
        guard.BASE, guard.CONFIG, guard.STATE_DIR, guard.EVENTS, guard.SESSIONS = self._saved
        shutil.rmtree(self.home)
        # tests must never write to the real ~/.burnlens
        self.assertEqual(self.snapshot_real(), self.real_home_files)

    @staticmethod
    def snapshot_real():
        base = os.path.join(os.path.expanduser("~"), ".burnlens")
        out = {}
        for root, _, files in os.walk(base):
            for f in files:
                p = os.path.join(root, f)
                out[p] = os.path.getmtime(p)
        return out

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

    # status line and entry point
    def test_statusline(self):
        line = guard.statusline({"cost": {"total_cost_usd": 2.5}, "context_window": {
            "total_input_tokens": 50000, "used_percentage": 25,
            "current_usage": {"input_tokens": 0, "cache_read_input_tokens": 45000, "cache_creation_input_tokens": 5000}}},
            self.cfg)
        self.assertEqual(line, "🔥 $2.50 · ctx 50k (25%) · cache 90%")

    def run_hook(self, which, event):
        import io
        import sys
        from contextlib import redirect_stdout
        buf, stdin = io.StringIO(), sys.stdin
        sys.stdin = io.StringIO(json.dumps(event))
        try:
            with redirect_stdout(buf):
                guard.main([which])
        finally:
            sys.stdin = stdin
        return buf.getvalue()

    def test_master_switch_silences_every_guardrail_but_not_the_status_line(self):
        read = {"tool_name": "Read", "tool_input": {"file_path": self.file("yarn.lock", 5000)}}
        self.assertIn("permissionDecision", self.run_hook("pre-tool", read))
        cfg = dict(guard.DEFAULTS, enabled=False)
        guard.save_config(cfg)
        self.assertEqual(self.run_hook("pre-tool", read), "")
        self.assertEqual(self.run_hook("prompt", {"transcript_path": self.transcript(400000)}), "")
        self.assertIn("$1.00", self.run_hook("statusline", {"cost": {"total_cost_usd": 1.0}}))

    def test_env_switch(self):
        read = {"tool_name": "Read", "tool_input": {"file_path": self.file("yarn.lock", 5000)}}
        os.environ["BURNLENS_GUARD"] = "off"
        try:
            self.assertEqual(self.run_hook("pre-tool", read), "")
        finally:
            del os.environ["BURNLENS_GUARD"]

    # on/off comparison
    def data(self):
        calls, sessions = [], {}
        for sid, usd, n in (("on1", 1.0, 5), ("on2", 1.2, 5), ("on3", 0.8, 5), ("off1", 2.0, 5), ("off2", 2.4, 5), ("off3", 1.6, 5)):
            sessions[sid] = {"project": "p"}
            for i in range(n):
                calls.append({"s": sid, "ts": f"2026-09-20T10:0{i}:00Z", "m": "claude-opus-5-5", "i": 1, "o": 100,
                              "cr": 50000 if sid.startswith("off") else 30000, "cw": 500, "a": None, "usd": usd / n})
        return {"sessions": sessions, "calls": calls, "tools": [], "compactions": {}}

    def test_report_compares_sessions_by_tag(self):
        for sid in ("on1", "on2", "on3"):
            guard.record_session(sid, True)
        for sid in ("off1", "off2"):
            guard.record_session(sid, False)  # off3 untagged: guardrails weren't running
        r = guard.report(self.data())
        self.assertTrue(r["enough"])
        self.assertEqual((r["on"]["sessions"], r["off"]["sessions"]), (3, 3))
        self.assertAlmostEqual(r["on"]["usd_per_session"], 1.0)
        self.assertAlmostEqual(r["off"]["usd_per_session"], 2.0)
        self.assertLess(r["on"]["ctx_per_call"], r["off"]["ctx_per_call"])

    def test_toggling_mid_session_is_excluded(self):
        guard.record_session("on1", True)
        guard.record_session("on1", False)
        self.assertEqual(guard.load_sessions()["on1"], "mixed")
        self.assertEqual(guard.report(self.data())["on"]["sessions"], 0)

    def test_prevented_vs_allowed_reads(self):
        d = self.data()
        guard.log_event("read", "on1", path="/p/yarn.lock", tokens=10000, decision="ask")
        guard.log_event("read", "on2", path="/p/yarn.lock", tokens=10000, decision="ask")
        # on2: the user approved and the full read happened anyway
        d["tools"].append({"s": "on2", "n": "Read", "path": "/p/yarn.lock", "ts": "2099-01-01T00:00:00Z", "rt": 10000})
        # on1: Claude read a range instead, which doesn't undo the prevention
        d["tools"].append({"s": "on1", "n": "Read", "path": "/p/yarn.lock", "ranged": True, "ts": "2099-01-01T00:00:00Z", "rt": 300})
        r = guard.report(d)["reads"]
        self.assertEqual((r["questioned"], r["prevented"]), (2, 1))
        self.assertEqual(r["tokens_avoided"], 10000)  # event logged after the session's calls: no re-sends counted

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
