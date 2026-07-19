"""Shared fixtures for the config_sync test suite."""

import argparse

from config_sync import inventory

# Config root the helper computes `adoptable` against — matches the /h paths the
# record fixtures use, so repo_root is /h/.config/config-sync.
_HELPER_CONF = "/h/.config"


def scan_args(**kw):
    defaults = dict(json=False, generated=False, all=False, secrets=False,
                    only_orphans=False, min_relevance=0, root=[], config=None)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def rec(**kw):
    base = {"path": "/h/.config/x", "rel": ".config/x", "location": "config",
            "kind": "dir", "via_symlink": None, "size": 1, "mtime": None,
            "editable": True, "adoptable": False, "is_git_repo": False,
            "git": None, "program": None, "category": None, "installed": None,
            "flags": [], "relevance": 50, "relevance_terms": []}
    base.update(kw)
    # Mirror the scan: derive adoptable from the record's own fields unless a test
    # pins it explicitly, so fixtures stay in step with safe_to_adopt.
    if "adoptable" not in kw:
        base["adoptable"] = inventory.safe_to_adopt(
            base["location"], base["flags"], base["editable"], base["path"],
            _HELPER_CONF)
    return base


