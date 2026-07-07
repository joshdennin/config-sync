# Dotfile Sync Script — Plan / Spec

## Purpose

A Lua script that automates syncing dotfile configurations to a new Linux
install. It reads a list of entries from a Lua config file and, for each one,
gets the program's config from a git repo into the right place on disk.

The goal is to keep the script reasonably minimal.

**Failure philosophy.** Minimalism wins over defensiveness. The script does only
the validation it needs in order to branch correctly; any other unexpected
condition is allowed to raise a Lua error or a failed command. That error is
surfaced to the user (operation + target + message) and — for per-entry work —
the run continues to the next entry. "Let it error and show the user" is a
valid, preferred outcome: do **not** add recovery paths or exhaustive input
checking for cases the user can read and fix themselves.

## Invocation

```
luajit config-sync.lua [config.lua]
```

- One optional positional argument: the config file path. Defaults to
  `~/.config/config-sync/config.lua` when omitted.
- If the config path is missing or fails to load/parse, print the error and exit
  non-zero before touching anything — a whole-run abort, not a per-entry skip.

## Runtime & dependencies

- **Target runtime: Lua 5.1 / LuaJIT** — the same as Neovim (which embeds
  LuaJIT) and lazy.nvim. Chosen for consistency with a program the user runs
  daily, and because pinning one version removes all cross-version handling.
  Run with the `luajit` binary to match Neovim exactly.
- The `git` binary must be on `PATH`. Filesystem work that Lua's stdlib can't do
  is delegated to standard coreutils (guaranteed present on any Linux).
- Startup runs a dependency preflight (see Bootstrap) before any entries are
  processed.

### What Lua's stdlib handles (no shell-out)

- Backup-before-overwrite renames — `os.rename`.
- `~` → `$HOME` expansion via `os.getenv("HOME")`, applied to **every** path the
  script touches (`dest`, `KNOWN_PATHS` entries, the staging root), not just
  `dest`. This must happen in Lua: because `shq` single-quotes arguments, the
  shell never expands `~` itself.
- Backup timestamps — `os.date("%Y%m%d-%H%M%S")` (colon-free, sortable).
- Removing a symlink or an empty directory — `os.remove` (POSIX `remove()`
  handles both; it won't touch a non-empty dir).

### What shells out to coreutils (via `os.execute` / `io.popen`)

| Need | Command |
|------|---------|
| Exists? / is dir? / is file? / is symlink? | `test -e` / `test -d` / `test -f` / `test -L` (branch on exit code) |
| Directory empty? | `ls -A <dir>` — empty (trimmed) output means empty |
| Create directory (recursive) | `mkdir -p <path>` |
| Create symlink | `ln -s <target> <link>` |
| Read a symlink's target | `readlink <path>` |
| Detect a command's presence | `command -v <bin>` |
| Clone / update / identify repo | `git clone <repo> <dir>`, `git -C <dir> pull`, `git -C <dir> remote get-url origin` |

### Lua 5.1 conventions (no version branching)

Because the target is fixed at 5.1 / LuaJIT, use 5.1 idioms directly — no
`_VERSION` checks:

- **`run(cmd)`** = `os.execute(cmd) == 0`. Route every `os.execute` shell-out
  through it and branch on success/failure (the nonzero value isn't a clean exit
  code in 5.1).
- **`io.popen`** for commands whose stdout is needed (`readlink`, `ls -A`,
  `git … remote get-url`); read with `pipe:read("*a")` (5.1 requires the `*`).
  In 5.1 / LuaJIT `pipe:close()` does **not** report the child's exit status, so
  these are judged by stdout only: empty (after `trim`) means "absent / no match
  / failed" and is treated as a normal expected outcome, not a reported error.
- **`trim(s)`** — every `io.popen` read passes through one trim helper before use
  or comparison; `readlink`, `git remote get-url`, and `ls -A` all emit a
  trailing `\n`.
- **`shq(s)`** — all interpolated values (`dest`, `repo`, `source`, staging
  paths, `$HOME`) are user-controlled and may contain spaces or shell
  metacharacters, so every argument is wrapped by one quoting helper (single-
  quote wrap, `'` → `'\''`). This is the single concession to robustness;
  without it such paths silently misbehave instead of failing cleanly.
- **Config loading / sandboxing** uses `loadfile(path)` then `setfenv(chunk, {})`
  before calling it — the config only needs to `return` a table, so no globals
  are exposed. `loadfile` returning `nil, err` (missing file or syntax error) is
  a whole-run abort. (`setfenv` and global `unpack` exist in 5.1 / LuaJIT.)

## Interaction model

- **Attended / interactive.** The script prompts the user at decision points
  and waits for input. There is no unattended `--yes` mode.
- **stdin is assumed to be a TTY.** No special handling for piped/EOF stdin.
- Prompts are yes/no. State the default in the prompt (e.g. `[y/N]`) and apply
  it on an empty response.

## Bootstrap / self-install

Before processing entries, the script checks that its external dependencies are
present. The tool does not install anything itself — for **every** missing
dependency it prompts the user to install it, suggesting the exact `pacman`
command, then aborts. Since the tool is pure Lua stdlib + coreutils, the only
thing to check for is `git` (coreutils are assumed present on any Linux).

**Arch-only assumption.** The target is Arch / CachyOS, so there is no
package-manager probing — package names and the suggested install command are
given for `pacman` directly (`sudo pacman -S <pkg>`).

**Uniform missing-dependency handling.** All dependency checks share one path: a
`command -v <bin>` probe, and on failure a message that names the missing binary
and prompts the user to install it with the suggested `sudo pacman -S <pkg>`,
then aborts. Adding a future dependency is just another entry in that check —
same message shape, same abort.

**Chicken-and-egg note:** the script is Lua, so the interpreter must already
exist to run it — it cannot install the interpreter for itself. If LuaJIT is
not installed, suggest that the user install Neovim (or a standalone `luajit`)
first.

Preflight (currently one entry, but the mechanism is generic):

1. **`git`** — `command -v git`. If missing, prompt the user to install it,
   suggesting `sudo pacman -S git`, then abort — nothing can be synced without
   it. The script does not run the install itself.

## Config file format

The config file returns a list of entries. Each entry carries the three original
values (program, repo, destination) plus a per-entry `mode`:

```lua
return {
  {
    program = "nvim",                                  -- lookup key + label
    repo    = "https://github.com/user/nvim.git",      -- source git repo
    dest    = "~/.config/nvim",                         -- where config lives
    mode    = "direct",                                 -- "direct" | "symlink"
  },
  {
    program = "tmux",
    repo    = "https://github.com/user/tmux.git",
    dest    = "~/.tmux.conf",
    mode    = "symlink",
    source  = "tmux.conf",                             -- optional, defaults to "all"
  },
}
```

- `~` in `dest` is expanded to `$HOME`.
- `mode` is required per entry and must be `"direct"` or `"symlink"` (see Sync
  models). Some configs are placed directly, others symlinked — chosen per
  entry, not globally. Any other value errors that entry (surfaced, then
  continue).
- `source` is optional and only applies to `mode = "symlink"`. It names what
  inside the cloned repo gets linked into `dest`:
  - Omitted or `"all"` → symlink the whole staged repo directory as `dest`
    (`dest -> <staging path>`).
  - A path (e.g. `"tmux.conf"`) → symlink just that single file/dir inside the
    repo, and `dest` is treated as the link target for it.
  - Ignored for `mode = "direct"`.

## Known config paths (lookup list)

The `program` value is used as a key into a built-in lookup table of common
locations where a config for that program may already exist. This is how the
"check common paths" pre-flight works.

```lua
local KNOWN_PATHS = {
  nvim  = { "~/.config/nvim", "~/.vim", "~/.vimrc" },
  tmux  = { "~/.tmux.conf", "~/.config/tmux" },
  -- ...extend as needed
}
```

If `program` is not in the table, skip the pre-flight probe (only the `dest`
checks below apply) and note that in the output.

## Sync models

### `mode = "direct"`
The repo *is* the config. Clone directly into `dest`; `dest` becomes a git
working copy.
- Not yet cloned → `git clone <repo> <dest>`.
- Already the correct clone → `git -C <dest> pull` (see idempotency).

### `mode = "symlink"`
Clone the repo into a central staging root, then symlink from it into place.
- Staging root: `~/.local/share/config-sync/<program>` (create if absent).
- Not yet cloned → `git clone <repo> <staging>`.
- Already cloned → `git -C <staging> pull`.
- Then create the symlink according to `source`:
  - `"all"` (default) → link the whole staged repo: `dest -> <staging path>`.
  - a named path → link a single entry: `dest -> <staging>/<source>`.
- If `dest` already exists as a symlink pointing at the correct staged target,
  leave it.

## Per-entry flow

For each entry, in order:

1. **Expand paths.** Resolve `~` in `dest`.

2. **Pre-flight — probe known paths.** For each path in `KNOWN_PATHS[program]`
   other than `dest`, check if a config already exists there. If any do, warn
   the user (list them) and prompt whether to proceed with this entry.
   - No → skip this entry.

3. **Inspect state and branch.** The state machine differs by `mode`: a direct
   entry manages a *clone at `dest`*, while a symlink entry manages a *clone in
   staging* plus a *link at `dest`*.

   **`mode = "direct"`** — inspect `dest`:

   | `dest` state               | Action |
   |----------------------------|--------|
   | Does not exist             | Prompt to create. Yes → `git clone` (git makes the path). No → skip. |
   | Exists, empty              | Clone into it. |
   | Exists, already this clone | Re-run → `git -C <dest> pull`. |
   | Exists, other content      | Prompt to overwrite. Yes → back up (rename to `<dest>.bak.<timestamp>`), then clone. No → skip. |

   **`mode = "symlink"`** — first bring the staging clone up to date (clone if
   absent, else `git -C <staging> pull`), then reconcile the link at `dest`. The
   intended target is `<staging>` for `source` omitted/`"all"`, else
   `<staging>/<source>`:

   | `dest` state                        | Action |
   |-------------------------------------|--------|
   | Is a symlink (`test -L`)            | `readlink` it: matches the intended target → no-op; otherwise `os.remove` the link and re-create it (covers wrong target and dangling links). |
   | Does not exist                      | `mkdir -p` the parent, then `ln -s`. |
   | Exists, empty directory             | `os.remove` it, then `ln -s`. |
   | Exists, other content (file or dir) | Prompt to overwrite. Yes → back up (rename), then `ln -s`. No → skip. |

   "Already this clone" / "the staging clone matches" = the git repo's `origin`
   URL equals `repo` after trimming whitespace and a trailing `.git`. A mismatch
   is treated as "other content" and re-prompts; there is no deeper
   reconciliation.

4. **Sync** per the entry's `mode` (see Sync models).

5. **Record the result** for the summary, one of:
   - `synced` — fresh clone made, or symlink newly created/replaced.
   - `pulled` — existing clone updated via `git pull` (whether or not it had new
     commits — no output parsing).
   - `unchanged` — nothing to do: a symlink already pointing at the correct
     target (true no-op).
   - `skipped` — user declined a prompt (create / overwrite / known-path).
   - `failed` — an operation errored (surfaced, run continued).

## Idempotency (re-runs)

Running the script again must be safe:
- If the target is already the correct clone → `git pull`, don't re-clone.
- If a symlink is already correct → leave it in place.

## Error handling

- Check the result of every operation that has a checkable one: the `run()`
  success boolean (`os.execute(cmd) == 0`) for `os.execute` shell-outs, and the
  `nil, errmsg` return of stdlib calls like `os.rename`. `io.popen` commands
  carry no exit status in 5.1 (see conventions) and are judged by trimmed
  stdout.
- On failure (clone/pull/mkdir/ln/rename/etc.): **surface the error to the user**
  with the operation, the target, and the exit code or error message, then
  continue to the next entry.
  Do not attempt automatic recovery — the user attends to failures manually.
- At the end, print a **summary**: the per-entry outcome (`synced` / `pulled` /
  `unchanged` / `skipped` / `failed`), and a clear list of any entries that
  failed or were skipped so nothing is silently missed.

## Out of scope

- No `--yes` / non-interactive mode.
- No non-TTY stdin handling.
- No automatic conflict resolution beyond the backup-then-overwrite prompt.
