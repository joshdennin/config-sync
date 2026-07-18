# config-sync

Inventory, health-check, and manage the config/dotfiles on an Arch / CachyOS
system. `config-sync` answers *what config do I have, where, which program owns
it, and is it under version control?* — and then, on request, builds a dotfiles
repo from what it finds, symlinks that repo back into place, and can undo either
step.

Discovery is always **read-only**. Every mutation is opt-in, copies before it
touches the original, and is reversible — no operation ever destroys the only
copy of anything.

## Install

Requires Python 3.11+ and, for `scan` / `adopt`, `pacman` on `PATH` (used for
package-ownership queries). The only third-party dependency is `tomli-w`.

```sh
pip install -e .
```

This installs the `config-sync` console script; `python -m configsync` is
equivalent.

## Commands

| Command | What it does | Writes? |
|---------|--------------|---------|
| `scan` | Discover config entries, classify them, print a report (or `--json`). | no |
| `health` | Render a `:checkhealth`-style report from a saved `scan --json`. | no |
| `tidy` | Relocate a safe set of `$HOME` config files into `~/.config`. | only with `--move` |
| `adopt` | Write an editable plan, then copy chosen configs into a managed repo. | only with `--apply` |
| `link` | Back up each home original and symlink it to the repo copy. | only with `--apply` |
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

# 4. Plan a dotfiles repo from the strong-signal configs. This writes an
#    editable plan file and copies nothing.
config-sync adopt --select curated --plan config-sync-adopt.toml

# 5. Edit the plan: set `adopt = false` on anything you want to skip.
$EDITOR config-sync-adopt.toml

# 6. Build the repo at ~/.config/config-sync/ from the edited plan
#    (copies originals in, writes a manifest, git-commits).
config-sync adopt --apply --plan config-sync-adopt.toml

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
home ⇄ repo mapping. After step 7, a re-`scan` shows the linked configs
resolving into that repo.

## Configuration

The classification tables — which programs exist, which files are shell startup
files, which stores are secret, which names are machine-generated noise — live
in `inventory-config.toml` (shipped with the package), not in the code. Teaching
the tool a new program or secret store is a file edit. Point at your own copy
with `scan --config PATH`. The tool never writes this file.

## Safety

- `scan` and `health` never write, move, or delete — their only side effects are
  filesystem reads and read-only `pacman` / `git` queries.
- Every write goes through primitives that refuse to overwrite or delete
  existing state; `adopt` copies (originals untouched) and `tidy --move` only
  relocates when the target is absent.
- `adopt` never copies secrets (`.ssh`, `.gnupg`, `.aws`, …), dangling links,
  generated/cache/state content, or the managed repo itself.
- `link` backs up before it symlinks and is atomic — a failed link restores the
  original rather than leaving it stranded. `unlink` only touches a symlink
  config-sync created, leaving anything you've since replaced by hand alone.

## More

- [`DESIGN.md`](DESIGN.md) — architecture, the data model, and the rationale
  behind the read-only/mutating split.
- [`ISSUES.md`](ISSUES.md) — known gaps and backlog.
