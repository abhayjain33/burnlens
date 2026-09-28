import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

from burnlens import fixes, sync


def iso(days_ago, minutes=0):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago, minutes=-minutes)
    return t.isoformat().replace("+00:00", "Z")


class FixesTest(unittest.TestCase):
    def setUp(self):
        # everything the detectors read from "home" lives in a throwaway dir
        self.home = tempfile.mkdtemp()
        self.proj = os.path.join(self.home, "work", "shop")
        os.makedirs(self.proj)
        self._saved = fixes.HOME, fixes.LEDGER
        fixes.HOME = self.home
        fixes.LEDGER = os.path.join(self.home, ".burnlens", "fixes.json")
        self.data = {"sessions": {}, "calls": [], "tools": [], "agents": [], "compactions": {}}
        self.n = 0

    def tearDown(self):
        fixes.HOME, fixes.LEDGER = self._saved
        shutil.rmtree(self.home)

    def session(self, days_ago, model="claude-opus-5-5", calls=4, agent=None, sid=None):
        self.n += 1
        sid = sid or f"s{self.n}"
        self.data["sessions"].setdefault(sid, {"project": "shop", "cwd": self.proj, "title": sid})
        for i in range(calls):
            self.data["calls"].append({"id": f"{sid}-c{i}-{agent}", "ts": iso(days_ago, i), "s": sid, "m": model,
                                       "i": 5, "o": 500, "cr": 20000, "cw": 1000, "a": agent, "c": "Development",
                                       "usd": 0.02})
        return sid

    def tool(self, sid, days_ago, name, rt, cx=None, **extra):
        self.data["tools"].append(dict({"id": f"t{len(self.data['tools'])}", "ts": iso(days_ago, 1), "s": sid, "n": name,
                                        "srv": name.split("__")[1] if name.startswith("mcp__") else None,
                                        "t": name, "rt": rt, "cx": rt * 3 if cx is None else cx, "a": None}, **extra))

    def kinds(self, recs):
        return {r["kind"] for r in recs}

    def test_generated_file_reads_become_deny_rules(self):
        s = self.session(3)
        self.tool(s, 3, "Read", 9000, path=os.path.join(self.proj, "package-lock.json"))
        self.tool(s, 3, "Read", 9000, path=os.path.join(self.proj, "src", "app.ts"))  # real source: never blocked
        recs = [r for r in fixes.recommend(self.data) if r["kind"] == "large_file_reads"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["action"]["rules"], ["Read(**/package-lock.json)"])
        self.assertEqual(recs[0]["confidence"], "measured")
        self.assertTrue(recs[0]["action"]["file"].endswith(os.path.join(".claude", "settings.local.json")))

    def test_noisy_command_needs_repeats_and_volume(self):
        s = self.session(2)
        for _ in range(3):
            self.tool(s, 2, "Bash", 5000, cmd="npm test")
        self.tool(s, 2, "Bash", 9000, cmd="git diff")  # once is not a pattern
        rec = [r for r in fixes.recommend(self.data) if r["kind"] == "noisy_commands"][0]
        self.assertEqual(rec["metric"]["cmds"], ["npm test"])
        self.assertIn("`npm test`", rec["action"]["text"])

    def test_heavy_mcp_tool(self):
        s = self.session(2)
        for _ in range(2):
            self.tool(s, 2, "mcp__playwright__browser_snapshot", 15000)
        self.assertIn("heavy_mcp_tool", self.kinds(fixes.recommend(self.data)))

    def test_large_claude_md_in_project(self):
        self.session(1)
        with open(os.path.join(self.proj, "CLAUDE.md"), "w") as f:
            f.write("x" * 3600 * 3)  # ~3,000 tokens
        rec = [r for r in fixes.recommend(self.data) if r["kind"] == "large_instructions"][0]
        self.assertEqual(rec["action"]["type"], "claude_task")
        self.assertEqual(rec["metric"]["tokens_before"], 3000)

    def test_unused_mcp_server_needs_enough_sessions(self):
        with open(os.path.join(self.home, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"jira": {}, "github": {}}}, f)
        sids = [self.session(1) for _ in range(5)]
        self.tool(sids[0], 1, "mcp__github__search_code", 100)
        recs = [r for r in fixes.recommend(self.data) if r["kind"] == "unused_mcp_server"]
        self.assertEqual([r["metric"]["name"] for r in recs], ["jira"])
        self.assertEqual(recs[0]["action"]["command"], "claude mcp remove jira -s user")

    def test_expensive_subagent_model(self):
        self.session(1)
        self.session(1, agent="Explore", calls=40)
        rec = [r for r in fixes.recommend(self.data) if r["kind"] == "subagent_model"]
        self.assertEqual(len(rec), 1)
        self.assertGreater(rec[0]["usd_30d"], 0)

    def model_recs(self):
        return {(r["kind"], r["metric"].get("agent")): r for r in fixes.recommend(self.data)
                if r["kind"] in ("subagent_model", "reasoning_model")}

    def test_planning_agents_are_never_told_to_downgrade(self):
        for agent in ("Plan", "code-reviewer", "architect"):
            self.session(1, agent=agent, calls=60)
            self.session(2, agent=agent, calls=60)
        self.assertEqual(self.model_recs(), {})

    def test_planning_agent_on_cheap_model_is_told_to_upgrade(self):
        self.session(1, agent="Plan", calls=30, model="claude-sonnet-5")
        self.session(2, agent="Plan", calls=30, model="claude-sonnet-5")
        rec = self.model_recs()[("reasoning_model", "Plan")]
        self.assertIsNone(rec["usd_30d"])
        self.assertGreater(rec["extra_usd_30d"], 0)
        self.assertIn("model: opus", rec["action"]["prompt"])

    def test_general_purpose_only_when_it_implements(self):
        self.session(1, agent="general-purpose", calls=60)  # category Development
        self.assertIn(("subagent_model", "general-purpose"), self.model_recs())
        for c in self.data["calls"]:
            c["c"] = "Analysis"
        self.assertEqual(self.model_recs(), {})

    def test_builtin_agent_advice_avoids_global_subagent_override(self):
        self.session(1, agent="Explore", calls=60)
        rec = self.model_recs()[("subagent_model", "Explore")]
        self.assertIn("Do not set CLAUDE_CODE_SUBAGENT_MODEL", rec["action"]["prompt"])

    def test_mixed_model_agent_counts_only_expensive_runs(self):
        # runs already on cheap models must not cancel out the saving on the Opus runs
        for d in (1, 2):
            self.session(d, agent="Explore", calls=60)
        for d in (1, 2, 3, 4):
            self.session(d, agent="Explore", calls=200, model="claude-haiku-4-5")
        rec = self.model_recs()[("subagent_model", "Explore")]
        self.assertIn("2 of 6 runs", rec["detail"])
        self.assertGreater(rec["usd_30d"], 0)

    def test_opusplan_for_plan_then_implement_on_opus(self):
        for d in (1, 2, 3):
            s = self.session(d, calls=40)
            self.tool(s, d, "Edit", 200, path=os.path.join(self.proj, "src", "a.ts"))
        rec = [r for r in fixes.recommend(self.data) if r["kind"] == "plan_then_implement"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["action"]["command"], "/model opusplan")

    def test_no_opusplan_when_already_on_sonnet(self):
        for d in (1, 2, 3):
            s = self.session(d, calls=40, model="claude-sonnet-5")
            self.tool(s, d, "Edit", 200, path=os.path.join(self.proj, "src", "a.ts"))
        self.assertNotIn("plan_then_implement", self.kinds(fixes.recommend(self.data)))

    def test_model_switch_that_costs_more_per_run_is_flagged(self):
        for d in (10, 8, 6):
            self.session(d, agent="Explore", calls=40)  # $0.80 per run
        rec = self.model_recs()[("subagent_model", "Explore")]
        fixes.mark_applied(self.data, rec)
        for _ in range(3):  # cheaper model, but it needed 3x the turns: $2.40 per run
            self.session(0, agent="Explore", calls=120, model="claude-sonnet-5")
        row = fixes.savings(self.data)[0]
        self.assertEqual(row["status"], "made it worse")
        self.assertLess(row["saved_usd"], 0)

    def test_applied_fix_is_measured_against_baseline(self):
        lock = os.path.join(self.proj, "yarn.lock")
        for d in (20, 18, 15):  # before: every session reads the lockfile
            s = self.session(d)
            self.tool(s, d, "Read", 12000, path=lock)
        rec = [r for r in fixes.recommend(self.data) if r["kind"] == "large_file_reads"][0]
        fixes.mark_applied(self.data, rec)
        self.assertNotIn(rec["id"], {r["id"] for r in fixes.recommend(self.data)})  # applied fixes drop out

        row = fixes.savings(self.data)[0]
        self.assertEqual(row["status"], "measuring")  # no usage since the fix yet

        for d in range(3):  # after: sessions without lockfile reads, timestamped after applied_at
            self.data["sessions"][f"after{d}"] = {"project": "shop", "cwd": self.proj}
            self.data["calls"].append({"id": f"a{d}", "ts": iso(0, 5 + d), "s": f"after{d}", "m": "claude-opus-5-5",
                                       "i": 5, "o": 50, "cr": 1000, "cw": 100, "a": None, "usd": 0.001})
        row = fixes.savings(self.data)[0]
        self.assertEqual(row["status"], "measured")
        self.assertGreater(row["saved_tokens"], 0)
        self.assertGreater(row["saved_usd"], 0)

    def test_dismissed_fix_is_not_recommended_again(self):
        s = self.session(2)
        for _ in range(2):
            self.tool(s, 2, "mcp__figma__get_design_context", 20000)
        rec = fixes.recommend(self.data)[0]
        fixes.dismiss(rec["id"])
        self.assertNotIn(rec["id"], {r["id"] for r in fixes.recommend(self.data)})

    def test_demo_mode_ignores_local_config(self):
        with open(os.path.join(self.home, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"jira": {}}}, f)
        for _ in range(5):
            self.session(1)
        self.assertNotIn("unused_mcp_server", self.kinds(fixes.recommend(self.data, local=False)))


class SyncPrivacyTest(unittest.TestCase):
    def test_local_fields_never_leave_the_machine(self):
        tools = [{"id": "t1", "n": "Read", "rt": 10, "path": "/Users/me/secret/plan.md"},
                 {"id": "t2", "n": "Bash", "rt": 10, "cmd": "npm test"}]
        sessions = {"s1": {"project": "app", "cwd": "/Users/me/app", "title": None}}
        out_tools, out_sessions = sync.strip_local(tools, sessions)
        self.assertNotIn("/Users/me", json.dumps([out_tools, out_sessions]))
        self.assertNotIn("npm test", json.dumps(out_tools))
        self.assertEqual(out_sessions["s1"]["project"], "app")
        self.assertIn("path", tools[0])  # caller's data is left intact


if __name__ == "__main__":
    unittest.main()
