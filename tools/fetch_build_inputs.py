#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch and verify the pinned public build inputs listed in tools/build_inputs.json.

Everything lands in the ignored build tree, where the other tools look for it:

- the native app template at its pinned commit, built once: its host tool, the
  payload SDK it pins (v0.42) and the libc.prx it generates from source;
- dxil-spirv at the converter's pinned commit, with its submodules;
- dxc and libdxcompiler.so from Microsoft's DXC release;
- the BC250 RC11 amd_fidelityfx_upscaler_dx12.dll and its notices, from BC250's
  release (the DLL must match tools/fsr4_dll_map.json);
- the pipeline cache a PS5 saved for these pass tables and this driver, from this
  repository's build-inputs release.

--env prints the variables the builds read (PS5VK_LAB_ROOT, PS5_PAYLOAD_SDK,
PS5_NATIVE_APP_TEMPLATE, DXIL_SPIRV_DIR); --github-env appends them to
$GITHUB_ENV. --check-cache, run after make runtime, verifies that the pinned cache
belongs to this driver's cache identity and to the pass tables just generated.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "tools/build_inputs.json"
DLL_MAP = ROOT / "tools/fsr4_dll_map.json"
DOWNLOADS = ROOT / "build/inputs/downloads"
DRIVER_PROFILE = ROOT / "external/ps5-vulkan/src/physical_device_profile.h"
UUID_INPUTS = ("PS5VK_GFX_TARGET", "PS5VK_DRIVER_VERSION", "PS5VK_COMPILER_IDENTITY",
               "PS5VK_COMPILER_IDENTITY_VERSION", "PS5VK_CACHE_ABI_IDENTITY", "PS5VK_PIPELINE_CACHE_UUID_FORMAT")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, digest):
    """The file at url, cached under build/inputs/downloads and checked against digest."""
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / url.rsplit("/", 1)[1]
    if target.is_file() and sha256(target) == digest:
        return target
    print(f"downloading {url}", flush=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "ps5-fsr4-build-inputs"})
    with urllib.request.urlopen(request, timeout=120) as response, open(partial, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    actual = sha256(partial)
    if actual != digest:
        partial.unlink()
        raise SystemExit(f"{url}: SHA-256 {actual}, expected {digest}")
    partial.replace(target)
    return target


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def checkout(repository, commit, path, submodules=False):
    """A checkout of repository at commit; an existing one at that commit is kept as it is."""
    path = ROOT / path
    if (path / ".git").exists() and git("rev-parse", "HEAD", cwd=path) == commit:
        return path
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    print(f"fetching {repository} at {commit}", flush=True)
    git("init", "-q", cwd=path)
    git("remote", "add", "origin", repository, cwd=path)
    git("fetch", "-q", "--depth", "1", "origin", commit, cwd=path)
    git("checkout", "-q", "FETCH_HEAD", cwd=path)
    if submodules:
        git("submodule", "update", "-q", "--init", "--recursive", "--depth", "1", cwd=path)
    return path


def extract_tar(archive, members, path):
    """The members (paths below the archive's top directory), in one pass over the archive."""
    path = ROOT / path
    if all((path / m).is_file() for m in members):
        return path
    found = set()
    with tarfile.open(archive, "r|*") as tar:
        for info in tar:
            member = info.name.split("/", 1)[-1]
            if member not in members or not info.isfile():
                continue
            (path / member).parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(info) as source, open(path / member, "wb") as out:
                shutil.copyfileobj(source, out)
            (path / member).chmod(info.mode & 0o777)
            found.add(member)
    if found != set(members):
        raise SystemExit(f"{archive.name} lacks {sorted(set(members) - found)}")
    return path


def extract_zip(archive, members, path):
    path = ROOT / path
    with zipfile.ZipFile(archive) as z:
        for member in members:
            target = path / member
            data = z.read(member)
            if not target.is_file() or target.read_bytes() != data:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    return path


def build_template(path):
    """The template's payload SDK, generated libc.prx and host tool, from its own scripts.

    Its sample app is not built: at the pinned commit, LLVM 21's lld leaves that small
    image without the .data.rel.ro section the tool requires.
    """
    tool = path / "build/host/ps5-native-tool"
    needed = [tool, path / "runtime/libc.prx", path / ".deps/native/ps5-payload-sdk/bin/prospero-clang"]
    if not all(p.is_file() for p in needed):
        print("building the native app template's SDK, runtime and tool", flush=True)
        subprocess.run(["bash", "tools/setup-native-dependencies.sh"], cwd=path, check=True)
        subprocess.run(["make", "-C", str(path), "runtime/libc.prx"], check=True)
        # rebuild-libc.sh compiles the tool as tools/build.sh does, into its work folder.
        tool.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path / "build/runtime-shim/ps5-native-tool", tool)
    missing = [str(p) for p in needed if not p.is_file()]
    if missing:
        raise SystemExit("the native app template build did not produce " + ", ".join(missing))


def environment(pins):
    """The template sits in a lab layout (lab/third_party/...), where the driver's SDK builder looks."""
    template = ROOT / pins["app_template"]["path"]
    return {"PS5VK_LAB_ROOT": str(template.parents[1]), "PS5_NATIVE_APP_TEMPLATE": str(template),
            "PS5_PAYLOAD_SDK": str(template / ".deps/native/ps5-payload-sdk"),
            "DXIL_SPIRV_DIR": str(ROOT / pins["dxil_spirv"]["path"])}


def driver_constants(header=DRIVER_PROFILE):
    text = header.read_text()
    values = {}
    for name in UUID_INPUTS:
        match = re.search(rf"#define\s+{name}\s+(0x[0-9a-fA-F]+|\d+)u?\b", text)
        if not match:
            raise SystemExit(f"{header}: no {name}")
        values[name] = int(match.group(1), 0)
    return values


def cache_uuid(vendor_id, device_id, constants):
    """ps5vk_pipeline_cache_uuid() from the driver's physical_device_profile.h."""
    mask = (1 << 64) - 1

    def mix(h, value):
        for byte in range(4):
            h ^= (value >> (8 * byte)) & 0xFF
            h = (h * 0x100000001B3) & mask
        return h

    tag = b"ps5vk-gfx1013-pipeline-cache"
    a, b = 0xCBF29CE484222325, 0x9E3779B97F4A7C15
    for c in tag:
        a = mix(a, c)
    for c in reversed(tag):
        b = mix(b, c)
    a, b = mix(a, vendor_id), mix(b, device_id)
    a, b = mix(a, device_id), mix(b, vendor_id)
    target = constants["PS5VK_GFX_TARGET"]
    a, b = mix(a, target), mix(b, target ^ 0x5A5A5A5A)
    for name in UUID_INPUTS[1:]:
        a, b = mix(a, constants[name]), mix(b, constants[name])
    return (a.to_bytes(8, "little") + b.to_bytes(8, "little")).hex()


def check_cache(pins, runtime=ROOT / "build/fsr4-runtime"):
    """Problems that make the pinned cache miss for this build; empty when it fits."""
    pin = pins["pipeline_cache"]
    cache = ROOT / pin["path"]
    problems = []
    header = cache.read_bytes()[:32] if cache.is_file() else b""
    if len(header) < 32:
        return [f"{cache} is missing"]
    size, version, vendor, device = struct.unpack_from("<4I", header)
    expected = cache_uuid(vendor, device, driver_constants())
    if (size, version) != (32, 1) or header[16:32].hex() != expected:
        problems.append(f"the cache identity {header[16:32].hex()} is not this driver's ({expected}): "
                        "the driver's compiler identity changed")
    manifest = runtime / "manifest.json"
    table = json.loads(manifest.read_text())["table_sha256"] if manifest.is_file() else None
    if table != pin["table_sha256"]:
        problems.append(f"the pass tables ({table}) are not the ones the cache was saved for "
                        f"({pin['table_sha256']})")
    return problems


def pin_cache(source, pins):
    """Pins a cache a PS5 saved for the current driver and pass tables; returns its upload name."""
    data = Path(source).read_bytes()
    size, version, vendor, device = struct.unpack_from("<4I", data)
    expected = cache_uuid(vendor, device, driver_constants())
    if (size, version) != (32, 1) or data[16:32].hex() != expected:
        raise SystemExit(f"{source} was not saved by this driver (cache identity {data[16:32].hex()}, "
                         f"expected {expected})")
    digest = hashlib.sha256(data).hexdigest()
    name = f"pipeline-cache-ps5-{digest[:12]}.bin"
    pin = pins["pipeline_cache"]
    pin.update(url=pin["url"].rsplit("/", 1)[0] + "/" + name, sha256=digest, cache_uuid=expected,
               table_sha256=json.loads((ROOT / "build/fsr4-runtime/manifest.json").read_text())["table_sha256"])
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    (DOWNLOADS / name).write_bytes(data)
    (ROOT / pin["path"]).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / pin["path"]).write_bytes(data)
    PINS.write_text(json.dumps(pins, indent=2) + "\n")
    return DOWNLOADS / name


def fetch(pins, only):
    wanted = lambda name: not only or name in only  # noqa: E731
    if wanted("app_template"):
        pin = pins["app_template"]
        path = checkout(pin["repository"], pin["commit"], pin["path"])
        build_template(path)
    if wanted("dxil_spirv"):
        pin = pins["dxil_spirv"]
        checkout(pin["repository"], pin["commit"], pin["path"], submodules=True)
    if wanted("dxc"):
        pin = pins["dxc"]
        extract_tar(download(pin["url"], pin["sha256"]), pin["members"], pin["path"])
    if wanted("bc250_dll"):
        pin = pins["bc250_dll"]
        path = extract_zip(download(pin["url"], pin["sha256"]), pin["members"], pin["path"])
        dll = path / pin["members"][0]
        expected = json.loads(DLL_MAP.read_text())["dll_sha256"]
        if sha256(dll) != expected:
            raise SystemExit(f"{dll} is not the DLL tools/fsr4_dll_map.json pins ({expected})")
    if wanted("pipeline_cache"):
        pin = pins["pipeline_cache"]
        cache = ROOT / pin["path"]
        if not cache.is_file() or sha256(cache) != pin["sha256"]:
            cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(download(pin["url"], pin["sha256"]), cache)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", default=[],
                        choices=("app_template", "dxil_spirv", "dxc", "bc250_dll", "pipeline_cache"),
                        help="fetch only these inputs (repeatable)")
    parser.add_argument("--env", action="store_true", help="print the build variables and exit")
    parser.add_argument("--github-env", action="store_true", help="also append the build variables to $GITHUB_ENV")
    parser.add_argument("--check-cache", action="store_true",
                        help="verify the pinned pipeline cache against this driver and the generated pass tables")
    parser.add_argument("--pin-cache", type=Path, metavar="CACHE",
                        help="pin a pipeline cache a PS5 saved for this driver and the generated pass tables")
    args = parser.parse_args()
    pins = json.loads(PINS.read_text())
    variables = environment(pins)
    if args.env:
        print("\n".join(f"export {k}={v}" for k, v in variables.items()))
        return
    if args.pin_cache:
        upload = pin_cache(args.pin_cache, pins)
        print(f"pinned {upload.name} in {PINS.relative_to(ROOT)}; publish it before pushing the pin:\n"
              f"gh release upload build-inputs {upload} --repo blackbearreloaded/ps5-fsr4")
        return
    if args.check_cache:
        problems = check_cache(pins)
        for problem in problems:
            print("pipeline cache: " + problem, file=sys.stderr)
        if problems:
            raise SystemExit("regenerate the pipeline cache on a PS5 and update tools/build_inputs.json "
                             "(see docs/RELEASING.md)")
        print("pipeline cache: matches this driver and these pass tables")
        return
    fetch(pins, set(args.only))
    if args.github_env:
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in variables.items())
    print("\n".join(f"export {k}={v}" for k, v in variables.items()))


if __name__ == "__main__":
    main()
