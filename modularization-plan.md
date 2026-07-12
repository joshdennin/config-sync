# Modularization Plan — inventory.py → config-sync

Plan for breaking `inventory.py` and its companion scripts into modules behind a
central CLI (renamed **`config-sync`**), sized against two planned features:

1. **adopt** — automated generation of a dotfiles git repo from discovered files
2. **link** — automated symlinking of repo files back to their filesystem locations

## Guiding principle: two extension axes, one safety boundary

Everything the tool does today is read-only; the docstring's load-bearing
promise is "never writes, moves, or deletes anything." The new features are
**writers**, so the architecture grows a second axis:

- **Reporters** (read-only): `listing`, `health`, `json` — pure `(inv, args, cfg) -> str`.
- **Actions** (mutating): `tidy`, `adopt`, `link`, `unlink` — take intent, change
  the filesystem, return a result.

These are different seams and must not be conflated. The package makes the
boundary structural: **`inventory.py` and `report.py` never import `fsops` or
`sync`; `sync.py` is the only module that imports `fsops` or runs writing-git.**
That one invariant keeps the inspection core (scan → model → reporters) provably
read-only.

## Resolved design decisions

1. **No back-compat.** `xdg-tidy.py` is deleted; its logic becomes the `tidy`
   action. The `inventory.py` entry point goes away.
2. **Rename to `config-sync`.** Package `configsync`, console script `config-sync`.
   Error prefixes change from `inventory.py: error:` to `config-sync: error:`.
3. **Reversible by construction.** `adopt` **copies** (originals stay in place, so
   adopt reverses by deleting the repo). `link` **backs up the original, then
   symlinks**; a first-class `unlink` action restores the backup. No operation
   destroys the only copy of anything.
4. **Repo at `~/.config/config-sync/`, grouped by program.** Each program gets its
   own directory named by its command (the `bin`/section key `health` already
   groups by — `nvim`, `tmux`, `git`). Inside that directory the program's own
   structure is mirrored (e.g. `nvim/lua/plugins/…` is preserved as-is).

### Consequence: a manifest is the source of truth

Program-grouping discards the original home location, so `link` cannot infer that
`repo/tmux/tmux.conf` came from `~/.tmux.conf` rather than `~/.config/tmux/`. A
**manifest** (`~/.config/config-sync/manifest.toml`) records, per adopted entry:
`program`, `home_path`, `repo_path`, `kind`, and link state (`linked` + `backup_path`).
`adopt` writes it; `link`/`unlink` consume and update it; `scan` can cross-check it.
It is also what makes reversal robust rather than convention-guessing.

The manifest is **TOML**, matching the config style: read with stdlib `tomllib`,
written with the small `tomli_w` dependency. TOML has no null, so absent values
(unattributed program, not-yet-set backup path) are stored as `""`.

### Consequence: the tool must not adopt its own repo

`~/.config/config-sync/` lives under a scan root. The adoptability gate must
exclude anything under the repo path, and treat the repo itself as the managed
dotfiles repo (it is a git repo once `adopt` runs). After linking, a re-scan sees
`~/.config/nvim` as a symlink resolving into the repo and dedups via `via_symlink`
— the same `dotfiles/nvim` + `via_symlink:[.config/nvim]` case already in the test
fixture — so the linked state is representable and idempotency falls out for free.

## Target layout — 5 files

```
configsync/
  inventory.py  # READ-ONLY engine: Config/load_config, content probes, the
                #   capture/status_counts/git_record shell-out seam, build_inventory
                #   /analyze/score/categorize, the Entry model, adoptability gate
  report.py     # read-only reporters: listing, health, json (pure functions of the model)
  fsops.py      # safe copy/move/symlink/backup/restore: dry-run, no-overwrite, no-delete
  sync.py       # ALL mutation: home<->repo mapping, manifest read/write, and the
                #   tidy/adopt/link/unlink actions (the only importer of fsops + writing-git)
  cli.py        # argparse dispatch: scan · health · tidy · adopt · link · unlink
```

Dependency flow is acyclic. `inventory.py` and `report.py` are read-only and
never import `fsops`/`sync`. `sync.py` is the sole importer of `fsops` and the
sole place running writing-git (`init`/`add`/`commit`) — the read-only git seam
(`git_record`) stays in `inventory.py`. The console script `config-sync` maps to
`configsync.cli:main`.

Rationale for the merges: the read-only pipeline (config, probes, shell seam,
scan, model) is one cohesive engine, so it is one module. The path-mapping and
manifest are ~50 lines each and exist only to serve the actions, so they live in
`sync.py` with them. `fsops.py` stays separate on purpose: it is the code most
likely to clobber `$HOME` if wrong, so the dangerous primitives are isolated and
unit-tested with no domain logic mixed in. (An even leaner 4-file variant folds
`report.py` into `inventory.py`; `fsops` is the one split not worth collapsing.)

The `capture`/`status_counts`/`_git_cache` monkeypatch seam stays as functions in
`inventory.py`; tests patch `inventory.capture` exactly as today.

---

## Step 1 — Config extraction ✅ DONE

Replaced ten module-level mutable globals with a frozen `Config` dataclass;
`load_config()` returns it; threaded `cfg` through the scan and health paths. A
default-constructed `Config()` preserves `health`'s existing behavior. All 11
tests pass; real `scan → health` round-trip verified. Every classification
function is now a pure function of its inputs plus `cfg`.

## Step 2 — Fold `xdg-tidy.py` into a `tidy` action

First *action*, and a forcing function for the shared mutation substrate.

- Add a `tidy` subparser (`--move`); reuse the shared `die()` and a single
  HOME/`XDG_CONFIG_HOME` resolver. **Delete `xdg-tidy.py`.**
- Move `TIER1`, `STATUS`, `classify`, `survey`, `report`, `do_move` in.
- **Keep the pacman preflight scoped to scan** — `tidy` must not require pacman.
- Add a `TidyTest` (temp-HOME fixture: movable / merge / symlink / done).
- `tidy` (XDG hygiene) stays orthogonal to the repo workflow and coexists with it.

## Step 3 — Shared mutation substrate (`fsops` + mapping/manifest helpers) ✅ DONE

Foundation built ahead of the features that need it, written as sections of the
single file (destined for `fsops.py` and `sync.py` at the step-9 split). 12 new
tests; suite at 26, all green.

- **`fsops` primitives** — `safe_copy`, `safe_move`, `safe_symlink`,
  `remove_symlink`, `backup`, `restore`, all guarded by an `FsError`: never
  overwrite, never delete (`remove_symlink` refuses a non-symlink; `backup`
  refuses a path outside home), parents created as needed. Dry-run/reporting was
  deliberately left to the action layer (survey a plan, then call these to
  execute) so the primitives stay small and always-act.
- **mapping helper** — `repo_root`, `program_dirname`, `repo_path_for`. Repo target
  = `config-sync/<cmd>/…`; a directory entry maps to the program dir itself (tree
  copied in, structure preserved), a file entry to a file beneath it. Takes fields
  rather than a record, so it survives the step-4 Entry refactor.
- **manifest helper** — `load_manifest` (missing → empty), `save_manifest`,
  `empty_manifest`, `manifest_entry`, `manifest_path`; **TOML** via stdlib
  `tomllib` (read) + `tomli_w` (write), with `""` for absent values.
- Retargeted `tidy_move` onto `safe_move`, proving the substrate on the simplest
  action (behavior-preserving; a `movable` status guarantees no collision).

## Step 4 — Typed `Entry` model + adoptability gate (→ `inventory.py`)

With writers sharing the field contract, the loose dict is no longer safe enough
(misreading `flags`/`editable` can copy a secret into a repo or clobber a file).

- Define `Entry` (dataclass), JSON-round-trippable so the interchange format is
  unchanged.
- **Add the adoptability predicate to the read-only engine**: consolidate the
  secret/generated/editable/cache-state logic (today in `is_visible` + the
  `editable`/`flags` computation) into one tested `is_adoptable(entry)` gate that
  also **excludes anything under `~/.config/config-sync/`**. Reporters use it to
  filter for display; `adopt` uses it as a **safety gate** that hard-excludes
  `flags:["secret"]`.

## Step 5 — Reporter registry (→ `report.py`)

- Normalize `render_listing` to **return** a string (move `print` to the command
  wrapper) so all reporters share `(inv, args, cfg) -> str`.
- Small registry: format name → reporter (`listing`, `health`, `json`).
- Guardrail: inventory dict stays plain JSON-serializable.

## Step 6 — Feature 1: `adopt` (build dotfiles repo)  — COPY, preserving originals

- New `adopt` action (→ `sync.py`) + subcommand. Consumes the inventory, filters by
  `is_adoptable`, maps home→repo, **copies** the tree into `~/.config/config-sync/<cmd>/…`
  via `fsops` (originals untouched), writes the manifest, then writing-git
  `init`/`add`/`commit`.
- Dry-run by default (report what would be adopted); `--apply` / `--commit` to act.
- Reversal is trivial (originals preserved): removing the repo undoes adopt.
- Multi-location programs (e.g. tmux at `~/.tmux.conf` and `~/.config/tmux/`) are
  disambiguated by per-entry manifest rows.

## Step 7 — Feature 2: `link` (deploy repo → filesystem)  — BACK UP then symlink

- New `link` action (→ `sync.py`) + subcommand. For each manifest entry not yet
  linked: **back up** the home original via `fsops`, record `backup_path`, create a
  symlink home→repo, mark `linked` in the manifest.
- Never overwrite; on collision back up or skip.
- Dry-run by default; `--apply` to act. Idempotent: already-linked entries (symlink
  resolving into the repo) are skipped.

## Step 8 — `unlink` (reverse of link)  — restore originals

- New `unlink` action (→ `sync.py`) + subcommand. For each linked manifest entry:
  remove the symlink, **restore the backup** via `fsops`, clear the link state.
- Dry-run by default; `--apply` to act. This is the reversibility guarantee made
  operational.

## Step 9 — Package split + packaging

Mechanical, done last behind green tests at every prior step.

- Split the single file into the 5-file `configsync/` layout above. The
  `capture`/`status_counts`/`_git_cache` monkeypatch seam stays intact as functions
  in `inventory.py`.
- Split `test_inventory.py` to mirror the layout (`test_inventory`, `test_report`,
  `test_fsops`, `test_sync`); update import paths.
- Add `pyproject.toml` with the `config-sync = configsync.cli:main` console script
  and its runtime dependencies (currently just `tomli_w`; keep the set minimal).

---

## Sequencing & risk

- Done: 1. Then in order: 2 (tidy) → 3 (fsops + mapping/manifest) → 4 (model/gate) →
  5 (reporters) → 6 (adopt) → 7 (link) → 8 (unlink) → 9 (package split).
- 3 and 4 are the foundation the features stand on; building them first makes
  adopt/link/unlink small actions rather than big new mechanisms.
- 5 is independent of 3/4 and can slot in anywhere.
- 6→7→8 must stay in order (need a repo before linking; need a link before unlinking).
- 9 touches every test import, so it lands last.
- Each step is behavior-preserving or additive and ships green.

## Sub-decisions

Settled while building step 3:
- **Manifest format** — **TOML** (`manifest.toml`), schema `program`, `home_path`,
  `repo_path`, `kind`, `linked`, `backup_path`; read via stdlib `tomllib`, written
  via the small `tomli_w` dependency; absent values stored as `""` (TOML has no null).
- **Program directory name** — the program's command name (`bin` from the config,
  else the program key), via `program_dirname`; falls back to the home basename for
  unattributed entries.
- **Backup mechanism** — `fsops.backup(path, backups_root, home)` mirrors the home
  path under a backups root; `unlink` reverses via `restore`.

Still open (settle at the relevant step):
- **Backup root location** (step 7) — recommended `~/.config/config-sync/.backups/`,
  which `fsops.backup` already supports by passing it as `backups_root`.
- **adopt selection controls** (step 6) — flags to include/exclude specific programs
  or categories, and whether the relevance floor applies to adoption as it does to
  display.
