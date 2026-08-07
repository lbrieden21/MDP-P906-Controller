"""
Fetches and pins the third-party source trees vendored under
nrf_adapter_source_multiceiver/Drivers/ at the versions recorded in
tools/vendor.json, so a fresh checkout builds without those ~11 MB of
upstream code sitting in git history.

Each tree is downloaded from its pinned upstream tag or commit, extracted,
pruned to the files this repo actually builds, patched (currently one line,
in the Teensy 4.x linker script), hashed, and only then swapped into place --
a failed or interrupted fetch never leaves a half-populated Drivers/ subtree
behind for the build to trip over.

Standard library only: this has to run before anything else in the repo
exists, under a bare `python3` as well as `venv/bin/python`.

Usage:
    python3 tools/fetch_vendor.py                          # everything (zero-friction default)
    python3 tools/fetch_vendor.py --target teensy4x-eth     # just what that build needs
    python3 tools/fetch_vendor.py --tree qnethernet         # one tree by name
    python3 tools/fetch_vendor.py --list                    # trees, targets, pins, licenses, sizes
    python3 tools/fetch_vendor.py --check                   # verify what's on disk, download nothing
    python3 tools/fetch_vendor.py --force                   # re-fetch trees already present
    python3 tools/fetch_vendor.py --update-hashes            # record freshly computed tree_sha256

--target groups (see tools/vendor.json for what each tree contains):
    stm32f030          cmsis-core, cmsis-f0
    stm32f103          cmsis-core, cmsis-f1, tinyusb
    stm32f103-usart1   cmsis-core, cmsis-f1
    teensy3x           cores-teensy3, spi
    teensy4x           cores-teensy4, spi
    teensy4x-eth       cores-teensy4, spi, qnethernet
    esp32              nothing -- install ESP-IDF and set IDF_PATH instead
"""

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "tools" / "vendor.json"

TARGET_GROUPS = {
    "stm32f030": ["cmsis-core", "cmsis-f0"],
    "stm32f103": ["cmsis-core", "cmsis-f1", "tinyusb"],
    "stm32f103-usart1": ["cmsis-core", "cmsis-f1"],
    "teensy3x": ["cores-teensy3", "spi"],
    "teensy4x": ["cores-teensy4", "spi"],
    "teensy4x-eth": ["cores-teensy4", "spi", "qnethernet"],
    "esp32": [],
}

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--target", help="fetch only the trees one build target needs (see --list)")
parser.add_argument("--tree", help="fetch only one tree by name (see --list)")
parser.add_argument("--list", action="store_true", help="print trees, targets, pins, licenses, sizes, then exit")
parser.add_argument("--check", action="store_true", help="verify what's on disk against tools/vendor.json, download nothing")
parser.add_argument("--force", action="store_true", help="re-fetch trees that are already present")
parser.add_argument(
    "--update-hashes",
    action="store_true",
    help="after fetching, record the freshly computed tree_sha256 into tools/vendor.json instead of "
    "verifying against what's already recorded there",
)


def load_manifest():
    if not MANIFEST_PATH.exists():
        raise SystemExit(f"error: manifest not found: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text())


def save_manifest(manifest):
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def tree_by_name(manifest, name):
    for tree in manifest["trees"]:
        if tree["name"] == name:
            return tree
    names = ", ".join(t["name"] for t in manifest["trees"])
    raise SystemExit(f"error: unknown tree '{name}' -- known trees: {names}")


def resolve_trees(manifest, args):
    if args.tree:
        return [tree_by_name(manifest, args.tree)]
    if args.target:
        if args.target not in TARGET_GROUPS:
            names = ", ".join(TARGET_GROUPS)
            raise SystemExit(f"error: unknown target '{args.target}' -- known targets: {names}")
        names = TARGET_GROUPS[args.target]
        if not names:
            print(f"{args.target} does not vendor anything through this script.")
            if args.target == "esp32":
                print("Install ESP-IDF and set IDF_PATH -- see targets/esp32/Makefile:167.")
            return []
        return [tree_by_name(manifest, n) for n in names]
    return manifest["trees"]


def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def tree_dest_paths(tree):
    return [REPO_ROOT / mapping["to"] for mapping in tree["extract"]]


def hash_tree_at(root, tree):
    """sha256 over sorted(relpath + '\\0' + sha256(content)) for every file this
    tree places, rooted at `root` (either a staging dir or REPO_ROOT). Returns
    None if any of the tree's destination paths is missing."""
    entries = []
    for mapping in tree["extract"]:
        dst = root / mapping["to"]
        if dst.is_dir():
            for f in sorted(dst.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(root).as_posix()
                    entries.append((rel, hashlib.sha256(f.read_bytes()).hexdigest()))
        elif dst.is_file():
            rel = dst.relative_to(root).as_posix()
            entries.append((rel, hashlib.sha256(dst.read_bytes()).hexdigest()))
        else:
            return None
    hasher = hashlib.sha256()
    for rel, digest in sorted(set(entries)):
        hasher.update(rel.encode() + b"\0" + digest.encode())
    return hasher.hexdigest()


def download(url, cache):
    if url in cache:
        return cache[url]
    print(f"  downloading {url}")
    fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(fd, "wb") as out:
            shutil.copyfileobj(resp, out)
    except Exception as exc:
        Path(tmp_path).unlink(missing_ok=True)
        raise SystemExit(f"error: failed to download {url}: {exc}")
    cache[url] = Path(tmp_path)
    return cache[url]


def extracted_root(archive_path, cache):
    if archive_path in cache:
        return cache[archive_path]
    extract_dir = Path(tempfile.mkdtemp())
    with tarfile.open(archive_path) as tf:
        try:
            tf.extractall(extract_dir, filter="data")
        except TypeError:
            tf.extractall(extract_dir)  # Python < 3.12 has no `filter` kwarg
    top = [p for p in extract_dir.iterdir()]
    if len(top) != 1 or not top[0].is_dir():
        raise SystemExit(f"error: unexpected archive layout in {archive_path} (expected one top-level directory)")
    cache[archive_path] = top[0]
    return top[0]


def apply_excludes(staging, tree):
    patterns = tree.get("exclude") or []
    if not patterns:
        return
    for mapping in tree["extract"]:
        dst = staging / mapping["to"]
        if not dst.is_dir():
            continue
        for pattern in patterns:
            if pattern.endswith("/"):
                target = dst / pattern.rstrip("/")
                if target.exists():
                    shutil.rmtree(target)
            else:
                target = dst / pattern
                if target.exists():
                    target.unlink()


def apply_patches(staging, tree):
    for rule in tree.get("patch", []):
        path = staging / rule["file"]
        if not path.exists():
            raise SystemExit(f"error: {tree['name']}: patch target {rule['file']} not found after extract")
        text = path.read_text()
        count = text.count(rule["find"])
        if count != 1:
            raise SystemExit(
                f"error: {tree['name']}: patch find-string occurs {count} time(s) in {rule['file']} "
                f"(expected exactly 1) -- a pin bump likely moved it; fix tools/vendor.json"
            )
        path.write_text(text.replace(rule["find"], rule["replace"]))


def build_staging(tree, download_cache, extract_cache):
    staging = Path(tempfile.mkdtemp(prefix=f"vendor-{tree['name']}-"))
    archive_path = download(tree["archive_url"], download_cache)
    root = extracted_root(archive_path, extract_cache)
    for mapping in tree["extract"]:
        src = root if mapping["from"] == "." else root / mapping["from"]
        dst = staging / mapping["to"]
        if not src.exists():
            raise SystemExit(f"error: {tree['name']}: '{mapping['from']}' not found in {tree['archive_url']}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    apply_excludes(staging, tree)
    apply_patches(staging, tree)
    return staging


def fetch_tree(tree, manifest, args, download_cache, extract_cache):
    dest_paths = tree_dest_paths(tree)
    already_present = all(p.exists() for p in dest_paths)
    recorded = tree.get("tree_sha256")

    if already_present and not args.force and not args.update_hashes and recorded:
        current = hash_tree_at(REPO_ROOT, tree)
        if current == recorded:
            print(f"{tree['name']}: already present, hash OK, skipping")
            return
        print(
            f"{tree['name']}: WARNING -- present on disk but hash does not match tools/vendor.json "
            f"(recorded {recorded[:12]}..., on disk {current[:12] if current else 'unreadable'}...); "
            f"not overwriting, re-run with --force if that's intended",
            file=sys.stderr,
        )
        return

    print(f"{tree['name']}: fetching {tree['pin']['type']} {tree['pin']['value']} from {tree['homepage']}")
    staging = build_staging(tree, download_cache, extract_cache)
    computed = hash_tree_at(staging, tree)

    if args.update_hashes:
        tree["tree_sha256"] = computed
        print(f"  {tree['name']}: recorded tree_sha256 = {computed}")
    elif recorded is None:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit(
            f"error: {tree['name']}: no tree_sha256 recorded in tools/vendor.json -- "
            f"verify the fetch by hand, then re-run with --update-hashes to populate it"
        )
    elif computed != recorded:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit(
            f"error: {tree['name']}: fetched content does not match the recorded tree_sha256\n"
            f"  expected: {recorded}\n"
            f"  got:      {computed}\n"
            f"  the upstream pin may have moved, or the archive is corrupt -- {tree['name']} was not touched"
        )

    for mapping in tree["extract"]:
        dst = REPO_ROOT / mapping["to"]
        src = staging / mapping["to"]
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    shutil.rmtree(staging, ignore_errors=True)
    print(f"  {tree['name']}: OK ({tree['pin']['type']} {tree['pin']['value']})")


def list_trees(manifest):
    print("Trees:")
    for tree in manifest["trees"]:
        present = all(p.exists() for p in tree_dest_paths(tree))
        if present:
            size = sum(
                f.stat().st_size
                for p in tree_dest_paths(tree)
                for f in ([p] if p.is_file() else p.rglob("*"))
                if f.is_file()
            )
            size_str = human_size(size)
        else:
            size_str = "not fetched"
        pin = tree["pin"]
        print(f"  {tree['name']:<16} {tree['license']:<26} {pin['type']:<6} {pin['value'][:16]:<16} {size_str}")
    print()
    print("Targets:")
    for target, names in TARGET_GROUPS.items():
        dest = ", ".join(names) if names else "(nothing -- see targets/esp32/Makefile for IDF_PATH)"
        print(f"  {target:<20} {dest}")


def check_trees(trees):
    all_ok = True
    for tree in trees:
        recorded = tree.get("tree_sha256")
        current = hash_tree_at(REPO_ROOT, tree)
        if current is None:
            print(f"  {tree['name']}: MISSING")
            all_ok = False
        elif recorded is None:
            print(f"  {tree['name']}: NO RECORDED HASH (run --update-hashes)")
            all_ok = False
        elif current == recorded:
            print(f"  {tree['name']}: OK")
        else:
            print(f"  {tree['name']}: MISMATCH")
            print(f"    recorded: {recorded}")
            print(f"    on disk:  {current}")
            all_ok = False
    if not all_ok:
        sys.exit(1)
    print("all trees OK")


def main():
    args = parser.parse_args()
    if args.target and args.tree:
        parser.error("--target and --tree are mutually exclusive")

    manifest = load_manifest()

    if args.list:
        list_trees(manifest)
        return

    trees = resolve_trees(manifest, args)

    if args.check:
        if trees:
            check_trees(trees)
        return

    if not trees:
        return

    download_cache = {}
    extract_cache = {}
    try:
        for tree in trees:
            fetch_tree(tree, manifest, args, download_cache, extract_cache)
    finally:
        for archive in download_cache.values():
            Path(archive).unlink(missing_ok=True)
        for extracted in extract_cache.values():
            shutil.rmtree(extracted.parent, ignore_errors=True)

    if args.update_hashes:
        save_manifest(manifest)


if __name__ == "__main__":
    main()
