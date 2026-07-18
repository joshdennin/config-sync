"""config-sync — inventory, health, and dotfiles-repo management for Arch/CachyOS.

scan   — discover config entries under home, classify them, print a report
health — read a saved `scan --json` inventory and print a Markdown
         checkhealth-style report (inspired by Neovim's :checkhealth)
tidy   — report (and with --move, perform) safe XDG relocations of a
         conservative "Tier 1" set of HOME config files into ~/.config
adopt  — write an editable plan of discovered configs (curated/extended/
         everything tier); with --apply, copy the plan's entries into the
         managed repo at ~/.config/config-sync/, write the manifest, git commit
link   — report (and with --apply, perform) symlinking of adopted configs from
         the repo back into place, backing up each original first
sync   — deploy a repo (typically cloned from another machine): report (and with
         --apply, perform) reconciling the symlinks with what's installed —
         symlink the configs whose program is present, and remove the symlink of
         one whose program is gone (restoring the original); --force links all
unlink — reverse `link`: report (and with --apply, perform) removing the
         symlinks and restoring the backed-up originals

scan flags:
  --json               emit the complete structured inventory to stdout
  --generated          show machine-generated entries in the listing
  --all                also scan the state and cache roots (implies --generated)
  --secrets            show secret-flagged entries in the listing
  --only-orphans       restrict the listing to orphan entries
  --min-relevance N    hide entries scoring below N from the listing (default 0)
  --root PATH          add an extra scan root, categorized "unknown" (repeatable)
  --config PATH        TOML config with the classification tables

health arguments:
  inventory            path to an inventory file written by `scan --json`

tidy flags:
  --move               move the safe candidates into ~/.config (default: report)

adopt flags:
  --select TIER        plan breadth: curated | extended | everything (default curated)
  --include NAME       restrict the plan to these programs/categories (repeatable)
  --exclude NAME       drop these programs/categories from the plan (repeatable)
  --plan PATH          plan file to write, then read with --apply
  --apply              build the repo from the (edited) plan
  --force              adopt into an already-populated (e.g. cloned) repo
  --config PATH        TOML classification config

link / unlink flags:
  --apply              perform the change (default: report the plan only)

sync flags:
  --apply              create the symlinks (default: report the plan only)
  --force              link every config, even ones whose program is absent here
  --config PATH        TOML config (default: the repo's captured copy)

`scan` and `health` never write, move, or delete anything; their only side
effects are filesystem reads and read-only `pacman` / `git` queries. `tidy` is
read-only unless given --move; `adopt` is read-only unless given --apply (it
only writes a plan file otherwise). Every write goes through the fsops
primitives, which refuse to overwrite or delete: `adopt` copies (originals are
left untouched) and `tidy --move` only relocates when the target is absent.
"""

import argparse
import json
import os
import shutil
import sys

from .inventory import (ConfigSyncError, build_inventory, config_home,
                        default_config_path, die, load_config, load_pacman_qq,
                        repo_config_path, repo_root, require_home, tilde)
from .report import REPORTERS
from .sync import (ADOPT_TIERS, LINK_STATUS, SYNC_STATUS, UNLINK_STATUS,
                   adopt_apply, adopt_candidates, adopt_plan_rows,
                   default_plan_path, ensure_repo_scaffold, link_apply,
                   link_report, link_survey, load_adopt_plan, load_manifest,
                   omitted_programs, sync_apply, sync_report, sync_survey,
                   tidy_move, tidy_report, tidy_survey, unlink_apply,
                   unlink_report, unlink_survey, write_adopt_plan)


def cmd_scan(args):
    home = require_home()
    if shutil.which("pacman") is None:
        die("pacman not found on PATH — this tool needs Arch's pacman for "
            "package-ownership queries (on Arch: sudo pacman -S pacman)")
    cfg = load_config(args.config or default_config_path())
    inv = build_inventory(args, os.path.abspath(home), cfg)
    print(REPORTERS["json" if args.json else "listing"](inv, args, cfg))
    return 0


def cmd_health(args):
    try:
        with open(args.inventory) as f:
            inv = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die(f"cannot read inventory {args.inventory!r}: {e}")
    if not isinstance(inv, dict) or not isinstance(inv.get("entries"), list):
        die(f"{args.inventory!r} is not a valid inventory "
            "(expected the {meta, entries} object written by `scan --json`)")
    sys.stdout.write(REPORTERS["health"](inv, args))
    return 0


def cmd_tidy(args):
    home = require_home()
    rows = tidy_survey(home, config_home(home))
    if args.move:
        tidy_move(rows)
    else:
        tidy_report(rows)
    return 0


def cmd_adopt(args):
    home = os.path.abspath(require_home())
    conf = config_home(home)
    cfg = load_config(args.config or default_config_path())
    plan_path = args.plan or default_plan_path(conf)
    if args.apply:
        result = adopt_apply(load_adopt_plan(plan_path), home, conf, cfg,
                             force=args.force)
        if result["copied"]:
            print(f"Adopted {len(result['copied'])} config(s) into {result['repo']}:")
            for p in result["copied"]:
                print(f"  + {p}")
            print("Committed." if result["committed"]
                  else "  (git commit skipped — set git user.name/email, then commit by hand)")
        if result["skipped"]:
            print(f"Skipped {len(result['skipped'])} (already adopted or unavailable):")
            for p in result["skipped"]:
                print(f"  - {p}")
        if not result["copied"] and not result["skipped"]:
            print("Nothing to adopt — no entries marked adopt = true in the plan.")
        return 0
    # Plan phase: scan, filter to the tier, write the editable plan.
    if shutil.which("pacman") is None:
        die("pacman not found on PATH — this tool needs Arch's pacman for "
            "package-ownership queries (on Arch: sudo pacman -S pacman)")
    ensure_repo_scaffold(conf)  # create the repo dir + capture config before writing
    scan_ns = argparse.Namespace(all=False, root=[])
    inv = build_inventory(scan_ns, home, cfg)
    cands = adopt_candidates(inv, args.select, args.include, args.exclude, conf)
    rows = adopt_plan_rows(cands, cfg, conf)
    omitted = omitted_programs(inv, rows)
    write_adopt_plan(plan_path, rows, args.select, tilde(repo_root(conf), home), omitted)
    print(f"Wrote {len(rows)} program(s) to {tilde(plan_path, home)} ({args.select} tier).")
    print(f"Edit the file, then run:  config-sync adopt --apply --plan {plan_path}")
    return 0


def cmd_link(args):
    home = os.path.abspath(require_home())
    conf = config_home(home)
    manifest = load_manifest(conf, home)
    if not manifest["entries"]:
        die("nothing to link — no adopted configs "
            "(run `config-sync adopt --apply` first)")
    if args.apply:
        result = link_apply(manifest, home, conf)
        for p in result["linked"]:
            print(f"  linked {tilde(p, home)}")
        for p, status in result["skipped"]:
            print(f"  skipped {tilde(p, home)} ({LINK_STATUS.get(status, ('', status))[1] or status})")
        if not result["linked"]:
            print("Nothing to link.")
    else:
        link_report(link_survey(manifest), home)
    return 0


def cmd_unlink(args):
    home = os.path.abspath(require_home())
    conf = config_home(home)
    manifest = load_manifest(conf, home)
    if not manifest["entries"]:
        die("nothing to unlink — no adopted configs")
    if args.apply:
        result = unlink_apply(manifest, home, conf)
        for p in result["restored"]:
            print(f"  restored {tilde(p, home)}")
        for p, status in result["skipped"]:
            print(f"  skipped {tilde(p, home)} "
                  f"({UNLINK_STATUS.get(status, ('', status))[1] or status})")
        if not result["restored"]:
            print("Nothing to unlink.")
    else:
        unlink_report(unlink_survey(manifest), home)
    return 0


def cmd_sync(args):
    home = os.path.abspath(require_home())
    conf = config_home(home)
    if shutil.which("pacman") is None:
        die("pacman not found on PATH — this tool needs Arch's pacman for "
            "package-ownership queries (on Arch: sudo pacman -S pacman)")
    # Prefer the repo's captured config (the registry it was built with) so a
    # freshly cloned repo validates correctly; fall back to the package copy.
    repo_cfg = repo_config_path(conf)
    cfg = load_config(args.config or
                      (repo_cfg if os.path.exists(repo_cfg) else default_config_path()))
    manifest = load_manifest(conf, home)
    if not manifest["entries"]:
        die("nothing to sync — no adopted configs in the repo "
            f"({tilde(repo_root(conf), home)}); clone or build it first")
    qq = load_pacman_qq()
    if args.apply:
        result = sync_apply(manifest, home, conf, qq, cfg, force=args.force)
        for p in result["linked"]:
            print(f"  linked {tilde(p, home)}")
        for p in result["unlinked"]:
            print(f"  unlinked {tilde(p, home)} (program no longer installed)")
        for p, status in result["skipped"]:
            print(f"  skipped {tilde(p, home)} "
                  f"({SYNC_STATUS.get(status, ('', status))[1] or status})")
        if not result["linked"] and not result["unlinked"]:
            print("Nothing to do — already in sync.")
    else:
        sync_report(sync_survey(manifest, qq, cfg), home)
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(prog="config-sync",
                                description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="inspect the system and emit an inventory")
    s.add_argument("--json", action="store_true",
                   help="emit the complete structured inventory (stdout)")
    s.add_argument("--generated", action="store_true",
                   help="show machine-generated entries in the listing")
    s.add_argument("--all", action="store_true",
                   help="also scan the state and cache roots (implies --generated)")
    s.add_argument("--secrets", action="store_true",
                   help="show secret-flagged entries in the listing")
    s.add_argument("--only-orphans", action="store_true",
                   help="restrict the listing to orphan entries")
    s.add_argument("--min-relevance", type=int, default=0, metavar="N",
                   help="hide entries scoring below N from the listing")
    s.add_argument("--root", action="append", default=[], metavar="PATH",
                   help="add an extra scan root (repeatable)")
    s.add_argument("--config", metavar="PATH",
                   help="TOML config with the classification tables "
                        "(default: inventory-config.toml next to the package)")
    s.set_defaults(func=cmd_scan)

    h = sub.add_parser("health",
                       help="render a Markdown health report for a saved "
                            "`scan --json` inventory")
    h.add_argument("inventory", help="inventory file written by `scan --json`")
    h.set_defaults(func=cmd_health)

    t = sub.add_parser("tidy",
                       help="report (and optionally perform) safe XDG "
                            "relocations of Tier 1 config files")
    t.add_argument("--move", action="store_true",
                   help="move the safe candidates into ~/.config (default: report only)")
    t.set_defaults(func=cmd_tidy)

    a = sub.add_parser("adopt",
                       help="generate an editable adopt plan (and with --apply, "
                            "build the dotfiles repo from it)")
    a.add_argument("--select", choices=list(ADOPT_TIERS), default="curated",
                   help="breadth of the generated plan (default: curated)")
    a.add_argument("--include", action="append", default=[], metavar="NAME",
                   help="restrict the plan to these programs/categories (repeatable)")
    a.add_argument("--exclude", action="append", default=[], metavar="NAME",
                   help="drop these programs/categories from the plan (repeatable)")
    a.add_argument("--plan", metavar="PATH",
                   help="plan file to write, then read back with --apply "
                        "(default: <repo>/config-sync-adopt.toml)")
    a.add_argument("--apply", action="store_true",
                   help="copy the plan's adopt=true entries into the repo, write "
                        "the manifest, and git commit")
    a.add_argument("--force", action="store_true",
                   help="adopt into a repo that already holds configs (e.g. one "
                        "cloned from another machine); off by default to protect "
                        "a shared repo")
    a.add_argument("--config", metavar="PATH",
                   help="TOML classification config (default: next to the package)")
    a.set_defaults(func=cmd_adopt)

    ln = sub.add_parser("link",
                        help="symlink adopted configs from the repo back into "
                             "place (backs up each original first)")
    ln.add_argument("--apply", action="store_true",
                    help="create the symlinks (default: report the plan only)")
    ln.set_defaults(func=cmd_link)

    sy = sub.add_parser("sync",
                        help="deploy a repo (e.g. cloned from another machine): "
                             "symlink the configs whose program is installed "
                             "here, and unlink any whose program is now gone")
    sy.add_argument("--apply", action="store_true",
                    help="create the symlinks (default: report the plan only)")
    sy.add_argument("--force", action="store_true",
                    help="link every config, even those whose program is not "
                         "installed here")
    sy.add_argument("--config", metavar="PATH",
                    help="TOML classification config (default: the repo's "
                         "captured copy, else next to the package)")
    sy.set_defaults(func=cmd_sync)

    ul = sub.add_parser("unlink",
                        help="reverse `link`: remove the symlinks and restore "
                             "the backed-up originals")
    ul.add_argument("--apply", action="store_true",
                    help="restore the originals (default: report the plan only)")
    ul.set_defaults(func=cmd_unlink)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return args.func(args)
    except ConfigSyncError as e:
        print(f"config-sync: error: {e}", file=sys.stderr)
        return 1
