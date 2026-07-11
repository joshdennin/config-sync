"""Tests for inventory.py: fixture home tree scan + health as a pure function.

The pacman/git seams (capture, status_counts) are monkeypatched so no real
tools are needed; the filesystem fixture is a synthetic $HOME in a temp dir.
"""

import argparse
import os
import tempfile
import unittest
from unittest import mock

import inventory


def scan_args(**kw):
    defaults = dict(json=False, generated=False, all=False, secrets=False,
                    only_orphans=False, min_relevance=0, root=[])
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def make_fixture(home):
    def write(rel, data=b"# config\nkey = value\n"):
        path = os.path.join(home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    # dotfiles repo with a symlinked nvim config
    os.makedirs(os.path.join(home, "dotfiles/.git"))
    write("dotfiles/nvim/init.lua", b"vim.o.number = true\n")
    os.makedirs(os.path.join(home, ".config"))
    os.symlink(os.path.join(home, "dotfiles/nvim"), os.path.join(home, ".config/nvim"))
    # plain configs
    write(".config/ghostty/config")
    write(".config/polybar/config.ini")
    write(".tmux.conf")
    # noise dir under .config
    write(".config/Code/User/settings.json", b"{}")
    # pure state dir (only generated/binary content)
    write(".config/statedir/app.log", b"log line\n")
    write(".config/statedir/state.db", b"\x00\x01\x02binary")
    # secret dir
    write(".ssh/id_ed25519", b"\x00secret-key-material")
    # dangling symlink
    os.symlink(os.path.join(home, "gone"), os.path.join(home, ".config/broken"))
    # cache root exists but is only scanned with --all
    write(".cache/junk/blob.bin", b"\x00" * 64)


class FakeGit:
    """Canned read-only git/pacman outputs for the dotfiles repo."""

    def __init__(self, top):
        self.top = top

    def capture(self, cmd, cwd=None):
        if cmd[:2] == ["pacman", "-Qq"]:
            return "neovim\nghostty\ntmux"
        if cmd[0] != "git":
            return None
        sub = cmd[3:]
        if sub[:2] == ["rev-parse", "--show-toplevel"]:
            return self.top
        if sub == ["remote", "-v"]:
            return ("origin\tgit@github.com:jd/dotfiles.git (fetch)\n"
                    "origin\tgit@github.com:jd/dotfiles.git (push)")
        if sub == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "main"
        if sub == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return "origin/main"
        if sub == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        if sub[:3] == ["rev-parse", "--verify", "--quiet"]:
            return "abc123"
        if sub[:3] == ["rev-list", "--left-right", "--count"]:
            return "2\t0" if sub[3] == "HEAD...@{u}" else "0\t0"
        if sub[:1] == ["log"]:
            return "a1b2c3d|2026-07-09T21:14:03-05:00"
        return None


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        make_fixture(self.home)
        fake = FakeGit(os.path.join(self.home, "dotfiles"))
        patches = [
            mock.patch.object(inventory, "capture", fake.capture),
            mock.patch.object(inventory, "status_counts",
                              lambda top, cap=500: {"modified": 1, "untracked": 0}),
            mock.patch.object(inventory.shutil, "which", lambda name: None),
            mock.patch.dict(os.environ, {"HOME": self.home}, clear=False),
            mock.patch.dict(inventory._git_cache, clear=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                    "XDG_CACHE_HOME"):
            os.environ.pop(var, None)
        self.addCleanup(self.tmp.cleanup)

    def scan(self, **kw):
        inv = inventory.build_inventory(scan_args(**kw), self.home)
        return inv, {e["rel"]: e for e in inv["entries"]}

    def test_symlink_resolution_and_dedup(self):
        _, by_rel = self.scan()
        e = by_rel["dotfiles/nvim"]
        self.assertEqual(e["via_symlink"], [os.path.join(self.home, ".config/nvim")])
        self.assertNotIn(".config/nvim", by_rel)
        self.assertEqual(e["category"], "config")  # from the link's root
        self.assertEqual(e["kind"], "dir")

    def test_attribution_and_relevance(self):
        _, by_rel = self.scan()
        nvim = by_rel["dotfiles/nvim"]
        self.assertEqual((nvim["program"], nvim["installed"]), ("neovim", True))
        self.assertTrue(nvim["is_git_repo"])
        self.assertEqual(nvim["relevance"], 95)  # 30 + 25 + 25 + 15
        self.assertEqual(nvim["git"]["ahead"], 2)
        self.assertEqual(nvim["git"]["dirty"], {"modified": 1, "untracked": 0})

        tmux = by_rel[".tmux.conf"]
        self.assertEqual(tmux["category"], "config")
        self.assertEqual(tmux["relevance"], 80)  # 30 + 25 + 15 + 10 rc bonus
        self.assertIn("known rc file (.tmux.conf)",
                      [t["label"] for t in tmux["relevance_terms"]])

        polybar = by_rel[".config/polybar"]
        self.assertIs(polybar["installed"], False)
        self.assertEqual(polybar["relevance"], 20)  # 25 + 15 - 20

    def test_flags_and_editable(self):
        _, by_rel = self.scan()
        self.assertEqual(by_rel[".config/Code"]["flags"], ["noise"])
        self.assertIs(by_rel[".config/Code"]["editable"], False)
        self.assertIs(by_rel[".config/statedir"]["editable"], False)
        ssh = by_rel[".ssh"]
        self.assertEqual(ssh["flags"], ["secret"])
        self.assertIs(ssh["editable"], True)  # sniff skipped for secrets
        self.assertEqual(by_rel[".config/broken"]["flags"], ["dangling"])
        self.assertIsNone(by_rel[".config/broken"]["kind"])

    def test_display_filters_shape_listing_not_data(self):
        inv, by_rel = self.scan()
        args = scan_args()
        shown = {e["rel"] for e in inv["entries"] if inventory.is_visible(e, args)}
        self.assertIn(".config/broken", shown)          # dangling always shown
        self.assertNotIn(".ssh", shown)                 # secret hidden
        self.assertNotIn(".config/Code", shown)         # generated hidden
        self.assertNotIn(".config/statedir", shown)
        # ... but all of them are recorded in the inventory
        self.assertIn(".ssh", by_rel)
        self.assertIn(".config/statedir", by_rel)
        shown_secrets = {e["rel"] for e in inv["entries"]
                         if inventory.is_visible(e, scan_args(secrets=True))}
        self.assertIn(".ssh", shown_secrets)
        orphans = {e["rel"] for e in inv["entries"]
                   if inventory.is_visible(e, scan_args(only_orphans=True))}
        self.assertEqual(orphans, {".config/polybar"})

    def test_cache_root_needs_all(self):
        inv, by_rel = self.scan()
        self.assertNotIn(".cache/junk", by_rel)
        _, by_rel = self.scan(all=True)
        self.assertEqual(by_rel[".cache/junk"]["category"], "cache")
        self.assertIs(by_rel[".cache/junk"]["editable"], False)


def rec(**kw):
    base = {"path": "/h/.config/x", "rel": ".config/x", "category": "config",
            "kind": "dir", "via_symlink": None, "size": 1, "mtime": None,
            "editable": True, "is_git_repo": False, "git": None,
            "program": None, "installed": None, "flags": [],
            "relevance": 50, "relevance_terms": []}
    base.update(kw)
    return base


class HealthTest(unittest.TestCase):
    def sevs(self, recs):
        return [(s, t) for s, t, _ in inventory.section_findings(recs)]

    def test_orphan_warns_and_suggests_verification(self):
        findings = inventory.section_findings([rec(program="polybar", installed=False)])
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
        out = inventory.render_health(inv, "inv.json")
        self.assertIn("1 programs", out)
        self.assertIn("Needs attention:", out)
        self.assertIn("polybar", out)


if __name__ == "__main__":
    unittest.main()
