import dataclasses
import os
import tempfile
import unittest
from unittest import mock

from configsync import inventory, report

from .helpers import rec, scan_args


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
        # Classification tables live in the shipped TOML; load it before scanning.
        self.cfg = inventory.load_config(inventory.default_config_path())
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
        inv = inventory.build_inventory(scan_args(**kw), self.home, self.cfg)
        return inv, {e["rel"]: e for e in inv["entries"]}

    def test_symlink_resolution_and_dedup(self):
        _, by_rel = self.scan()
        e = by_rel["dotfiles/nvim"]
        self.assertEqual(e["via_symlink"], [os.path.join(self.home, ".config/nvim")])
        self.assertNotIn(".config/nvim", by_rel)
        self.assertEqual(e["location"], "config")  # from the link's root
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
        self.assertEqual(tmux["location"], "config")
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
        shown = {e["rel"] for e in inv["entries"] if report.is_visible(e, args)}
        self.assertIn(".config/broken", shown)          # dangling always shown
        self.assertNotIn(".ssh", shown)                 # secret hidden
        self.assertNotIn(".config/Code", shown)         # generated hidden
        self.assertNotIn(".config/statedir", shown)
        # ... but all of them are recorded in the inventory
        self.assertIn(".ssh", by_rel)
        self.assertIn(".config/statedir", by_rel)
        shown_secrets = {e["rel"] for e in inv["entries"]
                         if report.is_visible(e, scan_args(secrets=True))}
        self.assertIn(".ssh", shown_secrets)
        orphans = {e["rel"] for e in inv["entries"]
                   if report.is_visible(e, scan_args(only_orphans=True))}
        self.assertEqual(orphans, {".config/polybar"})

    def test_all_entries_share_the_entry_shape(self):
        # analyze and dangling_entry both build through Entry, so every record
        # — including the dangling one — carries exactly the Entry fields.
        _, by_rel = self.scan()
        expected = {f.name for f in dataclasses.fields(inventory.Entry)}
        self.assertIn(".config/broken", by_rel)  # a dangling record is present
        for e in by_rel.values():
            self.assertEqual(set(e.keys()), expected)

    def test_cache_root_needs_all(self):
        inv, by_rel = self.scan()
        self.assertNotIn(".cache/junk", by_rel)
        _, by_rel = self.scan(all=True)
        self.assertEqual(by_rel[".cache/junk"]["location"], "cache")
        self.assertIs(by_rel[".cache/junk"]["editable"], False)


class AdoptableTest(unittest.TestCase):
    CONF = "/h/.config"  # repo_root -> /h/.config/config-sync

    def adoptable(self, **kw):
        return inventory.is_adoptable(rec(**kw), self.CONF)

    def test_plain_editable_config_is_adoptable(self):
        self.assertTrue(self.adoptable())  # rec() default: editable, config, no flags

    def test_secret_is_never_adoptable_even_when_editable(self):
        # secrets carry editable=True (sniffing is skipped), so the flag check
        # is what protects them — the single most important exclusion.
        self.assertFalse(self.adoptable(editable=True, flags=["secret"]))

    def test_non_editable_and_dangling_excluded(self):
        self.assertFalse(self.adoptable(editable=False))
        self.assertFalse(self.adoptable(editable=None, flags=["dangling"]))

    def test_cache_and_state_excluded(self):
        self.assertFalse(self.adoptable(location="cache", editable=True))
        self.assertFalse(self.adoptable(location="state", editable=True))

    def test_the_managed_repo_is_never_adopted(self):
        self.assertFalse(self.adoptable(path="/h/.config/config-sync"))
        self.assertFalse(self.adoptable(path="/h/.config/config-sync/nvim"))
        # a sibling that merely shares the name prefix is still adoptable
        self.assertTrue(self.adoptable(path="/h/.config/config-sync-notes"))


class RepoMappingTest(unittest.TestCase):
    def setUp(self):
        self.cfg = inventory.Config(programs={"neovim": {"paths": [], "bin": "nvim"}})
        self.conf = "/h/.config"
        self.root = "/h/.config/config-sync"

    def test_dir_entry_maps_to_program_dir(self):
        got = inventory.repo_path_for("/h/.config/nvim", "dir", "neovim",
                                      self.cfg, self.conf)
        self.assertEqual(got, os.path.join(self.root, "nvim"))  # bin name, tree copied in

    def test_file_entry_maps_under_program_dir(self):
        got = inventory.repo_path_for("/h/.tmux.conf", "file", "tmux",
                                      self.cfg, self.conf)
        self.assertEqual(got, os.path.join(self.root, "tmux", ".tmux.conf"))

    def test_unattributed_falls_back_to_basename(self):
        got = inventory.repo_path_for("/h/.config/foo", "dir", None,
                                      self.cfg, self.conf)
        self.assertEqual(got, os.path.join(self.root, "foo"))



if __name__ == "__main__":
    unittest.main()
