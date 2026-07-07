#!/usr/bin/env python3
# config-sync.py — sync dotfile configs from git repos into place.
# Python port of config-sync.lua. See config-plan.md.
#
# Failure philosophy: do only the validation needed to branch correctly. Any
# other unexpected condition is allowed to fail; it is surfaced to the user and
# (for per-entry work) the run continues.
#
# Unlike the Lua/Bash ports, filesystem inspection is done natively (os.path,
# os.symlink, ...); only git operations shell out.
#
# Config format (default ~/.config/config-sync/config.py) defines a CONFIG list:
#   CONFIG = [
#     {"program": "nvim", "repo": "https://example/nvim.git",
#      "dest": "~/.config/nvim", "mode": "direct"},
#     {"program": "tmux", "repo": "https://example/tmux.git",
#      "dest": "~/.tmux.conf", "mode": "symlink", "source": "tmux.conf"},
#   ]

import datetime
import os
import shutil
import subprocess
import sys

HOME = os.environ.get("HOME", "")
STAGING_ROOT = os.path.join(HOME, ".local", "share", "config-sync")

KNOWN_PATHS = {
    "nvim": ["~/.config/nvim", "~/.vim", "~/.vimrc"],
    "tmux": ["~/.tmux.conf", "~/.config/tmux"],
    # ...extend as needed
}

# ----------------------------------------------------------------- helpers


def expand(path):
    """Expand a leading ~ to $HOME."""
    if path == "~":
        return HOME
    if path.startswith("~/"):
        return HOME + path[1:]
    return path


def run(cmd):
    """Run a command (argv list); True iff it exited 0."""
    return subprocess.call(cmd) == 0


def capture(cmd):
    """Full stdout of a command, stripped; '' on failure."""
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return ""
    return out.decode("utf-8", "replace").strip()


def git_origin(directory):
    return capture(["git", "-C", directory, "remote", "get-url", "origin"])


def norm_url(url):
    """Compare repo URLs ignoring whitespace and a trailing '.git'."""
    url = url.strip()
    return url[:-4] if url.endswith(".git") else url


def have(binary):
    return shutil.which(binary) is not None


def backup(path):
    """Rename path aside. Returns (ok, backup_path, err)."""
    dest = path + ".bak." + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        os.rename(path, dest)
        return True, dest, None
    except OSError as err:
        return False, dest, str(err)


def prompt_yn(question, default_yes):
    """Yes/no prompt; the stated default applies on empty input / EOF."""
    sys.stdout.write(question + (" [Y/n] " if default_yes else " [y/N] "))
    sys.stdout.flush()
    answer = sys.stdin.readline()
    if answer == "":  # EOF
        return default_yes
    answer = answer.strip().lower()
    if answer == "":
        return default_yes
    return answer in ("y", "yes")


# --------------------------------------------------------- result tracking

results = []  # list of {"program", "outcome", "detail"}


def record(program, outcome, detail=""):
    results.append({"program": program, "outcome": outcome, "detail": detail})


def fail(program, operation, target, msg):
    """Surface an error and record a failure; the run continues."""
    sys.stderr.write("[{}] {} failed on {}: {}\n".format(program, operation, target, msg))
    record(program, "failed", "{} {}".format(operation, target))


# --------------------------------------------------- config loading (sandboxed)


def load_config(path):
    """exec the config with no builtins; read its CONFIG list."""
    try:
        with open(path, "r") as handle:
            src = handle.read()
    except OSError as err:
        return None, str(err)
    namespace = {"__builtins__": {}}
    try:
        exec(compile(src, path, "exec"), namespace)
    except Exception as err:  # syntax or runtime error -> whole-run abort
        return None, str(err)
    config = namespace.get("CONFIG")
    if not isinstance(config, list):
        return None, "config did not define a CONFIG list"
    return config, None


# --------------------------------------------------------------- bootstrap

# Arch-only: suggest the exact pacman command; never auto-install.
DEPENDENCIES = [("git", "git")]  # (binary, package)


def preflight():
    for binary, package in DEPENDENCIES:
        if not have(binary):
            sys.stderr.write(
                "Missing dependency: {}\nInstall it with:\n  sudo pacman -S {}\n".format(
                    binary, package))
            return False
    return True


# ------------------------------------------------------------- known paths


def probe_known_paths(entry, dest):
    program = entry["program"]
    paths = KNOWN_PATHS.get(program)
    if paths is None:
        print("[{}] no known-paths entry; skipping probe.".format(program))
        return True
    found = [expand(p) for p in paths
             if expand(p) != dest and os.path.exists(expand(p))]
    if not found:
        return True
    print("[{}] existing config found at:".format(program))
    for p in found:
        print("  " + p)
    return prompt_yn("[{}] proceed with this entry anyway?".format(program), False)


# -------------------------------------------------------------- sync models


def git_clone(program, repo, directory, ok_detail):
    if run(["git", "clone", repo, directory]):
        record(program, "synced", ok_detail)
        return True
    fail(program, "git clone", directory, "clone failed")
    return False


def make_symlink(program, target, dest, ok_detail):
    try:
        os.symlink(target, dest)
    except OSError as err:
        fail(program, "ln -s", dest, str(err))
        return False
    record(program, "synced", ok_detail)
    return True


def dir_empty(path):
    try:
        return len(os.listdir(path)) == 0
    except OSError:
        return False


# mode = "direct": the repo IS the config; clone into dest.
def sync_direct(entry, dest):
    program, repo = entry["program"], entry["repo"]

    if not os.path.exists(dest):
        if not prompt_yn(
                "[{}] {} does not exist. Create and clone?".format(program, dest), True):
            record(program, "skipped", "create declined")
            return
        git_clone(program, repo, dest, "cloned to " + dest)
        return

    if os.path.isdir(dest) and dir_empty(dest):
        git_clone(program, repo, dest, "cloned into empty " + dest)
        return

    if os.path.isdir(dest) and norm_url(git_origin(dest)) == norm_url(repo):
        if run(["git", "-C", dest, "pull"]):
            record(program, "pulled", "pulled " + dest)
        else:
            fail(program, "git pull", dest, "pull failed")
        return

    if not prompt_yn(
            "[{}] {} has other content. Back up and overwrite?".format(program, dest), False):
        record(program, "skipped", "overwrite declined")
        return
    ok, bak, err = backup(dest)
    if not ok:
        fail(program, "backup", dest, err)
        return
    print("[{}] backed up to {}".format(program, bak))
    git_clone(program, repo, dest, "cloned to {} (backed up old)".format(dest))


# mode = "symlink": clone into staging, then link into place.
def sync_symlink(entry, dest):
    program, repo = entry["program"], entry["repo"]
    staging = os.path.join(STAGING_ROOT, program)

    # 1. Bring the staging clone up to date.
    if not os.path.exists(staging):
        if not run(["git", "clone", repo, staging]):
            fail(program, "git clone", staging, "clone failed")
            return
    elif norm_url(git_origin(staging)) == norm_url(repo):
        if not run(["git", "-C", staging, "pull"]):
            fail(program, "git pull", staging, "pull failed")
            return
    else:
        fail(program, "staging", staging, "exists but origin does not match repo")
        return

    # 2. Determine the intended link target.
    source = entry.get("source")
    target = staging if source in (None, "all") else os.path.join(staging, source)

    # 3. Reconcile the link at dest.
    if os.path.islink(dest):
        if os.readlink(dest) == target:
            record(program, "unchanged", "symlink already correct")
            return
        try:
            os.remove(dest)
        except OSError as err:
            fail(program, "remove symlink", dest, str(err))
            return
        make_symlink(program, target, dest,
                     "symlink re-created {} -> {}".format(dest, target))
        return

    if not os.path.exists(dest):
        parent = os.path.dirname(dest)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as err:
                fail(program, "mkdir -p", parent, str(err))
                return
        make_symlink(program, target, dest,
                     "symlink created {} -> {}".format(dest, target))
        return

    if os.path.isdir(dest) and dir_empty(dest):
        try:
            os.rmdir(dest)
        except OSError as err:
            fail(program, "remove empty dir", dest, str(err))
            return
        make_symlink(program, target, dest,
                     "symlink created {} -> {}".format(dest, target))
        return

    if not prompt_yn(
            "[{}] {} has other content. Back up and replace with symlink?".format(
                program, dest), False):
        record(program, "skipped", "overwrite declined")
        return
    ok, bak, err = backup(dest)
    if not ok:
        fail(program, "backup", dest, err)
        return
    print("[{}] backed up to {}".format(program, bak))
    make_symlink(program, target, dest,
                 "symlink created {} -> {} (backed up old)".format(dest, target))


# ------------------------------------------------------------- per-entry flow


def process_entry(entry):
    program = entry.get("program", "?")

    mode = entry.get("mode")
    if mode not in ("direct", "symlink"):
        fail(program, "validate", program, "invalid mode: {}".format(mode))
        return

    dest = expand(entry["dest"])

    if not probe_known_paths(entry, dest):
        record(program, "skipped", "known-path declined")
        return

    if mode == "direct":
        sync_direct(entry, dest)
    else:
        sync_symlink(entry, dest)


# ------------------------------------------------------------------ summary


def print_summary():
    order = ["synced", "pulled", "unchanged", "skipped", "failed"]
    print("\n=== Summary ===")
    for outcome in order:
        for r in results:
            if r["outcome"] == outcome:
                detail = "  ({})".format(r["detail"]) if r["detail"] else ""
                print("  {:<10} {}{}".format(outcome, r["program"], detail))

    flagged = sum(1 for r in results if r["outcome"] in ("skipped", "failed"))
    if flagged:
        noun = "entry needs" if flagged == 1 else "entries need"
        print("\n{} {} attention (skipped/failed above).".format(flagged, noun))


# --------------------------------------------------------------------- main


def main():
    if not preflight():
        sys.exit(1)

    config_path = sys.argv[1] if len(sys.argv) > 1 \
        else expand("~/.config/config-sync/config.py")
    config, err = load_config(config_path)
    if config is None:
        sys.stderr.write("Failed to load config ({}): {}\n".format(config_path, err))
        sys.exit(1)

    for entry in config:
        process_entry(entry)

    print_summary()


if __name__ == "__main__":
    main()
