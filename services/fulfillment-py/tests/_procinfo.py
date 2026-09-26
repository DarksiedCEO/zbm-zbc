"""Portable process and socket introspection for the live tests (fix wave 16).

The live tests used to read Linux-only files directly (/proc/<pid>/status for
RSS, /proc/net/tcp for the bound address) and bind the Linux-only loopback
alias 127.0.0.2, so they crashed on macOS before checking anything. Every
live test now goes through this one module:

- Linux: the same /proc files as before (nothing changes there).
- macOS / BSD: ``ps -o rss= -p <pid>`` (/bin/ps) and
  ``lsof -nP -iTCP:<port> -sTCP:LISTEN -Ftn`` (/usr/sbin/lsof); both ship
  with the OS. ``ss`` does not exist on macOS and is not used.

The same file is copied into each Python service's tests/ (tests there are
not a package; each service's suite runs on its own).
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# Non-default addresses a test can bind to prove a *_BIND_ADDR override is
# honoured, in order of preference. Linux routes all of 127.0.0.0/8 to lo,
# so 127.0.0.2 works there; macOS configures only 127.0.0.1 on lo0 (unless
# someone ran `ifconfig lo0 alias 127.0.0.2`) but has IPv6 ::1 on lo0 by
# default.
BIND_OVERRIDE_CANDIDATES = ("127.0.0.2", "::1")


# --- RSS ---------------------------------------------------------------------


def rss_kib(pid: int) -> int:
    """Resident set size of exactly ONE process, in KiB (1024 bytes).

    Only ``pid`` is measured: not the pytest process that spawned it, not
    any other process. Linux: ``VmRSS`` from /proc/<pid>/status (the kernel
    prints it in kB = 1024 bytes). Elsewhere: ``ps -o rss=``, which on macOS
    and the BSDs also reports 1024-byte units, so both paths use one unit.
    """
    status = Path(f"/proc/{pid}/status")
    if Path("/proc/self/status").exists():  # Linux (a /proc with per-pid status files)
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
        raise AssertionError(f"no VmRSS line in {status} (process exited or is a zombie?)")
    return rss_kib_ps(pid)


def rss_kib_ps(pid: int) -> int:
    """RSS of ``pid`` in KiB via ``ps`` (the macOS/BSD path; also works on Linux)."""
    ps = shutil.which("ps") or "/bin/ps"
    r = subprocess.run([ps, "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    value = r.stdout.strip()
    if r.returncode != 0 or not value:
        raise AssertionError(f"ps reports no RSS for pid {pid} (exited?): rc={r.returncode} {r.stderr.strip()!r}")
    return int(value.split()[0])


def rss_mib(pid: int) -> int:
    return rss_kib(pid) // 1024


# --- listening sockets -------------------------------------------------------


def listening_addrs(port: int) -> set[str]:
    """Every local address with a TCP socket in LISTEN state on ``port``, as
    normalized text ("127.0.0.1", "0.0.0.0", "::1", "::"). IPv4 and IPv6 are
    both reported, so an all-interfaces bind shows up as "0.0.0.0" or "::"."""
    if Path("/proc/net/tcp").exists():
        return listening_addrs_proc(port)
    return listening_addrs_lsof(port)


def listening_addrs_proc(port: int) -> set[str]:
    """Linux: /proc/net/tcp and /proc/net/tcp6, LISTEN = state 0A. The kernel
    prints each 32-bit word of the address in host byte order."""
    out: set[str] = set()
    for name in ("tcp", "tcp6"):
        table = Path("/proc/net") / name
        if not table.exists():
            continue  # no IPv6 in this kernel/namespace
        for line in table.read_text().splitlines()[1:]:
            fields = line.split()
            addr_hex, port_hex = fields[1].split(":")
            if int(port_hex, 16) != port or fields[3] != "0A":
                continue
            raw = bytes.fromhex(addr_hex)
            if sys.byteorder == "little":
                raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
            out.add(str(ipaddress.ip_address(raw)))
    return out


def _lsof() -> str:
    found = shutil.which("lsof") or next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof", "/sbin/lsof") if os.path.exists(p)), None)
    if found is None:
        raise AssertionError("cannot read the socket table: no /proc/net/tcp (not Linux) and no lsof on this system")
    return found


def listening_addrs_lsof(port: int) -> set[str]:
    """macOS/BSD (also works on Linux): lsof's machine-readable output."""
    r = subprocess.run([_lsof(), "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Ftn"],
                       capture_output=True, text=True, timeout=30)
    # lsof exits 1 with no output when nothing matches; anything else is a real error.
    if r.returncode not in (0, 1) or (r.returncode == 1 and r.stdout.strip()):
        raise AssertionError(f"lsof failed (rc={r.returncode}): {r.stderr.strip()!r}")
    return parse_lsof_fn(r.stdout, port)


def parse_lsof_fn(text: str, port: int) -> set[str]:
    """Parse ``lsof -Ftn`` output: records of one field per line, the first
    character naming the field (p=pid, f=fd, t=type "IPv4"/"IPv6",
    n=name "127.0.0.1:8091", "[::1]:8091" or "*:8091"). A LISTEN socket's
    name has no "->peer" part. The wildcard "*" is mapped to the family's
    any-address, so a 0.0.0.0 bind can never be mistaken for loopback."""
    out: set[str] = set()
    family = None
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag in ("p", "f"):
            family = None  # a new process or file record starts
        elif tag == "t":
            family = value
        elif tag == "n":
            if "->" in value:
                continue
            host, _, p = value.rpartition(":")
            if p != str(port):
                continue
            host = host.strip("[]")
            if host == "*":
                host = "::" if family == "IPv6" else "0.0.0.0"
            out.add(str(ipaddress.ip_address(host)))
    return out


# --- binding ------------------------------------------------------------------


def _family(host: str) -> socket.AddressFamily:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def can_bind(host: str, port: int = 0) -> bool:
    """True when this OS lets a socket bind ``host``:``port`` right now."""
    try:
        with socket.socket(_family(host), socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


def override_bind_addr() -> str | None:
    """The first non-default address this OS can bind (see
    BIND_OVERRIDE_CANDIDATES), or None when it has neither."""
    return next((h for h in BIND_OVERRIDE_CANDIDATES if can_bind(h)), None)


NO_OVERRIDE_ADDR_REASON = (
    "cannot prove the bind-address override on this OS: 127.0.0.2 is not configured "
    "(macOS only has 127.0.0.1 on lo0 unless `sudo ifconfig lo0 alias 127.0.0.2` is run) "
    "and IPv6 loopback ::1 is not available either"
)


def url_host(host: str) -> str:
    """``host`` as it must appear in a URL (IPv6 literals in brackets)."""
    return f"[{host}]" if ":" in host else host
