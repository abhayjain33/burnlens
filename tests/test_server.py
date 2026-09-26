"""Org server integration tests. Need Postgres and the server extras:

  docker compose -f deploy/docker-compose.yml up -d db
  DATABASE_URL=postgresql://burnlens:burnlens@localhost:5432/burnlens_test python -m unittest tests.test_server

Skipped when DATABASE_URL isn't set. Uses its own database, which it resets.
"""

import os
import unittest

DSN = os.environ.get("DATABASE_URL")


@unittest.skipUnless(DSN, "DATABASE_URL not set")
class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from burnlens.server import db

        cls.db = db
        with db.connect() as conn:
            conn.execute("drop table if exists compactions, agent_runs, tools, calls, sessions, tokens, users cascade")
        db.init()

    def payload(self, email="a@x.io", team="Core", call_id="r1", rt=100, cx=0):
        return {
            "user": {"email": email, "name": "A", "team": team},
            "client": {"version": "test"},
            "sessions": {"s-" + email: {"project": "app", "title": None, "start": "2026-09-20T10:00:00Z", "end": "2026-09-20T10:05:00Z"}},
            "calls": [{"id": call_id, "s": "s-" + email, "ts": "2026-09-20T10:00:01Z", "m": "claude-opus-5-5",
                       "i": 5, "o": 50, "cr": 1000, "cw": 200, "th": 0, "a": None, "c": "Development", "usd": 0.01}],
            "tools": [{"id": "t-" + call_id, "s": "s-" + email, "ts": "2026-09-20T10:00:01Z", "n": "mcp__github__search",
                       "srv": "github", "t": "search", "rt": rt, "cx": cx, "err": False, "c": "Development", "a": None}],
            "agents": [],
            "compactions": {},
        }

    def test_ingest_is_idempotent_and_updates_carried(self):
        self.db.ingest(self.payload(email="idem@x.io", cx=100))
        self.db.ingest(self.payload(email="idem@x.io", cx=900))
        with self.db.connect() as conn:
            n = conn.execute("select count(*) n from calls c join users u on u.id = c.user_id where u.email = 'idem@x.io'").fetchone()["n"]
            cx = conn.execute("select carried from tools t join users u on u.id = t.user_id where u.email = 'idem@x.io'").fetchone()["carried"]
        self.assertEqual(n, 1)
        self.assertEqual(cx, 900)

    def test_admin_team_overrides_client(self):
        self.db.ingest(self.payload(email="lock@x.io", team="ClientSays"))
        self.db.set_team("lock@x.io", "AdminSays")
        self.db.ingest(self.payload(email="lock@x.io", team="ClientSays", call_id="r2"))
        users = {u["email"]: u for u in self.db.list_users()}
        self.assertEqual(users["lock@x.io"]["team"], "AdminSays")

    def test_viewer_dataset_hides_people(self):
        self.db.ingest(self.payload(email="view@x.io"))
        viewer = self.db.dataset(0, admin=False)
        admin = self.db.dataset(0, admin=True)
        self.assertEqual(viewer["people"], {})
        self.assertTrue(all(s["user"] is None for s in viewer["sessions"].values()))
        self.assertIn("view@x.io", admin["people"])
        self.assertTrue(all("w" in c for c in admin["calls"]))

    def test_tokens(self):
        tok = self.db.create_token("viewer", "t")
        self.assertEqual(self.db.token_kind(tok), "viewer")
        self.assertIsNone(self.db.token_kind("bl_viewer_nope"))
        tid = [t for t in self.db.list_tokens() if t["label"] == "t"][0]["id"]
        self.db.revoke_token(tid)
        self.assertIsNone(self.db.token_kind(tok))

    def test_rejects_missing_email(self):
        with self.assertRaises(ValueError):
            self.db.ingest({"user": {}})


if __name__ == "__main__":
    unittest.main()
