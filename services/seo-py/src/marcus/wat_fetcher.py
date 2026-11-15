"""The WAT Fetcher (``cc_wat_fetcher``, proposed name Felix, name pending Q-MP-1): deliver the WAT files of one crawl
(or one targeted subset, path B) to the Link Extractor, verified (marcus-pipeline-spec/1 B-MP-10..15).

  - input: one recorded crawl release and its ``wat.paths`` list (CC's shape: one relative path a line, plain or
    gzip), or a targeted path list from the request (path B);
  - every path must be a CC data path of THAT crawl (``WAT_PATH``): anything else is ``PATH_NOT_ALLOWED`` (no
    traversal, no other host, no absolute path, no other crawl);
  - v1 source: the fixture directory (``SEO_MARCUS_FIXTURE_DIR``, tests only). Every path component is opened with
    ``O_NOFOLLOW`` relative to its parent's descriptor, so no symlink (file or directory) is ever followed and no file
    outside the directory is ever read; the live port is ``config.NOT_BUILT`` (``SEO_CC_WAT_ENABLED``);
  - each file is read gzip-streamed with the wave1 decompression rules (``gz_lines``): every member decoded within
    one budget (2 GiB decoded a file), trailing garbage ``PROTOCOL_ERROR``, corrupt or truncated
    ``DECOMPRESS_FAILED``, over budget ``TOO_LARGE``; lines longer than 16 MiB are skipped without being held
    (``TOO_LARGE`` marker); peak memory <= 64 MiB.

B-MP-14 (v2 live politeness, specified, not built): one connection, an identified user-agent, exponential backoff
with jitter on 429 / 503 from 1 s, at most 4 retries, the job stops on 3 consecutive 503s, never parallel to a host."""

from __future__ import annotations

import gzip
import os
import re
import stat
import zlib

AGENT = "cc_wat_fetcher"
DECODED_CAP = 2 * 1024 ** 3                # B-MP-12: decoded bytes a file
MAX_LINE = 16 * 1024 * 1024                # B-MP-20: a record larger than this is skipped and counted
PEAK_BYTES = 64 * 1024 * 1024              # B-MP-12 / A-MP-01: the memory bound
IN_CHUNK = 256 * 1024
OUT_CHUNK = 1024 * 1024
MAX_PATHS = 100_000                        # a wat.paths list (CC's lists hold up to ~100k files)
MAX_TARGETED_PATHS = 1_000                 # a targeted path list in a request (path B; fits the 128 KiB body limit)
MAX_PATHS_BYTES = 32 * 1024 * 1024
TOO_LARGE = "TOO_LARGE"
WAT_PATH = re.compile(r"crawl-data/(CC-MAIN-\d{4}-\d{2})/segments/\d{13}\.\d{1,2}/wat/"
                      r"CC-MAIN-\d{14}-\d{14}-\d{5}\.warc\.wat\.gz")
# v2 politeness constants (B-MP-14), not used by any v1 code path
V2_BACKOFF_START_S, V2_MAX_RETRIES, V2_STOP_AFTER_503 = 1, 4, 3


class PathNotAllowed(Exception):
    code = "PATH_NOT_ALLOWED"


class Quarantined(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def check_path(crawl_id: str, path) -> str:
    """``path`` when it is a WAT data path of ``crawl_id``; else ``PathNotAllowed``."""
    if not isinstance(path, str) or len(path) > 200 or not path.isascii():
        raise PathNotAllowed()
    m = WAT_PATH.fullmatch(path)
    if m is None or m.group(1) != crawl_id:
        raise PathNotAllowed()
    return path


def parse_path_list(data: bytes, crawl_id: str) -> list:
    """The paths of a ``wat.paths`` file (plain or gzip). Any line outside the grammar refuses the whole list."""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data[:MAX_PATHS_BYTES])
        except (OSError, EOFError, zlib.error):
            raise PathNotAllowed() from None
    if len(data) > MAX_PATHS_BYTES:
        raise Quarantined("PATH_LIST_TOO_LARGE")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise PathNotAllowed() from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_PATHS:
        raise Quarantined("PATH_LIST_TOO_LARGE")
    out = [check_path(crawl_id, ln) for ln in lines]
    if len(set(out)) != len(out):
        raise PathNotAllowed()
    return out


def _open_beneath(root_fd: int, rel: str) -> int:
    """A read-only descriptor of ``rel`` under ``root_fd``: every component opened with O_NOFOLLOW from its parent's
    descriptor (a symlink anywhere refuses), the last one a regular file."""
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PathNotAllowed()
    fd = os.dup(root_fd)
    try:
        for p in parts[:-1]:
            nxt = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        last = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
    except OSError:
        os.close(fd)
        raise PathNotAllowed() from None
    os.close(fd)
    if not stat.S_ISREG(os.fstat(last).st_mode):
        os.close(last)
        raise PathNotAllowed()
    return last


class FixtureWatSource:
    """The v1 ``cc_wat`` port: the fixture directory (tests only)."""

    connected = True

    def __init__(self, root: str):
        self.root = root

    def _root_fd(self) -> int:
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def paths(self, crawl_id: str) -> bytes:
        rfd = self._root_fd()
        try:
            for name in ("wat.paths", "wat.paths.gz"):
                try:
                    fd = _open_beneath(rfd, f"crawl-data/{crawl_id}/{name}")
                except PathNotAllowed:
                    continue
                with os.fdopen(fd, "rb") as fh:
                    return fh.read(MAX_PATHS_BYTES + 1)
        finally:
            os.close(rfd)
        raise Quarantined("PATH_LIST_MISSING")

    def open(self, path: str):
        """A binary file object for one checked WAT path (``PathNotAllowed`` for anything not a plain file there)."""
        rfd = self._root_fd()
        try:
            fd = _open_beneath(rfd, path)
        finally:
            os.close(rfd)
        return os.fdopen(fd, "rb")


class NotConnectedWat:
    connected = False

    def paths(self, crawl_id):
        raise RuntimeError("the cc_wat port is not connected")

    def open(self, path):
        raise RuntimeError("the cc_wat port is not connected")


def gz_lines(fh, cap: int = DECODED_CAP, max_line: int = MAX_LINE, check=None, stats=None):
    """Yield the lines (bytes, without the newline) of a gzip stream, or ``TOO_LARGE`` for a line longer than
    ``max_line`` (skipped without being held). Raises ``Quarantined`` for the file: DECOMPRESS_FAILED (not gzip,
    corrupt, truncated, empty), PROTOCOL_ERROR (bytes after a complete member that do not start another member),
    TOO_LARGE (more than ``cap`` decoded bytes over all members). ``check`` is called before every input chunk (the
    kill switches); ``stats`` receives ``compressed`` and ``decoded`` byte counts."""
    d = None
    decoded = compressed = 0
    members_done = 0
    parts, held, skipping = [], 0, False

    def lines_of(data: bytes):
        nonlocal parts, held, skipping
        start = 0
        while True:
            nl = data.find(b"\n", start)
            piece = data[start:] if nl < 0 else data[start:nl]
            if not skipping:
                if held + len(piece) > max_line:
                    parts, held = [], 0
                    skipping = nl < 0                  # the rest of this line is dropped as it arrives
                    yield TOO_LARGE
                else:
                    parts.append(piece)
                    held += len(piece)
                    if nl >= 0:
                        out = b"".join(parts)
                        parts, held = [], 0
                        yield out
            elif nl >= 0:
                skipping = False
            if nl < 0:
                return
            start = nl + 1

    while True:
        if check is not None:
            check()
        data = fh.read(IN_CHUNK)
        if not data:
            break
        compressed += len(data)
        try:
            while data:
                if d is None:
                    if not b"\x1f\x8b".startswith(data[:2]):
                        raise Quarantined("PROTOCOL_ERROR" if members_done else "DECOMPRESS_FAILED")
                    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                out = d.decompress(data, OUT_CHUNK)
                decoded += len(out)
                if decoded > cap:
                    raise Quarantined(TOO_LARGE)
                yield from lines_of(out)
                data = d.unconsumed_tail
                if d.eof:
                    data = d.unused_data
                    d = None
                    members_done += 1
        except zlib.error:
            raise Quarantined("DECOMPRESS_FAILED") from None
    if d is not None or members_done == 0:
        raise Quarantined("DECOMPRESS_FAILED")           # truncated (a member never ended), or an empty file
    tail = b"".join(parts)
    if tail and not skipping:
        yield tail                                       # a last line without its newline
    if stats is not None:
        stats["compressed"], stats["decoded"] = compressed, decoded
