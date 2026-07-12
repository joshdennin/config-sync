"""Reporters — read-only renderings of an inventory. All share the signature
(inv, args, cfg) -> str (some ignore args/cfg) so a format is selected by name
from REPORTERS; adding one is a single registry entry. The inventory stays a
plain JSON-serializable dict — that is the contract these consume.
"""

import json
from collections import Counter
from datetime import datetime

from .inventory import CAT_ORDER, Config, display_path


# --------------------------------------------------------------------------
# Human-readable listing

def shorten(path, rec):
    """Abbreviate the scanned home to ~ (derived from the record, so health
    output is right even for an inventory taken on another machine)."""
    rel = rec["rel"]
    if not rel.startswith("/") and rec["path"].endswith("/" + rel):
        home = rec["path"][:-len(rel) - 1]
        if path.startswith(home + "/"):
            return "~" + path[len(home):]
    return path


def git_token(git):
    if not git:
        return ""
    dirty = git.get("dirty") or {}
    tok = f"{git['name']}: {git['branch']}"
    if dirty.get("modified") or dirty.get("untracked"):
        tok += " ✎"
    if git.get("ahead"):
        tok += f" ↑{git['ahead']}"
    if git.get("behind"):
        tok += f"{'' if git.get('ahead') else ' '}↓{git['behind']}"
    return f"({tok})"


def badges(rec):
    out = list(rec["flags"])
    if rec["is_git_repo"]:
        out.append("git-repo")
    if rec["editable"] is False:
        out.append("generated")
    return out


def is_visible(rec, args):
    if args.only_orphans and rec["installed"] is not False:
        return False
    if "dangling" in rec["flags"]:
        return True  # always a cleanup signal
    if "secret" in rec["flags"] and not args.secrets:
        return False
    if rec["editable"] is False and not (args.generated or args.all):
        return False
    return (rec["relevance"] or 0) >= args.min_relevance


def report_listing(inv, args, cfg=None):
    entries = inv["entries"]
    shown = [e for e in entries if is_visible(e, args)]
    lines = []
    for cat in CAT_ORDER:
        group = sorted((e for e in shown if e["location"] == cat),
                       key=lambda e: (-(e["relevance"] or 0), e["rel"]))
        if not group:
            continue
        lines.append(cat.upper())
        for e in group:
            prog = e["program"] or "?"
            if e["installed"] is True:
                prog += " [installed]"
            elif e["installed"] is False:
                prog += " [orphan]"
            parts = [f"{e['relevance'] if e['relevance'] is not None else '-':>4}",
                     f"{display_path(e):<44}", f"{e['kind'] or '?':<5}", prog]
            if badges(e):
                parts.append(" ".join(badges(e)))
            if e["git"]:
                parts.append(git_token(e["git"]))
            lines.append("  " + "  ".join(parts))
        lines.append("")

    cats = Counter(e["location"] for e in shown)
    orphans = sum(1 for e in entries if e["installed"] is False)
    secrets = sum(1 for e in entries if "secret" in e["flags"])
    repos = len({e["git"]["root"] for e in entries if e["git"]})
    parts = [f"Summary: {len(shown)} shown / {len(entries)} recorded",
             *(f"{c} {n}" for c, n in cats.items()),
             f"orphans {orphans}", f"secrets {secrets}", f"git repos {repos}"]
    lines.append(" · ".join(parts))
    hidden = len(entries) - len(shown)
    if hidden:
        lines.append(f"{hidden} hidden from listing (show with --generated / --secrets; "
                     f"--json is always complete)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Health (pure functions of stored records)

def section_key(rec, cfg):
    prog = rec["program"]
    if not prog:
        return None
    return cfg.programs.get(prog, {}).get("bin", prog)  # command name reads best


def section_findings(recs):
    """Findings for one program's records: [(severity, text, suggestion|None)]."""
    f = []
    prog = recs[0]["program"]
    present = [r for r in recs if "dangling" not in r["flags"]]
    # Location checks apply to config proper, not a program's data/state dirs
    # that happen to attribute to it.
    conf = [r for r in present if r["location"] in ("config", "shell", "home", "unknown")]

    if prog:
        if any(r["installed"] for r in recs):
            f.append(("OK", f"program installed ({prog})", None))
        elif any(r["installed"] is False for r in recs):
            f.append(("WARN", "config present but program not found (pacman or PATH)",
                      f"likely stale; verify before removing {display_path(recs[0])}"))

    for r in conf:
        via = ""
        if r["via_symlink"]:
            links = ", ".join(shorten(l, r) for l in r["via_symlink"])
            via = f"  (via {links} → symlink)"
        f.append(("OK", f"config at {display_path(r)}{via}", None))
    if prog and len(conf) > 1:
        f.append(("WARN", f"config present at {len(conf)} known paths at once", None))
    for r in recs:
        if "dangling" in r["flags"]:
            f.append(("ERROR", f"dangling symlink: {display_path(r)} (target missing)",
                      f"broken link; verify and clean up {display_path(r)}"))

    for r in {r["git"]["root"]: r for r in recs if r["git"]}.values():
        git = r["git"]
        root = shorten(git["root"], r)
        f.append(("OK", f"git: {git['name']} @ {git['branch']}", None))
        dirty = git.get("dirty") or {}
        n = (dirty.get("modified") or 0) + (dirty.get("untracked") or 0)
        if n:
            plus = "+" if dirty.get("saturated") else ""
            f.append(("WARN", f"git: {n}{plus} uncommitted change{'s' if n != 1 else ''}"
                      " in the working tree", f"git -C {root} status"))
        ahead, behind = git.get("ahead"), git.get("behind")
        if ahead and behind:
            f.append(("WARN", f"git: diverged from {git['upstream']} "
                      f"({ahead} ahead / {behind} behind)", f"git -C {root} status"))
        elif ahead:
            f.append(("WARN", f"git: {ahead} commit{'s' if ahead != 1 else ''} ahead of "
                      f"{git['upstream']} (unpushed)", f"git -C {root} push"))
        elif behind:
            f.append(("WARN", f"git: {behind} commit{'s' if behind != 1 else ''} behind "
                      f"{git['upstream']} (needs pull)", f"git -C {root} pull"))
        if git["branch"] == "(detached)":
            f.append(("WARN", "git: detached HEAD", None))
        elif git.get("vs_default") and not git["vs_default"]["is_default"]:
            f.append(("INFO", f"git: on non-default branch ({git['branch']}, "
                      f"default {git['default_branch']})", None))
        if not git.get("upstream") or not git.get("remotes"):
            f.append(("INFO", "git: no remote/upstream configured", None))

    candidates = [r for r in conf if r["editable"]]
    if candidates and not any(r["is_git_repo"] for r in candidates):
        f.append(("INFO", "not under version control — candidate for a dotfiles repo",
                  None))
    if any("secret" in r["flags"] for r in recs):
        f.append(("WARN", "contains secrets — do not sync to a public repo", None))
    return f


# Severity → Markdown bullet icon, echoing Neovim's :checkhealth glyphs.
SEV_ICON = {"OK": "✅", "INFO": "ℹ️", "WARN": "⚠️", "ERROR": "❌"}

# Command-shaped suggestions get wrapped in inline code; prose is left plain.
_CMD_PREFIXES = ("git ", "pacman ", "rm ", "ln ", "cp ", "mv ")


def md_suggestion(text):
    return f"`{text}`" if text.startswith(_CMD_PREFIXES) else text


UNCATEGORIZED = "Uncategorized"  # health group for programs with no category


def render_health(inv, source, cfg=Config()):
    sections = {}  # section title -> records, in inventory order; unattributed last
    for rec in inv["entries"]:
        sections.setdefault(section_key(rec, cfg) or "unattributed", []).append(rec)
    if "unattributed" in sections:
        sections["unattributed"] = sections.pop("unattributed")

    # Group each program's section under its category (baked into the entries at
    # scan time, so this works from the stored inventory alone). Findings are
    # computed up front so the summary can lead the document.
    groups = {}  # category -> [(section title, findings)], first-appearance order
    totals = Counter()
    attention = []
    programs = 0
    for name, recs in sections.items():
        findings = section_findings(recs)
        cat = (name != "unattributed" and recs[0].get("category")) or UNCATEGORIZED
        groups.setdefault(cat, []).append((name, findings))
        for sev, _, _ in findings:
            totals[sev] += 1
        bad = [t for s, t, _ in findings if s in ("WARN", "ERROR")]
        if bad:
            attention.append((name, "; ".join(t.removeprefix("git: ") for t in bad)))
        if name != "unattributed":
            programs += 1
    # Uncategorized (and the unattributed section it holds) always sorts last.
    ordered_cats = ([c for c in groups if c != UNCATEGORIZED]
                    + ([UNCATEGORIZED] if UNCATEGORIZED in groups else []))

    meta = inv.get("meta", {})
    out = [
        "# config inventory — health check",
        "",
        f"`{meta.get('host', '?')}` · source `{source}` · "
        f"scanned `{meta.get('scanned_at', '?')}` · "
        f"checked `{datetime.now():%Y-%m-%d %H:%M}`",
        "",
        "## Summary",
        "",
        f"**{programs} program{'' if programs == 1 else 's'} checked** — "
        f"{SEV_ICON['OK']} {totals['OK']} OK · "
        f"{SEV_ICON['WARN']} {totals['WARN']} WARN · "
        f"{SEV_ICON['ERROR']} {totals['ERROR']} ERROR · "
        f"{SEV_ICON['INFO']} {totals['INFO']} INFO",
        "",
    ]
    if attention:
        out += ["### Needs attention", ""]
        out += [f"- **{n}** — {msg}" for n, msg in attention]
        out.append("")

    for cat in ordered_cats:
        out += [f"# {cat}", ""]
        for name, findings in groups[cat]:
            out += [f"## {name}", ""]
            for sev, text, suggestion in findings:
                out.append(f"- {SEV_ICON[sev]} {text}")
                if suggestion:
                    out.append(f"  - ↳ {md_suggestion(suggestion)}")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# Registry — select a reporter by name.

def report_json(inv, args=None, cfg=None):
    return json.dumps(inv, indent=2)


def report_health(inv, args, cfg=Config()):
    return render_health(inv, getattr(args, "inventory", "?"), cfg)


REPORTERS = {
    "listing": report_listing,   # scan default: the human-readable table
    "json": report_json,         # scan --json: the complete structured inventory
    "health": report_health,     # health: the Markdown checkhealth report
}
