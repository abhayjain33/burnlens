import os
import unittest

from burnlens import classify, parser, pricing

ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "projects")


class ParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = parser.load(ROOT)
        cls.main = [c for c in cls.data["calls"] if not c["a"]]
        cls.tools = {t["n"]: t for t in cls.data["tools"]}

    def test_dedupes_split_records_and_skips_synthetic(self):
        self.assertEqual(len(self.main), 6)
        self.assertEqual(self.main[0]["cw"], 20000)

    def test_session_metadata(self):
        s = self.data["sessions"]["s1"]
        self.assertEqual(s["title"], "Fix login bug")
        self.assertEqual(s["project"], "demo-app")

    def test_tool_result_tokens(self):
        self.assertEqual(self.tools["Read"]["rt"], 1000)  # 3600 chars / 3.6

    def test_skill_counts_injected_instructions(self):
        self.assertEqual(self.tools["Skill"]["skill"], "pdf")
        self.assertGreater(self.tools["Skill"]["rt"], 2000)

    def test_mcp_split(self):
        t = self.tools["mcp__github__get_file_contents"]
        self.assertEqual((t["srv"], t["t"]), ("github", "get_file_contents"))

    def test_carried_stops_at_compaction(self):
        # Read result lands before r2, r3, r4; the compaction drops it before r5.
        self.assertEqual(self.tools["Read"]["cx"], 1000 * 3)
        self.assertEqual(self.tools["mcp__github__get_file_contents"]["cx"], 0)

    def test_work_type_per_turn(self):
        self.assertEqual(self.main[0]["c"], "Development")
        self.assertEqual(self.main[-1]["c"], "Documentation")

    def test_subagent_transcript(self):
        sub = [c for c in self.data["calls"] if c["a"]]
        self.assertEqual([c["a"] for c in sub], ["Explore"])

    def test_cost(self):
        # opus-5-5: 10 in @ $4, 100 out @ $20, 20000 cache write (5m) @ $4 * 1.25
        self.assertAlmostEqual(self.main[0]["usd"], (10 * 4 + 100 * 20 + 20000 * 5) / 1e6)


class PricingTest(unittest.TestCase):
    def test_longest_prefix(self):
        self.assertEqual(pricing.rates("claude-opus-5-5")[0], 4.0)
        self.assertEqual(pricing.rates("claude-opus-5")[0], 5.0)
        self.assertIsNone(pricing.rates("gpt-4o"))


class ClassifyTest(unittest.TestCase):
    def test_read_only_is_analysis(self):
        self.assertEqual(classify.classify_turn([("Read", {}), ("Grep", {})]), "Analysis")

    def test_css_edit_is_design(self):
        self.assertEqual(classify.classify_turn([("Edit", {"file_path": "a.scss"})]), "Design")

    def test_no_tools_falls_back_to_keywords(self):
        self.assertEqual(classify.classify_turn([], "please write docs for this"), "Documentation")
        self.assertEqual(classify.classify_turn([], "hello"), "Conversation")


if __name__ == "__main__":
    unittest.main()
