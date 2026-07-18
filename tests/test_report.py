import json
import unittest

from config_sync import inventory, report

from .helpers import rec, scan_args


class HealthTest(unittest.TestCase):
    def sevs(self, recs):
        return [(s, t) for s, t, _ in report.section_findings(recs)]

    def test_orphan_warns_and_suggests_verification(self):
        findings = report.section_findings([rec(program="polybar", installed=False)])
        sev, text, suggestion = findings[0]
        self.assertEqual(sev, "WARN")
        self.assertIn("not found", text)
        self.assertIn("verify", suggestion)

    def test_git_states(self):
        git = {"root": "/h/dotfiles", "name": "dotfiles", "remotes": [{}],
               "branch": "main", "upstream": "origin/main", "ahead": 2, "behind": 1,
               "default_branch": "main",
               "vs_default": {"ahead": 0, "behind": 0, "is_default": True},
               "dirty": {"modified": 1, "untracked": 0}, "last_commit": None}
        sevs = self.sevs([rec(program="neovim", installed=True,
                              is_git_repo=True, git=git)])
        texts = [t for s, t in sevs if s == "WARN"]
        self.assertTrue(any("uncommitted" in t for t in texts))
        self.assertTrue(any("diverged" in t for t in texts))

    def test_untracked_and_secret(self):
        sevs = self.sevs([rec(program="tmux", installed=True)])
        self.assertIn(("INFO", "not under version control — candidate for a "
                       "dotfiles repo"), sevs)
        sevs = self.sevs([rec(flags=["secret"])])
        self.assertIn("WARN", [s for s, _ in sevs])

    def test_dangling_is_error(self):
        sevs = self.sevs([rec(kind=None, flags=["dangling"])])
        self.assertEqual([s for s, _ in sevs if s == "ERROR"], ["ERROR"])

    def test_multiple_locations_warn(self):
        sevs = self.sevs([rec(program="tmux", installed=True, rel=".tmux.conf"),
                          rec(program="tmux", installed=True, rel=".config/tmux")])
        self.assertTrue(any(s == "WARN" and "several" in t or "paths" in t
                            for s, t in sevs))

    def test_render_health_summary(self):
        inv = {"meta": {"host": "test", "scanned_at": "now"},
               "entries": [rec(program="polybar", installed=False)]}
        out = report.render_health(inv, "inv.json")
        self.assertIn("# config inventory — health check", out)  # Markdown title
        self.assertIn("**1 program checked**", out)              # singular
        self.assertIn("### Needs attention", out)
        self.assertIn("## polybar", out)                         # section header
        self.assertIn("⚠️", out)                                 # orphan is a WARN
        self.assertTrue(out.endswith("\n"))


class ReportersTest(unittest.TestCase):
    def setUp(self):
        self.inv = {"meta": {"host": "h", "scanned_at": "now"},
                    "entries": [rec(program="tmux", rel=".tmux.conf", location="shell")]}
        self.args = scan_args()

    def test_registry_has_the_three_formats(self):
        self.assertEqual(set(report.REPORTERS), {"listing", "json", "health"})

    def test_all_reporters_share_signature_and_return_text(self):
        for fn in report.REPORTERS.values():
            out = fn(self.inv, self.args, inventory.Config())
            self.assertIsInstance(out, str)
            self.assertTrue(out)

    def test_json_reporter_round_trips_the_inventory(self):
        out = report.report_json(self.inv, self.args, inventory.Config())
        self.assertEqual(json.loads(out), self.inv)  # inventory stays plain data

    def test_listing_and_health_content(self):
        self.assertIn("Summary:", report.report_listing(self.inv, self.args))
        health = report.REPORTERS["health"](self.inv, self.args, inventory.Config())
        self.assertIn("# config inventory — health check", health)



if __name__ == "__main__":
    unittest.main()
