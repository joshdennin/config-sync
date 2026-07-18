import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from config_sync import inventory, sync

from .helpers import rec, scan_args


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
        cands = sync.adopt_candidates(self.inv, tier, list(include),
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
        cfg = inventory.Config(programs={"tmux": {"paths": [".tmux.conf"], "bin": "tmux"}})
        rows = sync.adopt_plan_rows(
            [rec(program="tmux", category="Terminal multiplexers",
                 path="/h/.tmux.conf", rel=".tmux.conf", kind="file")],
            cfg, "/h/.config", "/h")
        sync.write_adopt_plan(self.path, rows, "curated")
        data = sync.load_adopt_plan(self.path)
        self.assertEqual(data["tier"], "curated")
        e = data["entries"][0]
        self.assertEqual(e["program"], "tmux")
        self.assertEqual(e["paths"], ["~/.tmux.conf"])
        self.assertEqual(e["repo_dir"], "~/.config/config-sync/tmux")
        self.assertTrue(e["adopt"])

    def test_rows_group_by_program_and_order_by_category(self):
        # One entry per program; category order follows first appearance (health's
        # order), so Editors precedes Shells even though bash sorts before nvim.
        cfg = inventory.Config(programs={
            "bash": {"paths": [".bashrc", ".bash_profile"], "bin": "bash"},
            "nvim": {"paths": ["nvim"], "bin": "nvim"}})
        cands = [
            rec(program="nvim", category="Editors", path="/h/.config/nvim",
                rel=".config/nvim", kind="dir"),
            rec(program="bash", category="Shells", path="/h/.bashrc",
                rel=".bashrc", kind="file"),
            rec(program="bash", category="Shells", path="/h/.bash_profile",
                rel=".bash_profile", kind="file"),
        ]
        rows = sync.adopt_plan_rows(cands, cfg, "/h/.config", "/h")
        self.assertEqual([r["program"] for r in rows], ["nvim", "bash"])
        bash = next(r for r in rows if r["program"] == "bash")
        self.assertEqual(bash["paths"], ["~/.bash_profile", "~/.bashrc"])  # grouped, sorted
        self.assertEqual(bash["repo_dir"], "~/.config/config-sync/bash")

    def test_header_comment_guides_editing(self):
        sync.write_adopt_plan(self.path, [], "everything")
        with open(self.path) as f:
            text = f.read()
        self.assertIn("# config-sync adopt plan", text)
        self.assertIn("adopt = false", text)  # the edit instruction

    def test_missing_plan_is_hard_error(self):
        with self.assertRaises(inventory.ConfigSyncError):
            sync.load_adopt_plan(os.path.join(self.tmp.name, "nope.toml"))


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
        p = mock.patch.object(sync, "git_init_commit", lambda repo, msg: True)
        p.start()
        self.addCleanup(p.stop)

    def plan(self, *entries):
        return {"version": 1, "tier": "everything", "entries": list(entries)}

    def entry(self, program, *paths, adopt=True):
        # kind is derived from the filesystem at apply time, so the plan carries
        # only the home paths grouped under their program.
        return {"program": program, "category": "", "repo_dir": "",
                "paths": list(paths), "adopt": adopt}

    def repo(self, *parts):
        return os.path.join(self.conf, "config-sync", *parts)

    def test_apply_copies_true_entries_preserving_structure(self):
        result = sync.adopt_apply(self.plan(
            self.entry("ghostty", "~/.config/ghostty"),
            self.entry("tmux", "~/.tmux.conf"),
        ), self.home, self.conf, self.cfg)
        self.assertTrue(os.path.isfile(self.repo("ghostty/config")))  # dir tree preserved
        self.assertTrue(os.path.isfile(self.repo("tmux/.tmux.conf")))
        # originals preserved (copy, not move) — reversible by construction
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".config/ghostty/config")))
        self.assertEqual(set(result["copied"]), {"~/.config/ghostty", "~/.tmux.conf"})
        self.assertEqual(len(sync.load_manifest(self.conf)["entries"]), 2)

    def test_adopt_false_entries_are_skipped(self):
        result = sync.adopt_apply(
            self.plan(self.entry("tmux", "~/.tmux.conf", adopt=False)),
            self.home, self.conf, self.cfg)
        self.assertEqual(result["copied"], [])
        self.assertFalse(os.path.lexists(self.repo("tmux")))  # repo untouched

    def test_reapply_is_idempotent(self):
        plan = self.plan(self.entry("tmux", "~/.tmux.conf"))
        sync.adopt_apply(plan, self.home, self.conf, self.cfg)
        again = sync.adopt_apply(plan, self.home, self.conf, self.cfg)
        self.assertEqual(again["copied"], [])
        self.assertEqual(again["skipped"], ["~/.tmux.conf"])  # already adopted


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.realpath(self.tmp.name)
        self.conf = os.path.join(self.home, ".config")
        self.repo = os.path.join(self.conf, "config-sync")
        # simulate a completed adopt: repo content + original still in place
        for base in (os.path.join(self.repo, "ghostty"), os.path.join(self.conf, "ghostty")):
            os.makedirs(base)
            with open(os.path.join(base, "config"), "w") as f:
                f.write("theme=dark\n")
        m = sync.empty_manifest()
        m["entries"].append(sync.manifest_entry(
            "ghostty", os.path.join(self.conf, "ghostty"),
            os.path.join(self.repo, "ghostty"), "dir"))
        sync.save_manifest(self.conf, m)

    def apply(self):
        return sync.link_apply(sync.load_manifest(self.conf), self.home, self.conf)

    def entry(self):
        return sync.load_manifest(self.conf)["entries"][0]

    def test_real_original_is_a_link_candidate(self):
        self.assertEqual(sync.link_status(self.entry()), "link")

    def test_apply_backs_up_then_symlinks_and_records_state(self):
        ghostty = os.path.join(self.conf, "ghostty")
        result = self.apply()
        self.assertTrue(os.path.islink(ghostty))                       # replaced by a symlink
        self.assertEqual(os.path.realpath(ghostty),
                         os.path.join(self.repo, "ghostty"))           # ...into the repo
        self.assertTrue(os.path.isfile(                                # original backed up
            os.path.join(self.repo, ".backups/.config/ghostty/config")))
        e = self.entry()
        self.assertTrue(e["linked"])
        self.assertTrue(e["backup_path"])
        self.assertEqual(result["linked"], [ghostty])

    def test_backups_are_git_ignored(self):
        self.apply()
        with open(os.path.join(self.repo, ".gitignore")) as f:
            self.assertIn(".backups/", f.read())

    def test_reapply_is_idempotent(self):
        self.apply()
        again = self.apply()
        self.assertEqual(again["linked"], [])
        self.assertEqual(again["skipped"], [(os.path.join(self.conf, "ghostty"), "done")])

    def test_status_variants(self):
        # repo content missing
        gone_repo = sync.manifest_entry(
            "x", os.path.join(self.conf, "ghostty"), os.path.join(self.repo, "nope"), "dir")
        self.assertEqual(sync.link_status(gone_repo), "no-source")
        # home is a symlink pointing somewhere other than the repo
        other = os.path.join(self.home, "other")
        os.makedirs(other)
        weird = os.path.join(self.conf, "weird")
        os.symlink(other, weird)
        conflict = sync.manifest_entry(
            "x", weird, os.path.join(self.repo, "ghostty"), "dir")
        self.assertEqual(sync.link_status(conflict), "conflict")
        # original gone, repo content present -> symlink with no backup
        missing = sync.manifest_entry(
            "x", os.path.join(self.conf, "gone"), os.path.join(self.repo, "ghostty"), "dir")
        self.assertEqual(sync.link_status(missing), "link-missing")

    def test_failed_symlink_rolls_back_the_backup(self):
        # If the original is moved aside but the symlink step fails, the original
        # must be restored — never left orphaned in the backups tree.
        ghostty = os.path.join(self.conf, "ghostty")
        with mock.patch.object(sync, "safe_symlink",
                               side_effect=OSError("disk full")):
            result = self.apply()
        self.assertFalse(os.path.islink(ghostty))          # no symlink created
        self.assertTrue(os.path.isdir(ghostty))            # original restored in place
        self.assertTrue(os.path.isfile(os.path.join(ghostty, "config")))
        self.assertFalse(os.path.exists(                   # nothing orphaned in backups
            os.path.join(self.repo, ".backups/.config/ghostty")))
        self.assertEqual(result["linked"], [])
        e = self.entry()
        self.assertFalse(e["linked"])
        self.assertEqual(e["backup_path"], "")


class UnlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.realpath(self.tmp.name)
        self.conf = os.path.join(self.home, ".config")
        self.repo = os.path.join(self.conf, "config-sync")
        for base in (os.path.join(self.repo, "ghostty"), os.path.join(self.conf, "ghostty")):
            os.makedirs(base)
            with open(os.path.join(base, "config"), "w") as f:
                f.write("theme=dark\n")
        m = sync.empty_manifest()
        m["entries"].append(sync.manifest_entry(
            "ghostty", os.path.join(self.conf, "ghostty"),
            os.path.join(self.repo, "ghostty"), "dir"))
        sync.save_manifest(self.conf, m)
        sync.link_apply(sync.load_manifest(self.conf), self.home, self.conf)

    def unapply(self):
        return sync.unlink_apply(sync.load_manifest(self.conf), self.home, self.conf)

    def entry(self):
        return sync.load_manifest(self.conf)["entries"][0]

    def test_linked_entry_is_a_restore_candidate(self):
        self.assertEqual(sync.unlink_status(self.entry()), "restore")

    def test_apply_restores_original_and_clears_state(self):
        ghostty = os.path.join(self.conf, "ghostty")
        result = self.unapply()
        self.assertFalse(os.path.islink(ghostty))                         # symlink gone
        self.assertTrue(os.path.isfile(os.path.join(ghostty, "config")))  # original back
        self.assertFalse(os.path.exists(                                  # backup consumed
            os.path.join(self.repo, ".backups/.config/ghostty/config")))
        self.assertTrue(os.path.isfile(                                   # repo copy intact
            os.path.join(self.repo, "ghostty/config")))
        e = self.entry()
        self.assertFalse(e["linked"])
        self.assertEqual(e["backup_path"], "")
        self.assertEqual(result["restored"], [ghostty])

    def test_reapply_is_idempotent(self):
        self.unapply()
        again = self.unapply()
        self.assertEqual(again["restored"], [])
        self.assertEqual(again["skipped"],
                         [(os.path.join(self.conf, "ghostty"), "not-linked")])

    def test_changed_home_is_left_alone(self):
        # user replaced the symlink with a real dir after linking -> never touch it
        ghostty = os.path.join(self.conf, "ghostty")
        os.unlink(ghostty)
        os.makedirs(ghostty)
        self.assertEqual(sync.unlink_status(self.entry()), "changed")
        result = self.unapply()
        self.assertEqual(result["restored"], [])
        self.assertTrue(os.path.isdir(ghostty) and not os.path.islink(ghostty))

    def test_unlink_only_when_no_backup(self):
        entry = sync.manifest_entry(
            "x", os.path.join(self.conf, "ghostty"),
            os.path.join(self.repo, "ghostty"), "dir")
        entry["linked"] = True  # linked, but backup_path stays ""
        self.assertEqual(sync.unlink_status(entry), "unlink-only")


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_manifest_reads_as_empty(self):
        m = sync.load_manifest(self.conf)
        self.assertEqual(m, {"version": sync.MANIFEST_VERSION, "entries": []})

    def test_save_then_load_round_trip(self):
        m = sync.empty_manifest()
        m["entries"].append(sync.manifest_entry(
            "neovim", "/h/.config/nvim",
            os.path.join(inventory.repo_root(self.conf), "nvim"), "dir"))
        path = sync.save_manifest(self.conf, m)
        self.assertTrue(path.endswith("config-sync/manifest.toml"))
        self.assertEqual(sync.load_manifest(self.conf), m)

    def test_manifest_entry_coerces_missing_program_to_empty(self):
        e = sync.manifest_entry(None, "/h/.config/foo", "/r/foo", "dir")
        self.assertEqual(e["program"], "")     # TOML has no null
        self.assertEqual(e["backup_path"], "")

    def test_invalid_manifest_is_hard_error(self):
        path = sync.manifest_path(self.conf)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('entries = "nope"\n')  # valid TOML, wrong shape (not a list)
        with self.assertRaises(inventory.ConfigSyncError):
            sync.load_manifest(self.conf)


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
        rows = sync.tidy_survey(self.home, inventory.config_home(self.home))
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
        rows = sync.tidy_survey(self.home, inventory.config_home(self.home))
        with contextlib.redirect_stdout(io.StringIO()):
            sync.tidy_move(rows)
        # movable relocated: source gone, target now present
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".gitconfig")))
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config/git/config")))
        # merge and symlink left untouched
        self.assertTrue(os.path.exists(os.path.join(self.home, ".gitignore_global")))

if __name__ == "__main__":
    unittest.main()
