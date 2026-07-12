"""Tests for inventory.py: fixture home tree scan + health as a pure function.

The pacman/git seams (capture, status_counts) are monkeypatched so no real
tools are needed; the filesystem fixture is a synthetic $HOME in a temp dir.
"""

import argparse
import contextlib
import dataclasses
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import inventory


def scan_args(**kw):
    defaults = dict(json=False, generated=False, all=False, secrets=False,
                    only_orphans=False, min_relevance=0, root=[], config=None)
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


def rec(**kw):
    base = {"path": "/h/.config/x", "rel": ".config/x", "location": "config",
            "kind": "dir", "via_symlink": None, "size": 1, "mtime": None,
            "editable": True, "is_git_repo": False, "git": None,
            "program": None, "category": None, "installed": None, "flags": [],
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
        self.assertEqual(set(inventory.REPORTERS), {"listing", "json", "health"})

    def test_all_reporters_share_signature_and_return_text(self):
        for fn in inventory.REPORTERS.values():
            out = fn(self.inv, self.args, inventory.Config())
            self.assertIsInstance(out, str)
            self.assertTrue(out)

    def test_json_reporter_round_trips_the_inventory(self):
        out = inventory.report_json(self.inv, self.args, inventory.Config())
        self.assertEqual(json.loads(out), self.inv)  # inventory stays plain data

    def test_listing_and_health_content(self):
        self.assertIn("Summary:", inventory.report_listing(self.inv, self.args))
        health = inventory.REPORTERS["health"](self.inv, self.args, inventory.Config())
        self.assertIn("# config inventory — health check", health)


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


class AdoptCandidatesTest(unittest.TestCase):
    CONF = "/h/.config"

    def setUp(self):
        self.inv = {"meta": {}, "entries": [
            rec(program="neovim", category="editor", relevance=95),
            rec(program="tmux", category="terminal", relevance=80),
            rec(program="polybar", category="desktop", relevance=20, installed=False),
            rec(program="foo", category="misc", relevance=5),
            rec(program="dots", relevance=90, is_git_repo=True),   # already versioned
            rec(program="ssh", relevance=60, flags=["secret"]),    # unsafe
        ]}

    def progs(self, tier, include=(), exclude=()):
        cands = inventory.adopt_candidates(self.inv, tier, list(include),
                                           list(exclude), self.CONF)
        return {c["program"] for c in cands}

    def test_curated_is_strong_signal_only(self):
        self.assertEqual(self.progs("curated"), {"neovim", "tmux"})

    def test_extended_adds_weaker_signals(self):
        self.assertEqual(self.progs("extended"), {"neovim", "tmux", "polybar"})

    def test_everything_includes_low_signal(self):
        self.assertEqual(self.progs("everything"),
                         {"neovim", "tmux", "polybar", "foo"})

    def test_git_repos_and_secrets_never_included(self):
        broadest = self.progs("everything")
        self.assertNotIn("dots", broadest)  # already under version control
        self.assertNotIn("ssh", broadest)   # secret (safety gate)

    def test_include_and_exclude_match_program_or_category(self):
        self.assertEqual(self.progs("extended", include=["editor"]), {"neovim"})
        self.assertEqual(self.progs("curated", exclude=["tmux"]), {"neovim"})


class AdoptPlanIOTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "plan.toml")

    def test_write_then_load_round_trip(self):
        rows = [inventory.adopt_plan_row(rec(program="tmux", rel=".tmux.conf",
                                             kind="file", relevance=80))]
        inventory.write_adopt_plan(self.path, rows, "curated")
        data = inventory.load_adopt_plan(self.path)
        self.assertEqual(data["tier"], "curated")
        self.assertEqual(data["entries"][0]["program"], "tmux")
        self.assertEqual(data["entries"][0]["path"], "~/.tmux.conf")
        self.assertTrue(data["entries"][0]["adopt"])

    def test_header_comment_guides_editing(self):
        inventory.write_adopt_plan(self.path, [], "everything")
        with open(self.path) as f:
            text = f.read()
        self.assertIn("# config-sync adopt plan", text)
        self.assertIn("adopt = false", text)  # the edit instruction

    def test_missing_plan_is_hard_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            inventory.load_adopt_plan(os.path.join(self.tmp.name, "nope.toml"))


class AdoptApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.realpath(self.tmp.name)
        self.conf = os.path.join(self.home, ".config")
        os.makedirs(os.path.join(self.home, ".config/ghostty"))
        with open(os.path.join(self.home, ".config/ghostty/config"), "w") as f:
            f.write("theme=dark\n")
        with open(os.path.join(self.home, ".tmux.conf"), "w") as f:
            f.write("set -g mouse on\n")
        self.cfg = inventory.load_config(inventory.default_config_path())
        p = mock.patch.object(inventory, "git_init_commit", lambda repo, msg: True)
        p.start()
        self.addCleanup(p.stop)

    def plan(self, *rows):
        return {"version": 1, "tier": "everything", "entries": list(rows)}

    def row(self, path, kind, program, adopt=True):
        return {"program": program, "path": path, "kind": kind,
                "category": "", "relevance": 50, "adopt": adopt}

    def repo(self, *parts):
        return os.path.join(self.conf, "config-sync", *parts)

    def test_apply_copies_true_entries_preserving_structure(self):
        result = inventory.adopt_apply(self.plan(
            self.row("~/.config/ghostty", "dir", "ghostty"),
            self.row("~/.tmux.conf", "file", "tmux"),
        ), self.home, self.conf, self.cfg)
        self.assertTrue(os.path.isfile(self.repo("ghostty/config")))  # dir tree preserved
        self.assertTrue(os.path.isfile(self.repo("tmux/.tmux.conf")))
        # originals preserved (copy, not move) — reversible by construction
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".config/ghostty/config")))
        self.assertEqual(set(result["copied"]), {"~/.config/ghostty", "~/.tmux.conf"})
        self.assertEqual(len(inventory.load_manifest(self.conf)["entries"]), 2)

    def test_adopt_false_entries_are_skipped(self):
        result = inventory.adopt_apply(
            self.plan(self.row("~/.tmux.conf", "file", "tmux", adopt=False)),
            self.home, self.conf, self.cfg)
        self.assertEqual(result["copied"], [])
        self.assertFalse(os.path.lexists(self.repo("tmux")))  # repo untouched

    def test_reapply_is_idempotent(self):
        plan = self.plan(self.row("~/.tmux.conf", "file", "tmux"))
        inventory.adopt_apply(plan, self.home, self.conf, self.cfg)
        again = inventory.adopt_apply(plan, self.home, self.conf, self.cfg)
        self.assertEqual(again["copied"], [])
        self.assertEqual(again["skipped"], ["~/.tmux.conf"])  # already adopted


class FsopsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def p(self, *rel):
        return os.path.join(self.root, *rel)

    def write(self, rel, data=b"data"):
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_safe_copy_file_creates_parents_and_refuses_overwrite(self):
        src = self.write("src.txt", b"hello")
        dst = self.p("nested/dir/out.txt")  # parents do not exist yet
        inventory.safe_copy(src, dst)
        self.assertTrue(os.path.isfile(dst))
        self.assertTrue(os.path.isfile(src))  # copy, not move
        with self.assertRaises(inventory.FsError):
            inventory.safe_copy(src, dst)  # dst now exists

    def test_safe_copy_tree_preserves_structure(self):
        self.write("tree/a.txt", b"a")
        self.write("tree/sub/b.txt", b"b")
        inventory.safe_copy(self.p("tree"), self.p("copy"))
        self.assertTrue(os.path.isfile(self.p("copy/a.txt")))
        self.assertTrue(os.path.isfile(self.p("copy/sub/b.txt")))

    def test_safe_move_refuses_overwrite(self):
        src = self.write("m-src.txt")
        dst = self.write("m-dst.txt")
        with self.assertRaises(inventory.FsError):
            inventory.safe_move(src, dst)
        self.assertTrue(os.path.isfile(src))  # untouched on refusal

    def test_symlink_create_and_remove_guards(self):
        target = self.write("target.txt")
        link = self.p("link")
        inventory.safe_symlink(target, link)
        self.assertTrue(os.path.islink(link))
        with self.assertRaises(inventory.FsError):
            inventory.safe_symlink(target, link)  # exists
        inventory.remove_symlink(link)
        self.assertFalse(os.path.lexists(link))
        with self.assertRaises(inventory.FsError):
            inventory.remove_symlink(target)  # never remove a real file
        self.assertTrue(os.path.isfile(target))

    def test_backup_restore_round_trip(self):
        home = self.p("home")
        orig = self.write("home/.config/app/config", b"cfg")
        backups = self.p("backups")
        bpath = inventory.backup(orig, backups, home)
        self.assertFalse(os.path.lexists(orig))          # moved aside
        self.assertTrue(os.path.isfile(bpath))
        self.assertTrue(bpath.startswith(backups + os.sep))
        inventory.restore(bpath, orig)
        self.assertTrue(os.path.isfile(orig))            # back in place
        self.assertFalse(os.path.lexists(bpath))

    def test_backup_refuses_path_outside_home(self):
        home = self.p("home")
        os.makedirs(home)
        outside = self.write("elsewhere.txt")
        with self.assertRaises(inventory.FsError):
            inventory.backup(outside, self.p("backups"), home)


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


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_manifest_reads_as_empty(self):
        m = inventory.load_manifest(self.conf)
        self.assertEqual(m, {"version": inventory.MANIFEST_VERSION, "entries": []})

    def test_save_then_load_round_trip(self):
        m = inventory.empty_manifest()
        m["entries"].append(inventory.manifest_entry(
            "neovim", "/h/.config/nvim",
            os.path.join(inventory.repo_root(self.conf), "nvim"), "dir"))
        path = inventory.save_manifest(self.conf, m)
        self.assertTrue(path.endswith("config-sync/manifest.toml"))
        self.assertEqual(inventory.load_manifest(self.conf), m)

    def test_manifest_entry_coerces_missing_program_to_empty(self):
        e = inventory.manifest_entry(None, "/h/.config/foo", "/r/foo", "dir")
        self.assertEqual(e["program"], "")     # TOML has no null
        self.assertEqual(e["backup_path"], "")

    def test_invalid_manifest_is_hard_error(self):
        path = inventory.manifest_path(self.conf)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('entries = "nope"\n')  # valid TOML, wrong shape (not a list)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            inventory.load_manifest(self.conf)


class TidyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patches = [mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)  # so config_home is ~/.config

        def touch(rel, data=b"x"):
            path = os.path.join(self.home, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

        touch(".gitconfig")                          # movable: no target yet
        touch(".gitignore_global")                   # merge: source + target both present
        touch(".config/git/ignore")
        os.symlink(os.path.join(self.home, "elsewhere"),
                   os.path.join(self.home, ".tmux.conf"))  # symlink source

    def survey(self):
        rows = inventory.tidy_survey(self.home, inventory.config_home(self.home))
        return {src_rel: status for _, src_rel, _, _, _, status in rows}

    def test_survey_classifies_statuses(self):
        s = self.survey()
        self.assertEqual(s[".gitconfig"], "movable")
        self.assertEqual(s[".gitignore_global"], "merge")
        self.assertEqual(s[".tmux.conf"], "symlink")

    def test_absent_source_yields_no_row(self):
        # .gitconfig is present; a program file that is neither present nor
        # already at its target simply produces no row. Remove the movable one
        # and confirm it drops out of the survey.
        os.remove(os.path.join(self.home, ".gitconfig"))
        self.assertNotIn(".gitconfig", self.survey())

    def test_move_relocates_only_movable(self):
        rows = inventory.tidy_survey(self.home, inventory.config_home(self.home))
        with contextlib.redirect_stdout(io.StringIO()):
            inventory.tidy_move(rows)
        # movable relocated: source gone, target now present
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".gitconfig")))
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config/git/config")))
        # merge and symlink left untouched
        self.assertTrue(os.path.exists(os.path.join(self.home, ".gitignore_global")))
        self.assertTrue(os.path.islink(os.path.join(self.home, ".tmux.conf")))
        # a re-survey now reports the relocated file as done
        self.assertEqual(self.survey().get(".gitconfig"), "done")


if __name__ == "__main__":
    unittest.main()
