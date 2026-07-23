#!/usr/bin/env python3
"""
bonzai.py — run this ON the droplet (after you SSH in).

Produces a single markdown file describing the box:
  1. Host (OS, kernel, CPU, RAM, uptime, IPs, timezone)
  2. Application roots (project layout at /root, /home, /opt, /srv, /var/www)
  3. Storage & memory (free vs used)
  4. Installed packages
  5. Scheduled tasks (cron + user-defined systemd timers)
  6. Live services (running units + listening ports)
  7. Web-server config (nginx sites-enabled, Caddyfile)

Usage:
  python3 bonzai.py            # writes report to the current directory
  python3 bonzai.py /tmp       # writes report to /tmp

Notes:
  - Read-only. Run as root (or a sudo user) for the complete picture; as a
    plain user it silently skips paths it can't read.
  - Application-root trees are capped at depth 4. Cache/venv/hidden dirs are
    summarized as one line with file count + size, not listed out.
  - Final markdown passes through a secret-redaction filter (Bearer tokens,
    PEM blocks, AWS/GitHub/OpenAI/Anthropic/Slack keys, JWTs, URL creds,
    env-var assignments to *_PASSWORD/*_SECRET/*_TOKEN/*_KEY). A count of
    redactions appears in a banner at the top of the report if any fire.
  - Stdlib only. No third-party packages, no `tree` binary required.
"""

import os
import re
import glob
import gzip
import shlex
import shutil
import argparse
import subprocess
from datetime import datetime, timezone

APP_ROOTS = ["/root", "/home", "/opt", "/srv", "/var/www"]

# Dirs to summarize as "name/ (N files, SIZE)" instead of listing contents.
# Hidden entries (name starts with '.') get the same treatment automatically.
SUMMARIZE = {"node_modules", "__pycache__", "venv", "snap"}

APP_TREE_DEPTH = 4
MAX_ENTRIES_PER_DIR = 100  # dirs with more children than this get summarized

IS_ROOT = (getattr(os, "geteuid", lambda: 1)() == 0)
SUDO = [] if IS_ROOT else (["sudo", "-n"] if shutil.which("sudo") else [])

# Anchored patterns only (no long-random-string heuristics — those false-positive
# on package hashes / version pins). Order matters: greedy multi-line first,
# then prefix-anchored, then context-anchored.
REDACTION_PATTERNS = [
    ("pem-block",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
     "<REDACTED-PEM-BLOCK>"),
    ("aws-access-key-id",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "<REDACTED-AWS-KEY-ID>"),
    ("github-token",
     re.compile(r"\b(?:ghp|gho|ghs|ghr|ghu)_[A-Za-z0-9_]{20,}\b"),
     "<REDACTED-GH-TOKEN>"),
    ("github-pat",
     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
     "<REDACTED-GH-PAT>"),
    ("anthropic-key",
     re.compile(r"\bsk-ant-[A-Za-z0-9_-]{40,}\b"),
     "<REDACTED-ANTHROPIC-KEY>"),
    ("openai-key",
     re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b"),
     "<REDACTED-OPENAI-KEY>"),
    ("slack-token",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
     "<REDACTED-SLACK-TOKEN>"),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
     "<REDACTED-JWT>"),
    ("bearer",
     re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})"),
     r"\1<REDACTED>"),
    ("basic-auth",
     re.compile(r"(?i)(Basic\s+)([A-Za-z0-9+/=]{12,})"),
     r"\1<REDACTED>"),
    ("url-credential",  # scheme://user:password@host
     re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)"),
     r"\1<REDACTED>\3"),
    ("url-query-secret",  # ?token=... / ?api_key=... / etc.
     re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret|password|passwd|pwd|auth)=)([^&\s\"']{6,})"),
     r"\1<REDACTED>"),
    ("env-assignment",  # PASSWORD=... / API_KEY=... / SECRET=... etc.
     re.compile(r"(?im)^(\s*(?:export\s+)?\w*(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|AUTH)\s*=\s*)[\"']?([^\"'\n]{4,})[\"']?"),
     r"\1<REDACTED>"),
]


def redact(text):
    """Run all redaction patterns over `text`. Returns (new_text, counts_dict)."""
    counts = {}
    for name, pat, repl in REDACTION_PATTERNS:
        text, n = pat.subn(repl, text)
        if n:
            counts[name] = n
    return text, counts


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


def child_count(path):
    try:
        return sum(1 for _ in os.scandir(path))
    except OSError:
        return 0


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
            summarize_dir = is_dir and (
                e.name.startswith(".")
                or e.name in SUMMARIZE
                or child_count(e.path) > MAX_ENTRIES_PER_DIR
            )
            if summarize_dir:
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
# cron file reading
# --------------------------------------------------------------------------- #
def _read_maybe_sudo(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except PermissionError:
        return run(["cat", path], use_sudo=True)
    except OSError as e:
        return f"(cannot read {path}: {e})"


# --------------------------------------------------------------------------- #
# report sections
# --------------------------------------------------------------------------- #
def _os_pretty_name():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "(unknown)"


def _cpu_info():
    model = None
    cores = 0
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name") and not model:
                    model = line.split(":", 1)[1].strip()
                elif line.startswith("processor"):
                    cores += 1
    except OSError:
        pass
    return model or "(unknown)", cores or "?"


def _mem_total():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return f"{int(line.split()[1]) / 1024 / 1024:.1f} GiB"
    except OSError:
        pass
    return "(unknown)"


def _timezone():
    try:
        with open("/etc/timezone") as f:
            tz = f.read().strip()
            if tz:
                return tz
    except OSError:
        pass
    return run(["timedatectl", "show", "-p", "Timezone", "--value"]) or "(unknown)"


def section_host():
    hostname = run(["hostname"]) or "unknown"
    model, cores = _cpu_info()
    ips = run(["hostname", "-I"]) or "(unknown)"
    lines = [
        f"Host:     {hostname}",
        f"OS:       {_os_pretty_name()}",
        f"Kernel:   {run(['uname', '-sr'])}",
        f"Arch:     {run(['uname', '-m'])}",
        f"CPU:      {model}  ({cores} cores)",
        f"Memory:   {_mem_total()}",
        f"Uptime:   {run(['uptime', '-p'])}",
        f"IPs:      {ips}",
        f"Timezone: {_timezone()}",
    ]
    return h("1. Host") + "\n" + block("\n".join(lines))


def _root_one_liner(root):
    """If `root` is trivially empty or contains only the default nginx welcome
    page, return a short summary. Otherwise return None (caller renders tree)."""
    files, _ = dir_stats(root)
    if files == 0:
        return "(empty)"
    if files == 1:
        for _dp, _dn, fn in os.walk(root, onerror=lambda e: None):
            if "index.nginx-debian.html" in fn:
                return "(default nginx welcome page only)"
    return None


def section_filesystem():
    out = [h(f"2. Application roots (depth {APP_TREE_DEPTH})")]
    for r in APP_ROOTS:
        if not os.path.isdir(r):
            continue
        summary = _root_one_liner(r)
        if summary:
            out.append(f"\n**{r}** — {summary}")
        else:
            out.append(f"\n**{r}**\n")
            out.append(block(render_tree(r)))
    return "\n".join(out)


def section_storage():
    out = [h("3. Storage & memory")]
    out.append(s("Disk usage (free vs used per mount)"))
    out.append(block(run(["df", "-hT", "-x", "tmpfs", "-x", "devtmpfs"])))
    out.append(s("RAM & swap"))
    out.append(block(run(["free", "-h"])))
    out.append(s("Largest directories under the app roots"))
    out.append(block(largest_dirs(APP_ROOTS)))
    return "\n".join(out)


def _apt_history_installs():
    """Packages a human explicitly installed via `apt install ...`, parsed
    from /var/log/apt/history.log*. Returns sorted list of names."""
    pkgs = set()
    for path in sorted(glob.glob("/var/log/apt/history.log*")):
        try:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except (OSError, PermissionError):
            continue
        for line in text.splitlines():
            if not line.startswith("Commandline: "):
                continue
            try:
                tokens = shlex.split(line[len("Commandline: "):])
            except ValueError:
                tokens = line[len("Commandline: "):].split()
            if not tokens:
                continue
            if os.path.basename(tokens[0]) not in ("apt", "apt-get", "aptitude"):
                continue
            try:
                i = tokens.index("install")
            except ValueError:
                continue
            for t in tokens[i + 1:]:
                if t.startswith("-"):
                    continue
                # strip version pin (foo=1.2) or release suffix (foo/jammy)
                name = t.split("=", 1)[0].split("/", 1)[0]
                if name:
                    pkgs.add(name)
    return sorted(pkgs)


def section_packages():
    out = [h("4. Installed packages")]

    out.append(s("APT (installed via `apt install`)"))
    installs = _apt_history_installs()
    total = run(["dpkg-query", "-f", ".", "-W"]) if shutil.which("dpkg-query") else ""
    count = len(total) if total and not total.startswith("(") else "?"
    if installs:
        out.append(block("\n".join(installs) + f"\n\ntotal dpkg packages: {count}"))
    elif shutil.which("apt-mark"):
        # Fallback: history.log unavailable — use apt-mark showmanual
        manual = run(["apt-mark", "showmanual"])
        manual = "\n".join(sorted(manual.splitlines()))
        out.append(block(f"(apt history unavailable; showing apt-mark showmanual)\n\n{manual}\n\ntotal dpkg packages: {count}"))
    else:
        out.append(block("(apt not present)"))

    if shutil.which("snap"):
        out.append(s("Snap"))
        out.append(block(run(["snap", "list"])))

    if shutil.which("npm"):
        out.append(s("npm (global)"))
        out.append(block(run(["npm", "ls", "-g", "--depth=0"])))

    out.append(s("Python virtualenvs"))
    venvs = find_venvs(["/root", "/home", "/opt", "/srv"])
    if not venvs:
        out.append(block("(none found)"))
    for v in venvs:
        py = os.path.join(v, "bin", "python")
        pip = os.path.join(v, "bin", "pip")
        ver = run([py, "--version"]) if os.path.exists(py) else "(no python)"
        if os.path.exists(pip):
            top = run([pip, "list", "--not-required", "--format=freeze"])
        else:
            top = "(no pip)"
        out.append(f"\n**{v}**\n")
        out.append(block(f"{ver}\n--- top-level packages ---\n{top}"))
    return "\n".join(out)


def section_cron():
    out = [h("5. Scheduled tasks")]

    if os.path.exists("/etc/crontab"):
        out.append(s("/etc/crontab"))
        out.append(block(_read_maybe_sudo("/etc/crontab")))

    cron_d = "/etc/cron.d"
    if os.path.isdir(cron_d):
        try:
            files = sorted(f for f in os.listdir(cron_d)
                           if os.path.isfile(os.path.join(cron_d, f))
                           and f != ".placeholder")
        except OSError:
            files = []
        if files:
            out.append(s("/etc/cron.d/"))
            for fname in files:
                out.append(f"\n**{fname}**\n")
                out.append(block(_read_maybe_sudo(os.path.join(cron_d, fname))))

    for interval in ("hourly", "daily", "weekly", "monthly"):
        d = f"/etc/cron.{interval}"
        if not os.path.isdir(d):
            continue
        try:
            files = sorted(f for f in os.listdir(d) if not f.startswith("."))
        except OSError:
            files = []
        if not files:
            continue
        out.append(s(f"/etc/cron.{interval}/"))
        out.append(block("\n".join(files)))

    for spool in ("/var/spool/cron/crontabs", "/var/spool/cron"):
        if not os.path.isdir(spool):
            continue
        out.append(s(f"{spool}/ (per-user crontabs)"))
        try:
            files = sorted(os.listdir(spool))
        except (OSError, PermissionError):
            ls_out = run(["ls", "-1", spool], use_sudo=True)
            files = [] if ls_out.startswith("(") else ls_out.splitlines()
        if not files:
            out.append(block("(none / not readable)"))
            break
        for fname in files:
            out.append(f"\n**user: {fname}**\n")
            out.append(block(_read_maybe_sudo(os.path.join(spool, fname))))
        break

    user_timer_dir = "/etc/systemd/system"
    try:
        with os.scandir(user_timer_dir) as it:
            user_timers = sorted(
                e.name for e in it
                if e.name.endswith(".timer") and e.is_file(follow_symlinks=False)
            )
    except OSError:
        user_timers = []
    if user_timers:
        out.append(s("systemd timers (user-defined)"))
        for tname in user_timers:
            out.append(f"\n**{tname}**\n")
            out.append(block(_read_maybe_sudo(os.path.join(user_timer_dir, tname))))

    return "\n".join(out)


def section_services():
    out = [h("6. Live services")]

    if shutil.which("systemctl"):
        out.append(s("Running services"))
        out.append(block(run([
            "systemctl", "list-units", "--type=service", "--state=running",
            "--no-pager", "--no-legend", "--plain",
        ])))

    if shutil.which("ss"):
        out.append(s("Listening ports (TCP/UDP)"))
        out.append(block(run(["ss", "-tulnp"], use_sudo=True)))

    return "\n".join(out)


def section_webserver():
    out = [h("7. Web-server config")]
    nginx_installed = shutil.which("nginx") is not None
    caddy_installed = shutil.which("caddy") is not None

    if not (nginx_installed or caddy_installed):
        out.append(block("(no web servers installed)"))
        return "\n".join(out)

    if nginx_installed:
        out.append(s("nginx sites-enabled"))
        sites = "/etc/nginx/sites-enabled"
        if os.path.isdir(sites):
            try:
                files = sorted(os.listdir(sites))
            except OSError:
                files = []
            if not files:
                out.append(block("(no sites enabled)"))
            for fname in files:
                out.append(f"\n**{fname}**\n")
                out.append(block(_read_maybe_sudo(os.path.join(sites, fname))))
        else:
            out.append(block(f"({sites} not present)"))

    if caddy_installed:
        out.append(s("Caddyfile"))
        cf = "/etc/caddy/Caddyfile"
        if os.path.exists(cf):
            out.append(block(_read_maybe_sudo(cf)))
        else:
            out.append(block(f"({cf} not present)"))

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
        "> Read-only snapshot of host specs, filesystem, storage, packages, "
        "scheduled tasks, live services, and web-server config.",
        section_host(),
        section_filesystem(),
        section_storage(),
        section_packages(),
        section_cron(),
        section_services(),
        section_webserver(),
        "\n---\n_Generated by bonzai.py_",
    ]

    report = "\n".join(parts) + "\n"
    report, redactions = redact(report)
    if redactions:
        summary = ", ".join(f"{n}× {name}" for name, n in redactions.items())
        banner = f"> [!] Secret redaction applied: {summary}\n\n"
        report = report.replace(
            "> Read-only snapshot",
            banner + "> Read-only snapshot",
            1,
        )

    with open(outpath, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"Wrote: {outpath}")


if __name__ == "__main__":
    main()
