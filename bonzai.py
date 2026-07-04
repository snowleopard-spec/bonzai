#!/usr/bin/env python3
"""
bonzai.py — run this ON the droplet (after you SSH in).

Produces a single markdown file describing the box:
  1. Application roots (project layout at /root, /home, /opt, /srv, /var/www)
  2. Storage & memory (free vs used)
  3. Installed packages

Usage:
  python3 bonzai.py            # writes report to the current directory
  python3 bonzai.py /tmp       # writes report to /tmp

Notes:
  - Read-only. Run as root (or a sudo user) for the complete picture; as a
    plain user it silently skips paths it can't read.
  - Application-root trees are capped at depth 4. Cache/venv/hidden dirs are
    summarized as one line with file count + size, not listed out.
  - Stdlib only. No third-party packages, no `tree` binary required.
"""

import os
import shutil
import argparse
import subprocess
from datetime import datetime, timezone

APP_ROOTS = ["/root", "/home", "/opt", "/srv", "/var/www"]

# Dirs to summarize as "name/ (N files, SIZE)" instead of listing contents.
# Hidden entries (name starts with '.') get the same treatment automatically.
SUMMARIZE = {"node_modules", "__pycache__", "venv", "snap"}

APP_TREE_DEPTH = 4

IS_ROOT = (getattr(os, "geteuid", lambda: 1)() == 0)
SUDO = [] if IS_ROOT else (["sudo", "-n"] if shutil.which("sudo") else [])


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
def dir_stats(path):
    files = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
                files += 1
            except OSError:
                pass
    return files, total


def render_tree(root, max_depth=APP_TREE_DEPTH):
    """ASCII tree of `root`, capped at `max_depth`. Hidden dirs and dirs in
    SUMMARIZE are replaced by a one-line `name/ (N files, SIZE)` summary.
    Hidden files are omitted entirely."""
    root = os.path.abspath(root)
    lines = [root]

    def walk(path, prefix, depth):
        if depth >= max_depth:
            return
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except PermissionError:
            lines.append(prefix + "└── [permission denied]")
            return
        except OSError:
            return

        visible = []
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if not is_dir and e.name.startswith("."):
                continue
            visible.append((e, is_dir))

        for i, (e, is_dir) in enumerate(visible):
            last = (i == len(visible) - 1)
            connector = "└── " if last else "├── "
            if is_dir and (e.name.startswith(".") or e.name in SUMMARIZE):
                n, sz = dir_stats(e.path)
                lines.append(f"{prefix}{connector}{e.name}/  ({n:,} files, {human(sz)})")
                continue
            name = e.name + ("/" if is_dir else "")
            lines.append(prefix + connector + name)
            if is_dir:
                walk(e.path, prefix + ("    " if last else "│   "), depth + 1)

    walk(root, "", 0)
    return "\n".join(lines)


def find_venvs(roots, max_depth=6):
    skip = {"node_modules", "__pycache__", ".git", ".cache",
            ".mypy_cache", ".pytest_cache", "snap"}
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
            dirnames[:] = [d for d in dirnames if d not in skip]
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
def section_filesystem():
    out = [h(f"1. Application roots (depth {APP_TREE_DEPTH})")]
    for r in APP_ROOTS:
        if not os.path.isdir(r):
            continue
        out.append(f"\n**{r}**\n")
        out.append(block(render_tree(r)))
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
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outpath = os.path.join(os.path.abspath(args.outdir),
                           f"droplet-report-{ts}.md")

    hostname = run(["hostname"]) or "unknown-host"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    parts = [
        f"# Droplet report — {hostname} — {now_utc}\n",
        "> Read-only snapshot of app roots, storage, and installed packages.",
        section_filesystem(),
        section_storage(),
        section_packages(),
        "\n---\n_Generated by bonzai.py_",
    ]

    with open(outpath, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")

    print(f"Wrote: {outpath}")


if __name__ == "__main__":
    main()
