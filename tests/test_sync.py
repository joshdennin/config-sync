import contextlib
import io
import os
import shutil
import tempfile
import tomllib
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
            rec(program="dots", relevance=10, is_git_repo=True),   # versioned, low score
            rec(program="ssh", relevance=60, flags=["secret"]),    # unsafe
        ]}

    def progs(self, tier, include=(), exclude=()):
        cands = sync.adopt_candidates(self.inv, tier, list(include),
                                           list(exclude), self.CONF)
        return {c["program"] for c in cands}

    def test_curated_is_strong_signal_only(self):
        # dots scores below every floor but is versioned, so it surfaces anyway.
        self.assertEqual(self.progs("curated"), {"neovim", "tmux", "dots"})

    def test_extended_adds_weaker_signals(self):
        self.assertEqual(self.progs("extended"),
                         {"neovim", "tmux", "polybar", "dots"})

    def test_everything_includes_low_signal(self):
        self.assertEqual(self.progs("everything"),
                         {"neovim", "tmux", "polybar", "foo", "dots"})

    def test_git_repo_bypasses_the_relevance_floor(self):
        # dots (relevance 10) is below the curated floor of 50 yet still included.
        self.assertIn("dots", self.progs("curated"))

    def test_secrets_are_never_included(self):
        self.assertNotIn("ssh", self.progs("everything"))  # secret (safety gate)

    def test_include_and_exclude_match_program_or_category(self):
        self.assertEqual(self.progs("extended", include=["editor"]), {"neovim"})
        self.assertEqual(self.progs("curated", exclude=["tmux"]), {"neovim", "dots"})


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
            cfg, "/h/.config")
        sync.write_adopt_plan(self.path, rows, "curated", "~/.config/config-sync")
        data = sync.load_adopt_plan(self.path)
        self.assertEqual(data["tier"], "curated")
        self.assertEqual(data["repo"], "~/.config/config-sync")  # repo root, once
        e = data["entries"][0]
        self.assertEqual(e["program"], "tmux")
        self.assertEqual(e["paths"], [{"home": "~/.tmux.conf",
                                       "repo": "tmux/.tmux.conf"}])  # relative to repo
        self.assertTrue(e["adopt"])
        self.assertFalse(e["managed"])  # a plain (non-versioned) config

    def test_versioned_config_is_managed_and_not_adopted_by_default(self):
        cfg = inventory.Config(programs={"neovim": {"paths": ["nvim"], "bin": "nvim"}})
        rows = sync.adopt_plan_rows(
            [rec(program="neovim", category="Editors", path="/h/.config/nvim",
                 rel=".config/nvim", kind="dir", is_git_repo=True)],
            cfg, "/h/.config")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["managed"])   # flagged as already versioned
        self.assertFalse(rows[0]["adopt"])    # opt-in, not adopted by default

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
        rows = sync.adopt_plan_rows(cands, cfg, "/h/.config")
        self.assertEqual([r["program"] for r in rows], ["nvim", "bash"])
        bash = next(r for r in rows if r["program"] == "bash")
        self.assertEqual(bash["paths"], [  # grouped, sorted, repo path relative to root
            {"home": "~/.bash_profile", "repo": "bash/.bash_profile"},
            {"home": "~/.bashrc", "repo": "bash/.bashrc"}])

    def test_header_comment_guides_editing(self):
        sync.write_adopt_plan(self.path, [], "everything", "~/.config/config-sync")
        with open(self.path) as f:
            text = f.read()
        self.assertIn("# config-sync adopt plan", text)
        self.assertIn("adopt = false", text)  # the edit instruction

    def test_omitted_programs_excludes_included_and_secrets(self):
        inv = {"entries": [
            rec(program="tmux"),                    # in the plan
            rec(program="polybar"),                 # discovered, left out
            rec(program="ssh", flags=["secret"]),   # secret — never listed
            rec(program=None),                      # unattributed — no name
        ]}
        rows = [{"program": "tmux"}]
        self.assertEqual(sync.omitted_programs(inv, rows), ["polybar"])

    def test_omitted_programs_named_in_plan_comment(self):
        inv = {"entries": [rec(program="polybar"), rec(program="Discord")]}
        sync.write_adopt_plan(self.path, [], "curated", "~/.config/config-sync",
                              sync.omitted_programs(inv, []))
        with open(self.path) as f:
            text = f.read()
        self.assertIn("Not in this", text)
        self.assertIn("Discord, polybar", text)  # sorted case-insensitively

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

    def entry(self, program, *homes, adopt=True):
        # kind is derived from the filesystem at apply time; repo is display-only
        # (recomputed on apply), so the tests leave it blank.
        return {"program": program, "category": "", "adopt": adopt,
                "paths": [{"home": h, "repo": ""} for h in homes]}

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
        self.assertEqual(len(sync.load_manifest(self.conf, self.home)["entries"]), 2)

    def test_apply_strips_git_venv_and_pycache_from_dir_copies(self):
        base = os.path.join(self.home, ".config/ghostty")
        os.makedirs(os.path.join(base, ".git"))
        os.makedirs(os.path.join(base, ".venv/bin"))
        os.makedirs(os.path.join(base, "themes/__pycache__"))  # nested cache
        for rel in (".git/HEAD", ".gitignore", ".venv/bin/activate",
                    "themes/__pycache__/x.pyc", "themes/dark.conf"):
            with open(os.path.join(base, rel), "w") as f:
                f.write("x\n")
        sync.adopt_apply(self.plan(self.entry("ghostty", "~/.config/ghostty")),
                         self.home, self.conf, self.cfg)
        self.assertTrue(os.path.isfile(self.repo("ghostty/config")))       # real config kept
        self.assertTrue(os.path.isfile(self.repo("ghostty/themes/dark.conf")))
        for stripped in (".git", ".gitignore", ".venv", "themes/__pycache__"):
            self.assertFalse(os.path.exists(self.repo("ghostty", stripped)),
                             f"{stripped} should not be copied into the repo")

    def test_adopt_false_entries_are_skipped(self):
        result = sync.adopt_apply(
            self.plan(self.entry("tmux", "~/.tmux.conf", adopt=False)),
            self.home, self.conf, self.cfg)
        self.assertEqual(result["copied"], [])
        self.assertFalse(os.path.lexists(self.repo("tmux")))  # repo untouched

    def test_reapply_is_idempotent(self):
        plan = self.plan(self.entry("tmux", "~/.tmux.conf"))
        sync.adopt_apply(plan, self.home, self.conf, self.cfg)
        # Re-adopting into the now-populated repo needs --force (guard); the copy
        # itself is still idempotent — the already-adopted entry is skipped.
        again = sync.adopt_apply(plan, self.home, self.conf, self.cfg, force=True)
        self.assertEqual(again["copied"], [])
        self.assertEqual(again["skipped"], ["~/.tmux.conf"])  # already adopted

    def test_apply_refuses_populated_repo_without_force(self):
        plan = self.plan(self.entry("tmux", "~/.tmux.conf"))
        sync.adopt_apply(plan, self.home, self.conf, self.cfg)  # first fills the repo
        with self.assertRaises(inventory.ConfigSyncError):
            sync.adopt_apply(self.plan(self.entry("ghostty", "~/.config/ghostty")),
                             self.home, self.conf, self.cfg)  # no force -> refused

    def test_bookkeeping_files_do_not_trip_the_guard(self):
        # A repo holding only the captured config/plan (plan phase) is not "populated".
        os.makedirs(self.repo_dir())
        for name in ("inventory-config.toml", "config-sync-adopt.toml", ".gitignore"):
            with open(os.path.join(self.repo_dir(), name), "w") as f:
                f.write("x\n")
        self.assertFalse(sync.repo_has_adopted_content(self.repo_dir()))
        result = sync.adopt_apply(self.plan(self.entry("tmux", "~/.tmux.conf")),
                                  self.home, self.conf, self.cfg)  # proceeds
        self.assertEqual(result["copied"], ["~/.tmux.conf"])

    def repo_dir(self):
        return os.path.join(self.conf, "config-sync")


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
        sync.save_manifest(self.conf, m, self.home)

    def apply(self):
        return sync.link_apply(sync.load_manifest(self.conf, self.home), self.home, self.conf)

    def entry(self):
        return sync.load_manifest(self.conf, self.home)["entries"][0]

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

    def test_backups_and_link_state_are_git_ignored(self):
        self.apply()
        with open(os.path.join(self.repo, ".gitignore")) as f:
            gitignore = f.read()
        self.assertIn(".backups/", gitignore)
        self.assertIn(".link-state.toml", gitignore)  # machine-local, never committed

    def test_link_writes_state_not_the_manifest(self):
        before = open(sync.manifest_path(self.conf), "rb").read()
        self.apply()
        after = open(sync.manifest_path(self.conf), "rb").read()
        self.assertEqual(before, after)  # linking never rewrites the shared manifest
        self.assertTrue(os.path.isfile(sync.link_state_path(self.conf)))

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
        sync.save_manifest(self.conf, m, self.home)
        sync.link_apply(sync.load_manifest(self.conf, self.home), self.home, self.conf)

    def unapply(self):
        return sync.unlink_apply(sync.load_manifest(self.conf, self.home), self.home, self.conf)

    def entry(self):
        return sync.load_manifest(self.conf, self.home)["entries"][0]

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
        self.home = "/h"  # manifest stores paths portable; resolved against home
        self.addCleanup(self.tmp.cleanup)

    def test_missing_manifest_reads_as_empty(self):
        m = sync.load_manifest(self.conf, self.home)
        self.assertEqual(m, {"version": sync.MANIFEST_VERSION, "entries": []})

    def test_save_then_load_round_trip(self):
        m = sync.empty_manifest()
        m["entries"].append(sync.manifest_entry(
            "neovim", "/h/.config/nvim",
            os.path.join(inventory.repo_root(self.conf), "nvim"), "dir"))
        path = sync.save_manifest(self.conf, m, self.home)
        self.assertTrue(path.endswith("config-sync/manifest.toml"))
        self.assertEqual(sync.load_manifest(self.conf, self.home), m)

    def test_manifest_is_stored_portable_and_mapping_only(self):
        # On disk the paths are machine-independent (home-root files ~/-relative,
        # repo paths repo-relative) and carry no machine-local link state.
        m = sync.empty_manifest()
        m["entries"].append(sync.manifest_entry(
            "git", "/h/.gitconfig",
            os.path.join(inventory.repo_root(self.conf), "git/.gitconfig"), "file"))
        sync.save_manifest(self.conf, m, self.home)
        with open(sync.manifest_path(self.conf), "rb") as f:
            row = tomllib.load(f)["entries"][0]
        self.assertEqual(row["home_path"], "~/.gitconfig")
        self.assertEqual(row["repo_path"], "git/.gitconfig")  # relative to repo root
        self.assertNotIn("linked", row)       # link state is not in the manifest
        self.assertNotIn("backup_path", row)

    def test_config_home_paths_survive_a_different_xdg_dir(self):
        # A config under ~/.config is stored config-relative ($CONFIG/…) so it
        # follows the *target* machine's $XDG_CONFIG_HOME, not a hard-coded path.
        conf_a = os.path.join(self.tmp.name, "a", ".config")
        os.makedirs(inventory.repo_root(conf_a))
        m = sync.empty_manifest()
        m["entries"].append(sync.manifest_entry(
            "neovim", os.path.join(conf_a, "nvim"),
            os.path.join(inventory.repo_root(conf_a), "nvim"), "dir"))
        sync.save_manifest(conf_a, m, os.path.join(self.tmp.name, "a"))
        with open(sync.manifest_path(conf_a), "rb") as f:
            self.assertEqual(tomllib.load(f)["entries"][0]["home_path"], "$CONFIG/nvim")
        # "clone" it onto a machine whose config dir is elsewhere, then resolve
        conf_b = os.path.join(self.tmp.name, "b", "xdgconf")
        os.makedirs(inventory.repo_root(conf_b))
        shutil.copy(sync.manifest_path(conf_a), sync.manifest_path(conf_b))
        loaded = sync.load_manifest(conf_b, os.path.join(self.tmp.name, "b"))
        self.assertEqual(loaded["entries"][0]["home_path"],
                         os.path.join(conf_b, "nvim"))  # lands under the new XDG dir

    def test_link_state_is_stored_separately_and_merged_on_load(self):
        m = sync.empty_manifest()
        e = sync.manifest_entry("neovim", "/h/.config/nvim",
            os.path.join(inventory.repo_root(self.conf), "nvim"), "dir")
        e["linked"] = True
        e["backup_path"] = os.path.join(sync.backups_root(self.conf), ".config/nvim")
        m["entries"].append(e)
        sync.save_manifest(self.conf, m, self.home)
        sync.save_link_state(self.conf, m, self.home)
        with open(sync.manifest_path(self.conf), "rb") as f:
            self.assertNotIn("linked", tomllib.load(f)["entries"][0])
        self.assertTrue(os.path.isfile(sync.link_state_path(self.conf)))
        loaded = sync.load_manifest(self.conf, self.home)["entries"][0]
        self.assertTrue(loaded["linked"])                       # state merged back
        self.assertEqual(loaded["backup_path"], e["backup_path"])

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
            sync.load_manifest(self.conf, self.home)


class ScaffoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = os.path.join(os.path.realpath(self.tmp.name), ".config")

    def test_scaffold_creates_repo_and_captures_config(self):
        repo = sync.ensure_repo_scaffold(self.conf)
        self.assertTrue(os.path.isdir(repo))                       # repo dir made first
        captured = inventory.repo_config_path(self.conf)
        self.assertTrue(os.path.isfile(captured))                  # config captured inside
        # captured copy matches the package's shipped config
        with open(captured) as a, open(inventory.default_config_path()) as b:
            self.assertEqual(a.read(), b.read())

    def test_scaffold_never_clobbers_an_existing_config(self):
        os.makedirs(inventory.repo_root(self.conf))
        captured = inventory.repo_config_path(self.conf)
        with open(captured, "w") as f:
            f.write("# customized/cloned copy\n")
        sync.ensure_repo_scaffold(self.conf)
        with open(captured) as f:
            self.assertEqual(f.read(), "# customized/cloned copy\n")  # left intact


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.realpath(self.tmp.name)
        self.conf = os.path.join(self.home, ".config")
        self.repo = os.path.join(self.conf, "config-sync")
        # a repo cloned from elsewhere: two programs' content + originals in place
        m = sync.empty_manifest()
        for prog in ("ghostty", "tmux"):
            for base in (os.path.join(self.repo, prog), os.path.join(self.conf, prog)):
                os.makedirs(base)
                with open(os.path.join(base, "config"), "w") as f:
                    f.write("x\n")
            m["entries"].append(sync.manifest_entry(
                prog, os.path.join(self.conf, prog),
                os.path.join(self.repo, prog), "dir"))
        sync.save_manifest(self.conf, m, self.home)
        # deterministic install check: a program is "installed" iff it's in qq
        p = mock.patch.object(sync, "check_installed",
                              lambda prog, qq, cfg: prog in qq)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = inventory.Config()

    def manifest(self):
        return sync.load_manifest(self.conf, self.home)

    def test_survey_flags_installed_and_missing(self):
        rows = sync.sync_survey(self.manifest(), {"tmux"}, self.cfg)
        by_prog = {e["program"]: installed for e, installed, _ in rows}
        self.assertEqual(by_prog, {"tmux": True, "ghostty": False})

    def test_apply_links_only_installed(self):
        result = sync.sync_apply(self.manifest(), self.home, self.conf,
                                 {"tmux"}, self.cfg)
        self.assertTrue(os.path.islink(os.path.join(self.conf, "tmux")))       # installed -> linked
        self.assertFalse(os.path.islink(os.path.join(self.conf, "ghostty")))   # missing -> skipped
        self.assertEqual([tilde for tilde, s in result["skipped"]
                          if s == "not-installed"],
                         [os.path.join(self.conf, "ghostty")])

    def test_force_links_even_when_not_installed(self):
        sync.sync_apply(self.manifest(), self.home, self.conf, set(), self.cfg,
                        force=True)
        self.assertTrue(os.path.islink(os.path.join(self.conf, "ghostty")))    # forced
        self.assertTrue(os.path.islink(os.path.join(self.conf, "tmux")))

    def test_apply_keeps_all_entries_in_the_manifest(self):
        sync.sync_apply(self.manifest(), self.home, self.conf, {"tmux"}, self.cfg)
        # the skipped (not-installed) entry is not dropped from the manifest
        progs = {e["program"] for e in self.manifest()["entries"]}
        self.assertEqual(progs, {"ghostty", "tmux"})

    def test_uninstalled_program_symlink_is_unlinked(self):
        # link both (force), then a later sync where ghostty is gone undoes only
        # the symlink config-sync created and restores the original.
        sync.sync_apply(self.manifest(), self.home, self.conf, set(), self.cfg,
                        force=True)
        ghostty = os.path.join(self.conf, "ghostty")
        self.assertTrue(os.path.islink(ghostty))
        result = sync.sync_apply(self.manifest(), self.home, self.conf,
                                 {"tmux"}, self.cfg)
        self.assertEqual(result["unlinked"], [ghostty])
        self.assertFalse(os.path.islink(ghostty))              # symlink removed
        self.assertTrue(os.path.isdir(ghostty))                # original restored
        self.assertTrue(os.path.islink(os.path.join(self.conf, "tmux")))  # installed kept

    def test_uninstalled_but_unlinked_is_left_untouched(self):
        # ghostty was never linked here, so there's nothing to undo — just skipped.
        result = sync.sync_apply(self.manifest(), self.home, self.conf,
                                 {"tmux"}, self.cfg)
        self.assertEqual(result["unlinked"], [])
        ghostty = os.path.join(self.conf, "ghostty")
        self.assertTrue(os.path.isdir(ghostty) and not os.path.islink(ghostty))

    def test_survey_plans_unlink_for_linked_uninstalled(self):
        sync.sync_apply(self.manifest(), self.home, self.conf, set(), self.cfg,
                        force=True)
        rows = sync.sync_survey(self.manifest(), {"tmux"}, self.cfg)
        actions = {e["program"]: action for e, _, action in rows}
        self.assertEqual(actions["ghostty"], "unlink")  # gone + config-sync's symlink
        self.assertEqual(actions["tmux"], "done")       # installed, already linked


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
