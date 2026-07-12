"""Shared fixtures for the configsync test suite."""

import argparse


def scan_args(**kw):
    defaults = dict(json=False, generated=False, all=False, secrets=False,
                    only_orphans=False, min_relevance=0, root=[], config=None)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def rec(**kw):
    base = {"path": "/h/.config/x", "rel": ".config/x", "location": "config",
            "kind": "dir", "via_symlink": None, "size": 1, "mtime": None,
            "editable": True, "is_git_repo": False, "git": None,
            "program": None, "category": None, "installed": None, "flags": [],
            "relevance": 50, "relevance_terms": []}
    base.update(kw)
    return base


