# Tracked issues

Backlog from the code review. Resolved items are kept (struck through with a
resolution note) so the history of the review stays legible; the open items
follow.

---

## 1. Secrets carry `editable = True` — a load-bearing footgun ✅ RESOLVED

**Where:** `inventory.py` `analyze` (sets `editable=True` for secrets because
content sniffing is skipped) and `is_adoptable` (re-excludes them explicitly).

**Problem:** A secret entry is marked `editable=True`, so the *only* thing
keeping it out of `adopt` was the explicit `"secret" in rec["flags"]` check at
the top of `is_adoptable`. The safety of the whole tool therefore rested on every
current and future consumer of `editable` remembering that, for secrets, the
flag means the opposite of what it reads like. A comment is not a guardrail.

**Impact:** High if it ever regresses (a secret store copied into a synced
repo), low probability today. It is a latent trap, not a live bug.

**Resolution:** The gate is now a pure predicate, `safe_to_adopt(location, flags,
editable, real_path, conf_home)`, evaluated **once per entry at scan time** and
stored as the new `Entry.adoptable` field. `is_adoptable(rec)` is a thin accessor
that only reads that bool — no downstream code reconstructs the decision from
`editable`, which is left to mean strictly "human-editable content." A live scan
confirms no `secret`-flagged entry is ever `adoptable`. Covered by
`AdoptableTest` (predicate) and `test_is_adoptable_reads_the_stored_decision`.

---

## 2. Module-level `_git_cache` global ✅ RESOLVED

**Where:** `inventory.py:_git_cache` (module-level dict), populated by
`git_record`.

**Problem:** Unbounded shared mutable state at module scope. It was correct for a
single CLI invocation, but tests had to `mock.patch.dict(..., clear=True)` to
isolate from each other, which is the tell.

**Impact:** Low. No user-visible bug; a maintainability/testability smell.

**Resolution:** The cache now lives on a per-scan `Scan` context dataclass (home,
config root, pacman set, `Config`, and `git_cache`), built once in
`build_inventory` and threaded through `analyze` → `git_record(anchor, cache)`.
Its lifetime is scoped to one scan; the module global is gone and the test that
cleared it was removed — scans are isolated by construction.

---

## 3. Performance on repo-heavy homes

**Where:** `inventory.py:git_record` (≈10 `capture` subprocesses per repo, each
with a 60s timeout) plus a separate `Popen` in `status_counts`.

**Problem:** A `$HOME` with many git repos spawns a large number of git
subprocesses. Memoization per repo toplevel helps, but the per-repo cost is
still ~10 forks.

**Impact:** Low–medium; a latency concern on large systems, not a correctness
issue.

**Suggested fix:** Batch the per-repo git queries (fewer invocations via
combined `git` calls / plumbing), and/or lower the per-call timeout for the
cheap queries.

---

## 4. Arch-only by hard dependency

**Where:** `cli.py:cmd_scan` / `cmd_adopt` (`shutil.which("pacman")` gate),
`inventory.py:load_pacman_qq` / `pacman_owner`.

**Problem:** The tool hard-requires `pacman`. This is intentional and documented,
but it caps the audience to Arch/CachyOS.

**Impact:** Scope decision, not a defect. Logged so it is a conscious choice
rather than an accident.

**Suggested fix (only if broadening is ever wanted):** Factor package-ownership
behind a small provider interface so a `dpkg`/`rpm` backend could be added; make
the "no package manager" path a soft degrade (skip the cross-reference) rather
than a hard error.

---

## 5. Inconsistent mutation verbs across subcommands

**Where:** `cli.py` — `tidy --move`, `link --apply`, `sync --apply`,
`unlink --apply` (and `adopt`, which now mutates directly with no gate flag —
the plan is its review surface and a populated-repo guard protects it).

**Problem:** `tidy` gates its mutation behind `--move`, `link`/`sync`/`unlink`
behind `--apply`, and `adopt` behind nothing. Minor inconsistency in the CLI
surface.

**Impact:** Cosmetic / UX.

**Suggested fix:** Standardize on one convention (a uniform `--apply`, or a
uniform `--dry-run`-defaults model) across all mutating commands.

---

## 6. No README

**Where:** repo root; the `cli.py` module docstring currently doubles as the
usage reference.

**Problem:** A `pyproject`-packaged tool ships without a README. The material
already exists in the `cli.py` docstring and `health.md`; it just is not
surfaced as a README.

**Impact:** Discoverability / packaging polish.

**Suggested fix:** Extract a short README from the existing `cli.py` docstring
(overview, install, the six subcommands, the safety model).

---

## 7. End-to-end round-trip test gap ✅ RESOLVED

**Where:** `tests/` — unit coverage of the pure logic is good, but there was no
test that drives `adopt → link → unlink` on a real temp filesystem and asserts
the original is byte-identical after restore.

**Problem:** The reversibility guarantee is the tool's central promise, and it
was only covered piecewise. (The link partial-failure path is covered by
`test_failed_symlink_rolls_back_the_backup`; the full happy-path round trip was
still worth an explicit assertion.)

**Impact:** Medium — this is exactly where a durability regression would hide.

**Resolution:** `RoundTripTest.test_adopt_link_unlink_restores_byte_identical`
adopts a file dotfile and a directory config into a temp home, links them
(asserting each home path becomes a symlink into the repo), unlinks them, and
asserts both originals are back byte-for-byte, are real files/dirs again, and
that no symlink or backup is left behind while the repo copies survive.
