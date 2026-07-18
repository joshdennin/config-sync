# config-sync

Inventory, health-check, and manage the config/dotfiles on an Arch / CachyOS
system. `config-sync` answers *what config do I have, where, which program owns
it, and is it under version control?* — and then, on request, builds a dotfiles
repo from what it finds, symlinks that repo back into place, and can undo either
step. The repo is designed to be **shared across machines**: push it from one
box, clone it on another, and `sync` deploys the configs whose programs are
actually installed there.

Discovery is always **read-only**. Every mutation is opt-in, copies before it
touches the original, and is reversible — no operation ever destroys the only
copy of anything.

## Install

Requires Python 3.11+ and, for `scan` / `adopt` / `sync`, `pacman` on `PATH`
(used for package-ownership and install queries). The only third-party
dependency is `tomli-w`.

```sh
pip install -e .
```

This installs the `config-sync` console script; `python -m config_sync` is
equivalent.

## Commands

| Command | What it does | Writes? |
|---------|--------------|---------|
| `scan` | Discover config entries, classify them, print a report (or `--json`). | no |
| `health` | Render a `:checkhealth`-style report from a saved `scan --json`. | no |
| `tidy` | Relocate a safe set of `$HOME` config files into `~/.config`. | only with `--move` |
| `adopt` | Write an editable plan, then copy chosen configs into a managed repo. | only with `--apply` |
| `link` | Back up each home original and symlink it to the repo copy. | only with `--apply` |
| `sync` | Deploy a repo (e.g. cloned from another machine): symlink the configs whose program is installed here, and unlink any whose program is gone. | only with `--apply` |
| `unlink` | Remove the symlinks and restore the backed-up originals. | only with `--apply` |

Run `config-sync <command> --help` for the full flag set.

## A vertical slice

A start-to-finish walkthrough: inspect the system, adopt a couple of configs
into a repo, deploy them as symlinks, then roll the whole thing back.

```sh
# 1. See what's on the box (human-readable listing).
config-sync scan

# 2. Save the full structured inventory, then health-check it offline.
config-sync scan --json > inventory.json
config-sync health inventory.json

# 3. (optional) Move stray $HOME config into ~/.config where it belongs.
config-sync tidy            # preview the safe relocations
config-sync tidy --move     # perform them

# 4. Plan a dotfiles repo from the strong-signal configs. This creates the
#    repo directory, captures the classification config and an editable plan
#    inside it, and copies nothing else. The plan defaults into the repo.
config-sync adopt --select curated

# 5. Edit the plan: set `adopt = false` on anything you want to skip.
$EDITOR ~/.config/config-sync/config-sync-adopt.toml

# 6. Build the repo at ~/.config/config-sync/ from the edited plan
#    (copies originals in, writes a manifest, git-commits).
config-sync adopt --apply

# 7. Deploy the repo back into place: each original is backed up, then
#    replaced with a symlink into the repo.
config-sync link            # preview the link plan
config-sync link --apply    # create the symlinks

# 8. Changed your mind? Reverse it — the symlinks are removed and the
#    backed-up originals restored.
config-sync unlink --apply
```

After step 6 the managed repo lives at `~/.config/config-sync/`, grouped by
program (`nvim/`, `tmux/`, `git/`, …), with a `manifest.toml` recording the
home ⇄ repo mapping and its own captured `inventory-config.toml`. After step 7,
a re-`scan` shows the linked configs resolving into that repo.

## Sharing across machines

The repo built by `adopt` is a portable dotfiles repo — its `manifest.toml`
stores machine-independent paths (`~/`-relative home, repo-relative repo), so it
resolves against any `$HOME`.

```sh
# On the source machine: push the repo somewhere you can reach it.
git -C ~/.config/config-sync remote add origin <git-url>
git -C ~/.config/config-sync push -u origin main

# On a new machine: clone it into place, then deploy.
git clone <git-url> ~/.config/config-sync
config-sync sync            # preview: which configs' programs are installed here?
config-sync sync --apply    # symlink the installed ones (missing programs skipped)
```

`sync` reconciles the symlinks with what's installed: it links the configs whose
program is present, and removes the symlink of one whose program is gone
(restoring the original). Pass `sync --force` to link everything regardless.

The repo stays clean across all of this: `manifest.toml` holds only the portable
home ⇄ repo mapping, while machine-local link state lives in a git-ignored
`.link-state.toml`. So deploying on one machine never dirties the shared repo,
and a later `git -C ~/.config/config-sync pull` updates every config in place
(they're symlinks into the repo) without conflicts.

To protect a shared repo, `adopt --apply` **refuses to copy local configs into a
repo that already holds adopted content** (one you cloned, or already built).
Pass `adopt --apply --force` to add to it deliberately.

## Configuration

The classification tables — which programs exist, which files are shell startup
files, which stores are secret, which names are machine-generated noise — live
in `inventory-config.toml` (shipped with the package), not in the code. Teaching
the tool a new program or secret store is a file edit. Point at your own copy
with `scan --config PATH`.

`adopt` captures a copy of this config inside the repo
(`~/.config/config-sync/inventory-config.toml`) so a clone carries the registry
it was built with. `scan` and `adopt` read the package copy; `sync` prefers the
repo's captured copy so a freshly cloned repo classifies consistently. Existing
captured copies are never overwritten.

## Safety

- `scan` and `health` never write, move, or delete — their only side effects are
  filesystem reads and read-only `pacman` / `git` queries.
- Every write goes through primitives that refuse to overwrite or delete
  existing state; `adopt` copies (originals untouched) and `tidy --move` only
  relocates when the target is absent.
- `adopt` never copies secrets (`.ssh`, `.gnupg`, `.aws`, …), dangling links,
  generated/cache/state content, or the managed repo itself. When it copies a
  directory it strips `.git`, `.gitignore`, `.venv`, and `__pycache__`, so a
  version-controlled config doesn't nest a repo and no virtualenv/bytecode noise
  comes along.
- Configs that are already their own git repo are surfaced in the plan flagged
  `managed = true` with `adopt = false` — visible for review but opt-in, never
  copied unless you turn `adopt` on.
- `adopt --apply` refuses to add to a repo that already holds adopted content
  (protecting a shared/cloned repo) unless given `--force`.
- `link` backs up before it symlinks and is atomic — a failed link restores the
  original rather than leaving it stranded. `sync` links only configs whose
  program is installed locally, and when it removes a symlink for an
  uninstalled program it only touches one config-sync itself created. `unlink`
  likewise only touches a symlink config-sync created, leaving anything you've
  since replaced by hand alone.

## More

- [`DESIGN.md`](DESIGN.md) — architecture, the data model, and the rationale
  behind the read-only/mutating split.
- [`ISSUES.md`](ISSUES.md) — known gaps and backlog.
