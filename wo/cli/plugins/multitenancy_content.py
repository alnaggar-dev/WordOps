"""Content release layout shared by deploy, cutover, init, create, status, health, and backups.

After cutover:

    <shared_root>/content/<YYYYMMDD-HHMMSS>-<commit7>/
        wp-content/{plugins,themes,mu-plugins,languages}
        baseline.json
        RELEASE                      JSON, see read_release_info()
    <shared_root>/content/current -> <release name>        relative link; only deploy switches it
    <shared_root>/wp-content -> content/current/wp-content  fixed link, set once
    <shared_root>/config/baseline.json -> ../content/current/baseline.json   fixed link, set once

Before cutover, <shared_root>/wp-content is a real folder and config/baseline.json is a real file.
Code that runs on both layouts must use these helpers instead of guessing.
"""

import json
import os
import re

CONTENT_DIR = 'content'
CURRENT_LINK = 'current'
RELEASE_FILE = 'RELEASE'
HISTORY_FILE = 'history.jsonl'
KEEP_RELEASES = 5
RELEASE_NAME_RE = re.compile(r'^\d{8}-\d{6}-(?:[0-9a-f]{7}|pre-cutover)$')


def content_root(shared_root):
    """Folder that holds all content releases."""
    return os.path.join(shared_root, CONTENT_DIR)


def current_link(shared_root):
    """Path of the content/current link."""
    return os.path.join(content_root(shared_root), CURRENT_LINK)


def is_cut_over(shared_root):
    """True when <shared_root>/wp-content is the fixed link into content/current."""
    return os.path.islink(os.path.join(shared_root, 'wp-content'))


def current_release(shared_root):
    """Absolute path of the live content release, or None when no release is live."""
    link = current_link(shared_root)
    if not os.path.islink(link):
        return None
    target = os.path.realpath(link)
    return target if os.path.isdir(target) else None


def list_releases(shared_root):
    """Release folder names, oldest first. Names sort by time because they start with it."""
    root = content_root(shared_root)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(
        name for name in names
        if RELEASE_NAME_RE.match(name) and os.path.isdir(os.path.join(root, name))
    )


def read_release_info(release_path):
    """Parse the RELEASE file of a content release. Returns {} when it is missing or invalid.

    Keys written by tools/deploy (and by cutover for the pre-cutover release):
    commit (40 hex or "pre-cutover"), short, branch, date (commit time, ISO 8601),
    built (ISO 8601), plugins {slug: version}, themes {slug: version}.
    """
    try:
        with open(os.path.join(release_path, RELEASE_FILE)) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
