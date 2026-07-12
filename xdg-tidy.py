#!/usr/bin/env python3
"""xdg-tidy — report (and optionally perform) safe XDG relocations.

A companion to inventory.py. It checks $HOME for a small, conservative set of
"Tier 1" config files: those whose program reads the ~/.config location
*automatically* — no environment variable, wrapper, or sourced stub required —
so the file can simply be moved there without breaking the application.

Programs that only relocate via a pointer ($ZDOTDIR for zsh, $GNUPGHOME for
gnupg, GTK2_RC_FILES, a bash `source` shim, …) are deliberately left out: those
are not transparent moves, so this tool does not offer them.

Usage:
  xdg-tidy.py            report what can be moved (default; changes nothing)
  xdg-tidy.py --move     move the safe candidates into ~/.config

A candidate is only moved when its target does not already exist. When both the
HOME file and the ~/.config target are present it is reported as a manual merge
and left untouched, and a symlinked source is reported but never moved (it is
usually managed by a dotfiles tool). The tool never overwrites or deletes.
"""

import argparse
import os
import shutil
import sys

# program -> [(HOME-relative source, ~/.config-relative target)].
# INVARIANT: only programs that read the target automatically. Each pair is a
# promise that moving source -> target is transparent to the program; keep it
# that way when adding rows (verify against the program's own docs, not a guess).
TIER1 = {
    "git": [
        (".gitconfig", "git/config"),          # $XDG_CONFIG_HOME/git/config
        (".gitignore_global", "git/ignore"),   # git's default XDG excludes file
    ],
    "tmux": [
        (".tmux.conf", "tmux/tmux.conf"),      # tmux >= 3.1 reads the XDG path
    ],
}

# Status -> (label, note). "movable" is the only actionable-by-move state.
STATUS = {
    "movable": ("MOVABLE", ""),
    "merge":   ("MERGE  ", "target exists — merge by hand"),
    "symlink": ("SYMLINK", "source is a symlink — left for your dotfiles tool"),
    "done":    ("DONE   ", "already at the target"),
}


def die(msg):
    print(f"xdg-tidy.py: error: {msg}", file=sys.stderr)
    sys.exit(1)


def classify(src, dst):
    """Movement state for a HOME source and its ~/.config target."""
    src_here = os.path.lexists(src)  # lexists so a dangling/symlink source counts
    dst_here = os.path.lexists(dst)
    if not src_here:
        return "done" if dst_here else "absent"
    if os.path.islink(src):
        return "symlink"
    return "merge" if dst_here else "movable"


def survey(home, config_home):
    rows = []  # (program, src_rel, dst_rel, src_abs, dst_abs, status)
    for prog, pairs in sorted(TIER1.items()):
        for src_rel, dst_rel in pairs:
            src = os.path.join(home, src_rel)
            dst = os.path.join(config_home, dst_rel)
            status = classify(src, dst)
            if status != "absent":  # nothing to say about files you don't have
                rows.append((prog, src_rel, dst_rel, src, dst, status))
    return rows


def report(rows, moved=False):
    verb = "Moved" if moved else "Tier 1 config relocations"
    print(f"xdg-tidy — {verb} (HOME → ~/.config)\n")
    if not rows:
        print("  nothing to report — no known Tier 1 config files in $HOME.")
        return
    width = max(len(f"~/{r[1]}") for r in rows)
    for prog, src_rel, dst_rel, _, _, status in rows:
        label, note = STATUS[status]
        arrow = f"~/{src_rel:<{width}} → ~/.config/{dst_rel}"
        print(f"  {label}  {arrow}" + (f"   ({note})" if note else ""))
    movable = sum(1 for r in rows if r[5] == "movable")
    if not moved:
        print()
        if movable:
            print(f"{movable} movable · run with --move to relocate them.")
        else:
            print("Nothing to move.")


def do_move(rows):
    done = []
    for prog, src_rel, dst_rel, src, dst, status in rows:
        if status != "movable":
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        done.append((prog, src_rel, dst_rel, src, dst, "done"))
    if done:
        report(done, moved=True)
    else:
        print("xdg-tidy — nothing to move.")
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(prog="xdg-tidy.py",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--move", action="store_true",
                    help="move the safe candidates into ~/.config (default: report only)")
    args = ap.parse_args(argv)

    home = os.environ.get("HOME")
    if not home:
        die("$HOME is not set")
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")

    rows = survey(home, config_home)
    if args.move:
        do_move(rows)
    else:
        report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
