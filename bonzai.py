#!/usr/bin/env python3
"""
droplet-report.py — run this ON the droplet (after you SSH in).

Produces a single markdown file describing the box:
  1. Filesystem / folder structure
  2. Storage & memory (free vs used)
  3. Installed packages

Usage:
  python3 droplet-report.py                 # writes to the current directory
  python3 droplet-report.py /tmp            # writes to /tmp
  python3 droplet-report.py --depth 4       # deeper whole-system tree (default 3)
  python3 droplet-report.py --full          # exhaustive tree of / (big!)

  DEPTH and FULL environment variables are also honoured, e.g. DEPTH=4 FULL=1.

Notes:
  - Read-only. Run as root (or a sudo user) for the complete picture; as a
    plain user it silently skips paths it can't read, giving a thinner tree.
  - The whole-system tree is pruned (pseudo-filesystems + noise dirs) and
    depth-limited so the output stays feedable to an LLM. The app roots are
    shown in full depth. Use --full to override (can be very large).
  - Stdlib only. No third-party packages, no `tree` binary required.
"""

import os
import sys
import shutil
import argparse
import subprocess
from datetime import datetime, timezone

APP_ROOTS = ["/root", "/home", "/opt", "/srv", "/var/www"]
NOISE = {"node_modules", "__pycache__", ".git", ".venv", "venv",
         ".cache", "snap", ".mypy_cache", ".pytest_cache"}
PSEUDO_FS = {"proc", "sys", "dev", "run"}

IS_ROOT = (getattr(os, "geteuid", lambda: 1)() == 0)
SUDO = [] if IS_ROOT else (["sudo", "-n"] if shutil.which("sudo") else [])

sys.setrecursionlimit(20000)  # deep trees (e.g. --full over /usr)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def run(cmd, use_sudo=False, timeout=120):
    """Run a command (list of args), return stdout as text. Never raises."""
    full = (SUDO + cmd) if (use_sudo and not IS_ROOT) else cmd
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").rstrip()
        if not out and r.returncode != 0:
            return f"({' '.join(cmd)} returned nothing / not permitted)"
        return out
    except FileNotFoundError:
        return f"({cmd[0]} not installed)"
    except subprocess.TimeoutExpired:
        return f"({cmd[0]} timed out)"
    except Exception as e:  # noqa: BLE001
        return f"(error running {' '.join(cmd)}: {e})"


def human(n):
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}P"


def h(title):
    return f"\n## {title}\n"


def s(title):
    return f"\n### {title}\n"


def block(text):
    return "```\n" + (text.rstrip() if text else "(empty)") + "\n```"


# --------------------------------------------------------------------------- #
# filesystem tree (native, no `tree` binary needed)
# --------------------------------------------------------------------------- #
def render_tree(root, max_depth=None, exclude=frozenset()):
    """Return an ASCII tree of `root`, excluding any entry whose basename is in
    `exclude`, limited to `max_depth` levels below root (None = unlimited)."""
    root = os.path.abspath(root)
    lines = [root]

    def walk(path, prefix, depth):
        if max_depth is not None and depth >= max_depth:
            return
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except PermissionError:
            lines.append(prefix + "└── [permission denied]")
            return
        except OSError:
            return
        entries = [e for e in entries if e.name not in exclude]
        for i, e in enumerate(entries):
            last = (i == len(entries) - 1)
            connector = "└── " if last else "├── "
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            name = e.name + ("/" if is_dir else "")
            lines.append(prefix + connector + name)
            if is_dir:
                walk(e.path, prefix + ("    " if last else "│   "), depth + 1)

    walk(root, "", 0)
    return "\n".join(lines)


def find_venvs(roots, max_depth=6):
    venvs = set()
    for r in roots:
        if not os.path.isdir(r):
            continue
        base = r.rstrip("/").count("/")
        for dirpath, dirnames, filenames in os.walk(r):
            depth = dirpath.count("/") - base
            if depth > max_depth:
                dirnames[:] = []
                continue
            if os.path.basename(dirpath) == "bin" and "activate" in filenames:
                venvs.add(os.path.dirname(dirpath))
                dirnames[:] = []          # don't descend into the venv
                continue
            dirnames[:] = [d for d in dirnames if d not in NOISE]
    return sorted(venvs)


def largest_dirs(roots, n=20):
    existing = [r for r in roots if os.path.isdir(r)]
    if not existing:
        return "(no app roots present)"
    out = run(["du", "-x", "--max-depth=1", "-b", *existing], use_sudo=True)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1]))
    rows.sort(reverse=True)
    if not rows:
        return out or "(none)"
    return "\n".join(f"{human(sz):>8}  {path}" for sz, path in rows[:n])


# --------------------------------------------------------------------------- #
# report sections
# --------------------------------------------------------------------------- #
def section_filesystem(depth, full):
    out = [h("1. Filesystem structure")]

    if full:
        out.append(s("Whole-system tree (full)"))
        out.append(block(render_tree("/", max_depth=None, exclude=PSEUDO_FS)))
    else:
        out.append(s(f"Whole-system map (depth {depth})"))
        out.append(block(render_tree("/", max_depth=depth,
                                      exclude=PSEUDO_FS | NOISE)))

    out.append(s("Application roots (full depth)"))
    for r in APP_ROOTS:
        if not os.path.isdir(r):
            continue
        out.append(f"\n**{r}**\n")
        out.append(block(render_tree(r, max_depth=None, exclude=NOISE)))
    return "\n".join(out)


def section_storage():
    out = [h("2. Storage & memory")]
    out.append(s("Disk usage (free vs used per mount)"))
    out.append(block(run(["df", "-hT", "-x", "tmpfs", "-x", "devtmpfs"])))
    out.append(s("RAM & swap"))
    out.append(block(run(["free", "-h"])))
    out.append(s("Largest directories under the app roots"))
    out.append(block(largest_dirs(APP_ROOTS)))
    return "\n".join(out)


def section_packages():
    out = [h("3. Installed packages")]

    out.append(s("APT (manually installed)"))
    if shutil.which("apt-mark"):
        manual = run(["apt-mark", "showmanual"])
        manual = "\n".join(sorted(manual.splitlines()))
        total = run(["dpkg-query", "-f", ".", "-W"])
        count = len(total) if total and not total.startswith("(") else "?"
        out.append(block(f"{manual}\n\ntotal dpkg packages: {count}"))
    else:
        out.append(block("(apt not present)"))

    if shutil.which("snap"):
        out.append(s("Snap"))
        out.append(block(run(["snap", "list"])))

    if shutil.which("npm"):
        out.append(s("npm (global)"))
        out.append(block(run(["npm", "ls", "-g", "--depth=0"])))

    out.append(s("Python (system pip)"))
    if shutil.which("pip3"):
        out.append(block(run(["pip3", "freeze"])))
    elif shutil.which("pip"):
        out.append(block(run(["pip", "freeze"])))
    else:
        out.append(block("(no system pip)"))

    out.append(s("Python virtualenvs"))
    venvs = find_venvs(["/root", "/home", "/opt", "/srv"])
    if not venvs:
        out.append(block("(none found)"))
    for v in venvs:
        py = os.path.join(v, "bin", "python")
        pip = os.path.join(v, "bin", "pip")
        ver = run([py, "--version"]) if os.path.exists(py) else "(no python)"
        freeze = run([pip, "freeze"]) if os.path.exists(pip) else "(no pip)"
        out.append(f"\n**{v}**\n")
        out.append(block(f"{ver}\n--- pip freeze ---\n{freeze}"))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Generate a markdown report of this host.")
    ap.add_argument("outdir", nargs="?", default=os.getcwd(),
                    help="directory to write the report into (default: cwd)")
    ap.add_argument("--depth", type=int,
                    default=int(os.environ.get("DEPTH", "3")),
                    help="depth of the whole-system tree map (default 3)")
    ap.add_argument("--full", action="store_true",
                    default=os.environ.get("FULL", "0") == "1",
                    help="exhaustive tree of / (can be very large)")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outpath = os.path.join(os.path.abspath(args.outdir),
                           f"droplet-report-{ts}.md")

    hostname = run(["hostname"]) or "unknown-host"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    parts = [
        f"# Droplet report — {hostname} — {now_utc}\n",
        "> Read-only snapshot of filesystem, storage, and installed packages.",
        section_filesystem(args.depth, args.full),
        section_storage(),
        section_packages(),
        "\n---\n_Generated by droplet-report.py_",
    ]

    with open(outpath, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")

    print(f"Wrote: {outpath}")


if __name__ == "__main__":
    main()
