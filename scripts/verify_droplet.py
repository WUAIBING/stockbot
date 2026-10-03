#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Does the live server run exactly what master says? Read-only.

Every push to master rsyncs the repository to /opt/stockbot (sync-do-repo.yml),
but files were also hand-installed during September, and a stacked merge
deployed intermediate states of master for minutes at a time. This compares
the committed content of every file the sync covers against the server's copy
and lists anything different or missing. It changes nothing.

    python scripts/verify_droplet.py            # against HEAD
    python scripts/verify_droplet.py origin/master
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

HOST = "do"
TARGET = "/opt/stockbot"
# Same exclusions as scripts/github-actions/sync_repo_to_do.sh: these never sync.
EXCLUDED_PREFIXES = (
    ".git/", ".venv/", "workbuddy/a-share-analyst/", "workbuddy/skills/a-share-analyst/task_wrappers/",
    "workbuddy_pool/", "workbuddy_distill/raw_top100/", "workbuddy_distill/evaluations/",
    "workbuddy_distill/artifacts/", "workbuddy_distill/templates/",
)
EXCLUDED_NAMES = (".mx_apikey",)


def committed_files(rev):
    # -z: raw paths. Without it git quotes non-ASCII names and they would never
    # match a file on the server.
    raw = subprocess.run(["git", "ls-tree", "-r", "-z", "--name-only", rev], capture_output=True,
                         check=True).stdout.decode("utf-8")
    out = [p for p in raw.split("\0") if p]
    return [p for p in out if not p.startswith(EXCLUDED_PREFIXES)
            and p.rsplit("/", 1)[-1] not in EXCLUDED_NAMES and "__pycache__/" not in p]


def committed_hashes(rev, paths):
    """sha256 of each file's committed bytes, via one git cat-file process."""
    proc = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    hashes = {}
    for p in paths:
        proc.stdin.write(("%s:%s\n" % (rev, p)).encode("utf-8"))
        proc.stdin.flush()
        header = proc.stdout.readline().decode("utf-8").split()
        if len(header) < 3 or header[1] != "blob":
            continue
        body = proc.stdout.read(int(header[2]))
        proc.stdout.read(1)
        hashes[p] = hashlib.sha256(body).hexdigest()
    proc.stdin.close()
    proc.wait()
    return hashes


def server_hashes(paths):
    script = "cd %s && while IFS= read -r f; do if [ -f \"$f\" ]; then sha256sum \"$f\"; else echo \"MISSING  $f\"; fi; done" % TARGET
    # Bytes, not text: on Windows a text-mode pipe turns "\n" into "\r\n" and
    # every filename arrives with a stray \r - which made the first run report
    # all 185 files missing.
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, script],
                         input=("\n".join(paths) + "\n").encode("utf-8"),
                         capture_output=True, check=True).stdout.decode("utf-8")
    result = {}
    for line in out.splitlines():
        digest, _, path = line.partition("  ")
        result[path.strip()] = None if digest == "MISSING" else digest
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    rev = argv[0] if argv else "HEAD"
    sha = subprocess.run(["git", "rev-parse", "--short", rev], capture_output=True, text=True,
                         check=True).stdout.strip()
    paths = committed_files(rev)
    mine = committed_hashes(rev, paths)
    theirs = server_hashes(list(mine))
    recorded = subprocess.run(["ssh", "-o", "BatchMode=yes", HOST,
                               "cat %s/.stockbot-sync-source-sha 2>/dev/null" % TARGET],
                              capture_output=True, text=True).stdout.strip()[:7]
    differ = sorted(p for p, h in mine.items() if theirs.get(p) and theirs[p] != h)
    missing = sorted(p for p, h in mine.items() if theirs.get(p, "x") is None)
    print("revision %s (%s) | server last synced %s | files compared %d" % (rev, sha, recorded or "?", len(mine)))
    print("  match %d | differ %d | missing on server %d" % (len(mine) - len(differ) - len(missing),
                                                            len(differ), len(missing)))
    for p in differ:
        print("  DIFFER   %s" % p)
    for p in missing:
        print("  MISSING  %s" % p)
    return 1 if (differ or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
