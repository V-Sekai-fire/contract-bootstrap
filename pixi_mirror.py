#!/usr/bin/env python3
"""Mirror exactly the artifacts a pixi.lock pins for one platform, verified by the lock's sha256.

    pixi_mirror.py build  --lock pixi.lock --platform linux-64 --out MIRROR [--env NAME]... [--jobs 8]
    pixi_mirror.py verify --lock pixi.lock --platform linux-64 --out MIRROR [--env NAME]...
    pixi_mirror.py serve  --out MIRROR [--port 8765] [--log FILE]
    pixi_mirror.py config --lock pixi.lock --platform linux-64 [--base-url http://127.0.0.1:8765]
    pixi_mirror.py manifest --out MIRROR
    pixi_mirror.py --self-test

Layout: MIRROR/<host>/<url path>. pixi 0.81 refuses file:// mirrors, so `serve` puts the tree on loopback
and `config` maps every origin the lock names onto it. Repeat --lock to build one mirror for several locks.
Exit status: 0 verified, 1 a verification failure, 2 the lock selects something that cannot be mirrored.
"""

import argparse
import concurrent.futures
import hashlib
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

META_DIR = ".pixi-mirror"
MANIFEST = "manifest.tsv"
CHUNK = 1 << 20


class LockError(Exception):
    pass


def _scalar(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        inner = raw[1:-1]
        return inner.replace("''", "'") if raw[0] == "'" else inner.replace('\\"', '"').replace("\\\\", "\\")
    return raw


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def parse_lock(text):
    """Return (version, environments, packages) from the subset of YAML that pixi writes.

    environments: {env: {"channels": [...], "indexes": [...], "find_links": [...],
                         "packages": {platform: [(kind, url), ...]}}}
    packages: {url: {"kind": kind, "sha256": ..., "md5": ..., "size": ...}}
    """
    version = None
    envs = {}
    packages = {}
    section = None
    env = None
    env_key = None
    platform = None
    current = None
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = _indent(line)
        body = line.strip()
        if ind == 0 and not body.startswith("- "):
            key, _, val = body.partition(":")
            section = key
            if key == "version":
                version = int(_scalar(val))
            continue
        if section == "environments":
            if ind == 2 and not body.startswith("- "):
                env = body.rstrip(":").strip()
                envs[env] = {"channels": [], "indexes": [], "find_links": [], "packages": {}}
                env_key = None
            elif ind == 4 and not body.startswith("- "):
                key, _, val = body.partition(":")
                env_key = key.strip()
                platform = None
            elif env_key == "packages" and ind == 6 and not body.startswith("- "):
                platform = body.rstrip(":").strip()
                envs[env]["packages"][platform] = []
            elif env_key == "packages" and ind == 6 and body.startswith("- "):
                kind, _, url = body[2:].partition(":")
                if platform is None:
                    raise LockError("line %d: package outside a platform" % lineno)
                envs[env]["packages"][platform].append((kind.strip(), _scalar(url)))
            elif env_key == "channels" and body.startswith("- url:"):
                envs[env]["channels"].append(_scalar(body[len("- url:"):]))
            elif env_key == "indexes" and body.startswith("- "):
                envs[env]["indexes"].append(_scalar(body[2:]))
            elif env_key == "find-links" and body.startswith("- "):
                item = body[2:]
                for prefix in ("url:", "path:"):
                    if item.startswith(prefix):
                        item = item[len(prefix):]
                envs[env]["find_links"].append(_scalar(item))
            continue
        if section == "packages":
            if ind == 0 and body.startswith("- "):
                kind, _, url = body[2:].partition(":")
                url = _scalar(url)
                if url in packages:
                    raise LockError("line %d: duplicate package %s" % (lineno, url))
                current = {"kind": kind.strip()}
                packages[url] = current
            elif ind == 2 and not body.startswith("- ") and current is not None:
                key, sep, val = body.partition(":")
                if sep and key in ("sha256", "md5", "size", "name", "version", "index"):
                    current[key] = _scalar(val)
    if version is None:
        raise LockError("no version line; not a pixi lock")
    if version < 6:
        raise LockError("lock version %d; this reader handles 6 and later" % version)
    return version, envs, packages


def _fetch_url(url):
    return url[len("direct+"):] if url.startswith("direct+") else url


def relpath_for(url):
    parts = urllib.parse.urlsplit(_fetch_url(url))
    host = parts.netloc or "_local"
    segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    if not segments or any(s in (".", "..") or "/" in s or "\\" in s or s.startswith(META_DIR) for s in segments):
        raise LockError("refusing unsafe path in %s" % url)
    if host.startswith(".") or "/" in host:
        raise LockError("refusing host in %s" % url)
    return "/".join([host] + segments)


def mirrorable(url):
    return urllib.parse.urlsplit(_fetch_url(url)).scheme in ("http", "https", "file")


def origin(url):
    parts = urllib.parse.urlsplit(_fetch_url(url))
    return (parts.scheme, parts.netloc)


def select(lock_paths, platform, env_names=None):
    """Union of what the given environments lock for one platform, keyed by mirror path."""
    wanted = {}
    unmirrorable = []
    per_env = {}
    hosts = set()
    selected_any = False
    for lock_path in lock_paths:
        _, envs, packages = parse_lock(Path(lock_path).read_text(encoding="utf-8"))
        names = env_names or sorted(envs)
        missing_envs = [n for n in names if n not in envs]
        if missing_envs:
            raise LockError("%s: no environment %s" % (lock_path, ", ".join(missing_envs)))
        for name in names:
            entries = envs[name]["packages"].get(platform)
            if entries is None:
                continue
            selected_any = True
            per_env["%s:%s" % (lock_path, name)] = len(entries)
            for url in envs[name]["channels"] + envs[name]["indexes"] + envs[name]["find_links"]:
                if mirrorable(url):
                    hosts.add(origin(url))
            for kind, url in entries:
                if not mirrorable(url):
                    unmirrorable.append((lock_path, name, kind, url, "not an http(s) artifact"))
                    continue
                record = packages.get(url)
                if record is None:
                    raise LockError("%s: %s referenced but absent from packages:" % (lock_path, url))
                sha = record.get("sha256", "")
                if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                    unmirrorable.append((lock_path, name, kind, url, "no sha256 in the lock"))
                    continue
                if "index" in record and mirrorable(record["index"]):
                    hosts.add(origin(record["index"]))
                rel = relpath_for(url)
                size = int(record["size"]) if "size" in record else None
                prior = wanted.get(rel)
                if prior and (prior["sha256"] != sha or prior["url"] != url):
                    raise LockError("%s locked twice with different hashes or urls" % rel)
                wanted[rel] = {"url": url, "kind": kind, "sha256": sha, "size": size}
                hosts.add(origin(url))
    if not selected_any:
        raise LockError("no environment in %s locks platform %s" % (", ".join(map(str, lock_paths)), platform))
    return wanted, unmirrorable, per_env, sorted(h for h in hosts if h[1])


def sha256_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def download(rel, item, out, attempts=3):
    dest = out / rel
    if dest.is_file():
        sha, size = sha256_file(dest)
        if sha == item["sha256"]:
            return rel, "present", size
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last = None
    for attempt in range(1, attempts + 1):
        try:
            h = hashlib.sha256()
            size = 0
            req = urllib.request.Request(_fetch_url(item["url"]), headers={"User-Agent": "pixi-mirror/1"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(part, "wb") as f:
                while True:
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    h.update(block)
                    f.write(block)
                    size += len(block)
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
            part.unlink(missing_ok=True)
            time.sleep(attempt)
            continue
        if item["size"] is not None and size != item["size"]:
            part.unlink()
            last = "size %d, lock says %d" % (size, item["size"])
            time.sleep(attempt)
            continue
        if h.hexdigest() != item["sha256"]:
            part.unlink()
            return rel, "REFUSED sha256 %s, lock says %s" % (h.hexdigest(), item["sha256"]), size
        os.replace(part, dest)
        return rel, "fetched", size
    return rel, "FAILED %s" % last, 0


def walk_artifacts(out):
    found = {}
    links = []
    for root, dirs, files in os.walk(out):
        rootp = Path(root)
        if rootp == out:
            dirs[:] = [d for d in dirs if d != META_DIR]
        for d in list(dirs):
            if (rootp / d).is_symlink():
                links.append((rootp / d).relative_to(out).as_posix())
                dirs.remove(d)
        for name in files:
            p = rootp / name
            rel = p.relative_to(out).as_posix()
            if p.is_symlink():
                links.append(rel)
            else:
                found[rel] = p
    return found, links


def verify(out, wanted, quiet=False):
    """Rehash every file. Returns (problems, rows) where rows feed the manifest."""
    found, links = walk_artifacts(out)
    lock_hashes = {item["sha256"]: rel for rel, item in wanted.items()}
    problems = []
    rows = []
    for rel in sorted(found):
        sha, size = sha256_file(found[rel])
        rows.append((rel, size, sha))
        item = wanted.get(rel)
        if item is None:
            where = "misplaced copy of %s" % lock_hashes[sha] if sha in lock_hashes else "sha256 not in the lock"
            problems.append(("EXTRA", rel, "%s (%s)" % (sha, where)))
        elif sha != item["sha256"]:
            problems.append(("MISMATCH", rel, "sha256 %s, lock says %s" % (sha, item["sha256"])))
        elif item["size"] is not None and size != item["size"]:
            problems.append(("SIZE", rel, "size %d, lock says %d" % (size, item["size"])))
    for rel in sorted(set(wanted) - set(found)):
        problems.append(("MISSING", rel, wanted[rel]["url"]))
    for rel in links:
        problems.append(("SYMLINK", rel, "symlinks are refused"))
    if not wanted:
        problems.append(("EMPTY", "-", "the selection names no artifacts"))
    if not quiet:
        for kind, rel, detail in problems:
            print("%-8s %s  %s" % (kind, rel, detail))
    return problems, rows


def write_manifest(out, rows):
    text = "".join("%s\t%d\t%s\n" % row for row in rows)
    meta = out / META_DIR
    meta.mkdir(parents=True, exist_ok=True)
    (meta / MANIFEST).write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize(wanted, rows, problems, unmirrorable):
    by_kind = {}
    for item in wanted.values():
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    total = sum(size for _, size, _ in rows)
    kinds = " ".join("%s=%d" % kv for kv in sorted(by_kind.items()))
    print("expected %d artifacts (%s); on disk %d files, %d bytes" % (len(wanted), kinds, len(rows), total))
    counts = {}
    for kind, _, _ in problems:
        counts[kind] = counts.get(kind, 0) + 1
    names = ("MISSING", "MISMATCH", "SIZE", "EXTRA", "SYMLINK", "EMPTY")
    print("problems: " + " ".join("%s=%d" % (k.lower(), counts.get(k, 0)) for k in names))
    print("unmirrorable entries: %d" % len(unmirrorable))
    for lock, env, kind, url, why in unmirrorable:
        print("UNMIRRORED %s %s:%s %s (%s)" % (lock, env, kind, url, why))


def cmd_build(args):
    out = Path(args.out).resolve()
    wanted, unmirrorable, per_env, _ = select(args.lock, args.platform, args.env)
    for key in sorted(per_env):
        print("selected %s: %d entries for %s" % (key, per_env[key], args.platform))
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(download, rel, wanted[rel], out) for rel in sorted(wanted)]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    fetch_s = time.monotonic() - started
    tally = {}
    for rel, status, _ in sorted(results):
        tally[status.split()[0]] = tally.get(status.split()[0], 0) + 1
        if status not in ("fetched", "present"):
            print("%s  %s" % (status, rel))
    print("download: " + " ".join("%s=%d" % kv for kv in sorted(tally.items())) + " in %.1f s" % fetch_s)
    problems, rows = verify(out, wanted)
    digest = write_manifest(out, rows)
    summarize(wanted, rows, problems, unmirrorable)
    print("manifest sha256 %s (%s)" % (digest, out / META_DIR / MANIFEST))
    print("build+verify wall %.1f s" % (time.monotonic() - started))
    if problems or any(s.split()[0] not in ("fetched", "present") for _, s, _ in results):
        return 1
    return 2 if unmirrorable and not args.allow_unmirrorable else 0


def cmd_verify(args):
    out = Path(args.out).resolve()
    if not out.is_dir():
        print("FAIL: mirror %s does not exist" % out)
        return 1
    wanted, unmirrorable, _, _ = select(args.lock, args.platform, args.env)
    started = time.monotonic()
    problems, rows = verify(out, wanted)
    digest = hashlib.sha256("".join("%s\t%d\t%s\n" % r for r in rows).encode("utf-8")).hexdigest()
    summarize(wanted, rows, problems, unmirrorable)
    print("manifest sha256 %s; verify %.1f s" % (digest, time.monotonic() - started))
    if problems:
        print("FAIL: %d problem(s)" % len(problems))
        return 1
    if unmirrorable and not args.allow_unmirrorable:
        print("FAIL: %d locked entries cannot come from this mirror" % len(unmirrorable))
        return 2
    print("OK: %d artifacts match the lock" % len(rows))
    return 0


def cmd_config(args):
    _, _, _, origins = select(args.lock, args.platform, args.env)
    base = args.base_url.rstrip("/")
    lines = ["# pixi_mirror.py config: every origin the lock names is served from %s" % base, "[mirrors]"]
    for scheme, host in origins:
        lines.append('"%s://%s/" = ["%s/%s/"]' % (scheme, host, base, host))
    print("\n".join(lines))
    return 0


def make_server(out, bind, port, log):
    import functools
    import http.server

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def log_request(self, code="-", size="-"):
            print("%s %s %s" % (self.command, self.path, getattr(code, "value", code)), file=log)

        def list_directory(self, path):
            self.send_error(404, "no listings")
            return None

        def guess_type(self, path):
            return "application/octet-stream"

    return http.server.ThreadingHTTPServer((bind, port), functools.partial(Handler, directory=str(out)))


def cmd_serve(args):
    """Serve the mirror on loopback and log every request, because pixi refuses file:// mirrors."""
    out = Path(args.out).resolve()
    if not out.is_dir():
        print("FAIL: mirror %s does not exist" % out)
        return 1
    log = open(args.log, "a", buffering=1) if args.log else sys.stderr
    server = make_server(out, args.bind, args.port, log)
    print("serving %s at http://%s:%d/" % (out, args.bind, server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_manifest(args):
    out = Path(args.out).resolve()
    found, links = walk_artifacts(out)
    for rel in sorted(found):
        sha, size = sha256_file(found[rel])
        print("%s\t%d\t%s" % (rel, size, sha))
    for rel in links:
        print("SYMLINK %s" % rel, file=sys.stderr)
    return 1 if links else 0


SELF_TEST_LOCK = """version: 7
platforms:
- name: linux-64
environments:
  default:
    channels:
    - url: CHANNEL/
    indexes:
    - https://pypi.example/simple
    packages:
      linux-64:
      - conda: CHANNEL/linux-64/a-1.0-0.conda
      - conda: CHANNEL/noarch/b-2.0-0.tar.bz2
      - pypi: FILES/packages/aa/bb/c-3.0-py3-none-any.whl
        extras:
        - fast
      osx-arm64:
      - conda: CHANNEL/osx-arm64/a-1.0-0.conda
  extra:
    channels:
    - url: CHANNEL/
    packages:
      linux-64:
      - conda: CHANNEL/noarch/b-2.0-0.tar.bz2
      - pypi: ./local-src
packages:
- conda: CHANNEL/linux-64/a-1.0-0.conda
  sha256: SHA_A
  md5: 00000000000000000000000000000000
  depends:
  - b >=2
  license: MIT
  size: SIZE_A
- conda: CHANNEL/noarch/b-2.0-0.tar.bz2
  sha256: SHA_B
  noarch: python
  size: SIZE_B
- pypi: FILES/packages/aa/bb/c-3.0-py3-none-any.whl
  name: c
  version: '3.0'
  sha256: SHA_C
  requires_dist:
  - b ; extra == 'fast'
- conda: CHANNEL/osx-arm64/a-1.0-0.conda
  sha256: SHA_D
  size: 1
- pypi: ./local-src
  name: local-src
  version: 0.1.0
"""


def run_self_test():
    """Plant one defect per failure kind and require each to be caught."""
    failures = []

    def expect(name, cond):
        print("%-4s %s" % ("ok" if cond else "MISS", name))
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        upstream = tmp / "upstream"
        blobs = {
            "chan/linux-64/a-1.0-0.conda": b"conda-a" * 100,
            "chan/noarch/b-2.0-0.tar.bz2": b"conda-b" * 50,
            "files/packages/aa/bb/c-3.0-py3-none-any.whl": b"wheel-c" * 70,
        }
        for rel, data in blobs.items():
            (upstream / rel).parent.mkdir(parents=True, exist_ok=True)
            (upstream / rel).write_bytes(data)
        sha = {k: hashlib.sha256(v).hexdigest() for k, v in blobs.items()}
        text = (SELF_TEST_LOCK.replace("CHANNEL", (upstream / "chan").as_uri())
                .replace("FILES", (upstream / "files").as_uri())
                .replace("SHA_A", sha["chan/linux-64/a-1.0-0.conda"])
                .replace("SIZE_A", str(len(blobs["chan/linux-64/a-1.0-0.conda"])))
                .replace("SHA_B", sha["chan/noarch/b-2.0-0.tar.bz2"])
                .replace("SIZE_B", str(len(blobs["chan/noarch/b-2.0-0.tar.bz2"])))
                .replace("SHA_C", sha["files/packages/aa/bb/c-3.0-py3-none-any.whl"])
                .replace("SHA_D", "d" * 64))
        lock = tmp / "pixi.lock"
        lock.write_text(text)

        version, envs, packages = parse_lock(text)
        expect("parser reads version, both environments and five packages",
               version == 7 and sorted(envs) == ["default", "extra"] and len(packages) == 5)
        expect("parser keeps the pypi extras line out of the package list",
               [k for k, _ in envs["default"]["packages"]["linux-64"]] == ["conda", "conda", "pypi"])

        wanted, unmirrorable, _, hosts = select([lock], "linux-64", ["default"])
        expect("selection is exactly the three linux-64 artifacts", len(wanted) == 3 and not unmirrorable)
        expect("a host named only by an index is still mapped", hosts == [("https", "pypi.example")])
        _, unm_all, _, _ = select([lock], "linux-64")
        expect("a path-based pypi entry is named as unmirrorable", len(unm_all) == 1 and "./local-src" in unm_all[0][3])
        try:
            select([lock], "win-64")
            expect("a platform the lock does not carry is a failure, not an empty pass", False)
        except LockError:
            expect("a platform the lock does not carry is a failure, not an empty pass", True)

        out = tmp / "mirror"
        quiet = open(os.devnull, "w")
        old = sys.stdout
        sys.stdout = quiet
        try:
            rc_build = cmd_build(argparse.Namespace(lock=[lock], platform="linux-64", env=["default"], out=str(out),
                                                    jobs=2, allow_unmirrorable=False))
            problems, rows = verify(out, wanted, quiet=True)
        finally:
            sys.stdout = old
        expect("clean build exits 0 and verifies", rc_build == 0 and not problems and len(rows) == 3)
        digest_a = (out / META_DIR / MANIFEST).read_bytes()

        import io
        import threading
        server = make_server(out, "127.0.0.1", 0, io.StringIO())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/" % server.server_address[1]

        def get(path):
            try:
                with urllib.request.urlopen(base + path, timeout=10) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, b""

        first = sorted(wanted)[0]
        status, body = get(urllib.parse.quote(first))
        expect("serve returns a mirrored artifact byte for byte", status == 200 and body == (out / first).read_bytes())
        expect("serve answers a missing artifact with 404", get("_local/absent-0.0-0.conda")[0] == 404)
        expect("serve refuses directory listings", get("_local/")[0] == 404)
        plus = out / "_local" / "torch-1.0+cpu.whl"
        plus.write_bytes(b"plus")
        expect("serve maps a %2B request onto a + filename", get("_local/torch-1.0%2Bcpu.whl") == (200, b"plus"))
        plus.unlink()
        server.shutdown()
        server.server_close()

        target = out / sorted(wanted)[0]
        original = target.read_bytes()
        flipped = bytearray(original)
        flipped[len(flipped) // 2] ^= 0x01
        target.write_bytes(bytes(flipped))
        problems, _ = verify(out, wanted, quiet=True)
        expect("one flipped byte is a MISMATCH", [p[0] for p in problems] == ["MISMATCH"])
        target.write_bytes(original)

        target.unlink()
        problems, _ = verify(out, wanted, quiet=True)
        expect("a deleted artifact is MISSING", [p[0] for p in problems] == ["MISSING"])
        target.write_bytes(original)

        stray = out / "_local" / "stray.conda"
        stray.write_bytes(b"not in the lock")
        problems, _ = verify(out, wanted, quiet=True)
        expect("a file whose hash is not in the lock is EXTRA", [p[0] for p in problems] == ["EXTRA"]
               and "not in the lock" in problems[0][2])
        stray.unlink()

        part = out / (sorted(wanted)[0] + ".part")
        part.write_bytes(b"partial")
        problems, _ = verify(out, wanted, quiet=True)
        expect("a leftover .part download is EXTRA", [p[0] for p in problems] == ["EXTRA"])
        part.unlink()

        link = out / "_local" / "link.conda"
        os.symlink(target, link)
        problems, _ = verify(out, wanted, quiet=True)
        expect("a symlink in the mirror is refused", "SYMLINK" in [p[0] for p in problems])
        link.unlink()

        upstream_file = upstream / "chan/noarch/b-2.0-0.tar.bz2"
        rel_b = [r for r in wanted if r.endswith("b-2.0-0.tar.bz2")][0]
        upstream_file.write_bytes(b"x" * len(blobs["chan/noarch/b-2.0-0.tar.bz2"]))
        fresh = tmp / "mirror2"
        sys.stdout = quiet
        try:
            rc_tamper = cmd_build(argparse.Namespace(lock=[lock], platform="linux-64", env=["default"],
                                                     out=str(fresh), jobs=2, allow_unmirrorable=False))
        finally:
            sys.stdout = old
        landed = (fresh / rel_b).exists() or (fresh / (rel_b + ".part")).exists()
        expect("a download whose bytes differ from the lock is refused and never lands", rc_tamper == 1 and not landed)
        upstream_file.write_bytes(blobs["chan/noarch/b-2.0-0.tar.bz2"])

        nohash = tmp / "nohash.lock"
        nohash.write_text(text.replace("  sha256: " + sha["chan/noarch/b-2.0-0.tar.bz2"] + "\n", ""))
        _, unm, _, _ = select([nohash], "linux-64", ["default"])
        expect("a locked artifact without a sha256 is refused", len(unm) == 1 and "no sha256" in unm[0][4])

        twice = tmp / "mirror3"
        sys.stdout = quiet
        try:
            cmd_build(argparse.Namespace(lock=[lock], platform="linux-64", env=["default"], out=str(twice),
                                         jobs=2, allow_unmirrorable=False))
        finally:
            sys.stdout = old
        expect("two builds write byte-identical manifests", (twice / META_DIR / MANIFEST).read_bytes() == digest_a)

        sys.stdout = quiet
        try:
            rc_unm = cmd_build(argparse.Namespace(lock=[lock], platform="linux-64", env=None, out=str(twice),
                                                  jobs=2, allow_unmirrorable=False))
        finally:
            sys.stdout = old
        expect("a build that cannot cover every locked entry exits 2", rc_unm == 2)

        for bad in ("https://h/a/../../etc/passwd", "https://h/%2e%2e/x", "https://h/a%2F..%2Fb", "https://h/"):
            try:
                relpath_for(bad)
                expect("unsafe url %s is refused" % bad, False)
            except LockError:
                expect("unsafe url %s is refused" % bad, True)
        quiet.close()

    print("self-test: %d control(s) missed" % len(failures) if failures else "self-test: every planted defect caught")
    return 1 if failures else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--self-test" in argv:
        return run_self_test()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("build", "verify", "config"):
        p = sub.add_parser(name)
        p.add_argument("--lock", action="append", required=True, type=Path)
        p.add_argument("--platform", required=True)
        p.add_argument("--env", action="append")
        if name == "config":
            p.add_argument("--base-url", default="http://127.0.0.1:8765")
            continue
        p.add_argument("--out", required=True)
        p.add_argument("--allow-unmirrorable", action="store_true")
        if name == "build":
            p.add_argument("--jobs", type=int, default=8)
    p = sub.add_parser("manifest")
    p.add_argument("--out", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--out", required=True)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--log")
    args = parser.parse_args(argv)
    commands = {"build": cmd_build, "verify": cmd_verify, "config": cmd_config, "manifest": cmd_manifest,
                "serve": cmd_serve}
    try:
        return commands[args.cmd](args)
    except LockError as exc:
        print("FAIL: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
