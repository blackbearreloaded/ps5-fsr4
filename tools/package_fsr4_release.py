#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Package a PS5 FSR4 release from the builds in this tree.

After make sdk and make showcase SHOWCASE_ARGS="--release VERSION", this writes to --out:

- ps5-fsr4-showcase-VERSION-PPSA99010.zip: the showcase app, from build/fsr4-showcase;
- ps5-fsr4-sdk-VERSION.zip: the staged SDK (dist-sdk) with its guide;
- ps5-fsr4-VERSION-source.tar.gz: the corresponding source of the GPL code in both,
  this repository and the Vulkan driver at the built commits, with ps5-agc-gears and
  the native app template at their pins;
- SHA256SUMS and release-notes.md for the GitHub release.

Both zips carry notices/ (AMD's notice for the FSR4 material, its provenance and the
licenses of what they contain), a README and their own SHA256SUMS.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "external/ps5-vulkan"
DIST_SDK = ROOT / "dist-sdk"
REPOSITORY = "blackbearreloaded/ps5-fsr4"
PINS = json.loads((ROOT / "tools/build_inputs.json").read_text())
TEMPLATE = ROOT / PINS["app_template"]["path"]
BC250 = ROOT / PINS["bc250_dll"]["path"]
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def git(*args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def revisions():
    return dict(commit=git("rev-parse", "HEAD"), driver=git("rev-parse", "HEAD", cwd=DRIVER),
                dll_zip=PINS["bc250_dll"]["sha256"],
                dll=json.loads((ROOT / "tools/fsr4_dll_map.json").read_text())["dll_sha256"])


def provenance():
    r = revisions()
    return f"""# Provenance of the FSR4 material

This package was built from https://github.com/{REPOSITORY} at `{r['commit']}`,
with the Vulkan driver https://github.com/blackbearreloaded/ps5-vulkan at `{r['driver']}`.

1. **AMD FidelityFX SDK v2.3.0**, commit `60f4ea81909200d8542eca14dccb2628b763a9a3`:
   AMD's FSR 4.1.1 upscaler, `Kits/FidelityFX/signedbin/amd_fidelityfx_upscaler_dx12.dll`,
   which `AMD-SDK-LICENSE.md` lists under its MIT terms.
2. **BC250 FSR4 RC11** (https://github.com/daniel-h-0/bc250-fsr4-fork, release
   `opticlient-v1.0.7-bc250.2`, `bc250-fsr4-dll-4.0.0-rc11-docs2.zip`, SHA-256
   `{r['dll_zip']}`): that DLL with the BC250 project's INT8 optimizations
   (DLL SHA-256 `{r['dll']}`). Its own provenance is `BC250-PROVENANCE.md`.
3. **ps5-fsr4**: the shaders and INT8 models of the standard and Ultra Performance
   families, for outputs up to 1920x1080 and up to 3840x2160, copied out of that DLL,
   converted from DXIL to SPIR-V with a patched dxil-spirv, partly replaced by INT8
   kernels generated with the models' weights, and embedded in `libps5_fsr4.a`. The
   pipeline cache holds them compiled for the PS5 GPU by the driver's compiler.

The FSR4 material is modified, is not signed by AMD and is not an official AMD
release. AMD / GPUOpen created FSR4. Credits: `LICENSING.md`.
"""


def third_party():
    return f"""# Third-party components

| Component | Where | License |
| --- | --- | --- |
| AMD FSR 4.1.1 shaders and models, modified | `libps5_fsr4.a`, the pipeline cache | MIT, `AMD-SDK-LICENSE.md` (see `PROVENANCE.md`) |
| ps5-vulkan (Manuel Pereira, BlackBearReloaded) | `libps5vk.a` | GPL-3.0-or-later, `ps5-vulkan-LICENSING.md` |
| ps5-agc-gears (Manuel Pereira) | `libps5vk.a` | GPL-3.0-or-later |
| opengnm-psbc (OpenGNM and Mesa contributors) | `libpsbc.a` | MIT, `opengnm-psbc-LICENSE` |
| ps5-native-app-boilerplate at `{PINS['app_template']['commit'][:12]}` (BlackBearReloaded) | app start-up code, `sce_module/libc.prx` | GPL-3.0-or-later |
| PS5 payload SDK v0.42 (https://github.com/ps5-payload-dev/sdk) | C and C++ runtime | its own terms |
| Vulkan-Headers (The Khronos Group) | SDK headers | Apache-2.0 or MIT, `Vulkan-Headers-LICENSE.md` |
| DejaVu Sans (showcase HUD text, rasterized) | the showcase's `eboot.bin` | Bitstream Vera, `DejaVu-LICENSE` |

The GPL code's corresponding source is the release's `ps5-fsr4-*-source.tar.gz`.
"""


def notices():
    """notices/ for both zips: name -> bytes."""
    files = {
        "AMD-SDK-LICENSE.md": DIST_SDK / "share/ps5fsr4/AMD-SDK-LICENSE.md",
        "BC250-PROVENANCE.md": BC250 / "notices/PROVENANCE.md",
        "LICENSE": ROOT / "LICENSE",
        "LICENSING.md": ROOT / "LICENSING.md",
        "ps5-vulkan-LICENSING.md": DRIVER / "LICENSING.md",
        "opengnm-psbc-LICENSE": DRIVER / "third_party/psbc-reference/LICENSE",
        "Vulkan-Headers-LICENSE.md": DRIVER / "third_party/vulkan-headers/LICENSE.md",
        "DejaVu-LICENSE": Path("/usr/share/doc/fonts-dejavu-core/copyright"),
    }
    missing = [str(p) for p in files.values() if not p.is_file()]
    if missing:
        raise SystemExit("missing notice sources: " + ", ".join(missing))
    out = {name: path.read_bytes() for name, path in files.items()}
    out["PROVENANCE.md"] = provenance().encode()
    out["THIRD-PARTY.md"] = third_party().encode()
    return out


def write_zip(path, entries):
    """A reproducible zip of entries (archive name -> bytes), with SHA256SUMS of them added."""
    sums = "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(entries.items()))
    entries = dict(entries, SHA256SUMS=sums.encode())
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, entries[name])
    return path


def tree(folder, prefix):
    return {f"{prefix}/{p.relative_to(folder).as_posix()}": p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def showcase_readme(version):
    r = revisions()
    return f"""# PS5 FSR4 Showcase {version}

A native PS5 app (title PPSA99010) that shows AMD's FSR 4 upscaler running on the PS5
GPU through the ps5_fsr4 SDK: every quality mode, 1080p, 1440p and 4K output, dynamic
resolution, sharpening, and side-by-side comparisons with a magnifier, in a guided tour.

## Install

1. Extract this archive.
2. Upload the `PPSA99010` folder to `/data/homebrew/` on a PS5 running a homebrew
   loader with ShadowMountPlus, for example over FTP.
3. Once the title is registered, start **PS5 FSR4 Showcase** from the home screen.

The app opens with a guided tour of about two minutes; any button takes over. Controls
and measured performance:
https://github.com/{REPOSITORY}/blob/{r['commit']}/examples/fsr4_showcase/README.md

## About this build

Built from https://github.com/{REPOSITORY} at `{r['commit']}`. Experimental and
unofficial: not affiliated with or endorsed by AMD or Sony Interactive Entertainment.
`SHA256SUMS` lists every file. `notices/` holds the licenses and the provenance of the
FSR4 material: AMD's FSR 4.1.1 with the BC250 project's INT8 optimizations, converted
for the PS5.
"""


def showcase_zip(package, path, version):
    """The showcase app folder with its README, notices and checksums."""
    entries = tree(package, package.name)
    entries["README.md"] = showcase_readme(version).encode()
    entries.update({f"notices/{name}": data for name, data in notices().items()})
    return write_zip(path, entries)


def sdk_zip(path, version):
    top = f"ps5-fsr4-sdk-{version}"
    entries = tree(DIST_SDK, top)
    identity = json.loads((DIST_SDK / "share/ps5fsr4/identity.json").read_text())
    if "pipeline_cache_sha256" not in identity:
        raise SystemExit("dist-sdk has no pipeline cache; fetch the pinned one before make sdk")
    entries[f"{top}/FSR4_SDK.md"] = (ROOT / "docs/FSR4_SDK.md").read_bytes()
    entries[f"{top}/README.md"] = f"""# ps5_fsr4 SDK {version}

AMD FSR 4.1.1 (INT8) for native PS5 homebrew on the ps5-vulkan driver:

- `include/ps5fsr4/ps5_fsr4.h` and `lib/libps5_fsr4.a`, the upscaler;
- the driver SDK it runs on: `lib/libps5vk.a`, `lib/libpsbc.a`, the Vulkan and
  `ps5vk` headers, the link script and symbol map;
- `share/ps5fsr4/pipeline-cache-ps5.bin`, compiled on a PS5 for this build, and
  `identity.json`, the digests of the archives it belongs to.

`FSR4_SDK.md` explains how to create a device, link an application and upscale.
Build applications with the PS5 payload SDK v0.42 and the native app template, as
https://github.com/{REPOSITORY} does. Experimental and unofficial; see `notices/`.
""".encode()
    entries.update({f"{top}/notices/{name}": data for name, data in notices().items()})
    return write_zip(path, entries)


def listed(repository, prefix, exclude=()):
    """(archive name, path) of the files git tracks in repository, working-tree contents."""
    names = subprocess.run(["git", "ls-files", "-z"], cwd=repository, check=True,
                           capture_output=True).stdout.decode().split("\0")
    for name in sorted(n for n in names if n and not n.startswith(tuple(exclude))):
        path = repository / name
        if path.is_file() and not path.is_symlink():
            yield f"{prefix}/{name}", path


def source_archive(path, version):
    top = f"ps5-fsr4-{version}"
    gears = DRIVER / "third_party/ps5-agc-gears"
    members = [*listed(ROOT, top, exclude=("comparisons/", "external/ps5-vulkan")),
               *listed(DRIVER, f"{top}/external/ps5-vulkan"),
               *listed(gears, f"{top}/external/ps5-vulkan/third_party/ps5-agc-gears"),
               *listed(TEMPLATE, f"{top}/third_party/ps5-native-app-boilerplate")]
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, file in members:
            info = tarfile.TarInfo(name)
            data = file.read_bytes()
            info.size, info.mtime = len(data), 0
            info.mode = 0o755 if file.stat().st_mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(data))
    with open(path, "wb") as out, gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as z:
        z.write(raw.getvalue())
    return path


def release_notes(version, assets):
    r = revisions()
    rows = {
        f"ps5-fsr4-showcase-{version}-PPSA99010.zip": "The PS5 FSR4 Showcase app, ready to install (see its README)",
        f"ps5-fsr4-sdk-{version}.zip": "The ps5_fsr4 SDK for native PS5 applications",
        f"ps5-fsr4-{version}-source.tar.gz": "Corresponding source: this repository with its Vulkan driver, "
                                             "ps5-agc-gears and the native app template",
        "SHA256SUMS": "SHA-256 of the assets",
    }
    table = "\n".join(f"| `{name}` | {rows[name]} |" for name in [*assets, "SHA256SUMS"])
    return f"""Experimental pre-release of the PS5 FSR4 port, built by GitHub Actions from
`{r['commit']}` (Vulkan driver `{r['driver']}`).

| Asset | Contents |
| --- | --- |
{table}

**Install the showcase:** extract the showcase zip, upload its `PPSA99010` folder to
`/data/homebrew/` on a PS5 running a homebrew loader with ShadowMountPlus, and start
**PS5 FSR4 Showcase** from the home screen once it is registered. It opens with a guided
tour; [controls and performance](https://github.com/{REPOSITORY}/blob/{r['commit']}/examples/fsr4_showcase/README.md).

**Use the SDK:** see `FSR4_SDK.md` in the SDK zip.

AMD / GPUOpen created FSR4. The INT8 optimizations come from the BC250 FSR4 project
(dmoraza and daniel-h-0 with contributors). This build is modified, unofficial and not
affiliated with or endorsed by AMD or Sony Interactive Entertainment. Each zip's
`notices/` holds AMD's license and the provenance of the FSR4 material.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "build/release")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    showcase = ROOT / f"build/fsr4-showcase/ps5-fsr4-showcase-{args.version}-PPSA99010.zip"
    if not showcase.is_file():
        raise SystemExit(f"{showcase} is missing; run make showcase SHOWCASE_ARGS='--release {args.version}'")
    assets = [shutil.copyfile(showcase, out / showcase.name),
              sdk_zip(out / f"ps5-fsr4-sdk-{args.version}.zip", args.version),
              source_archive(out / f"ps5-fsr4-{args.version}-source.tar.gz", args.version)]
    (out / "SHA256SUMS").write_text("".join(f"{sha256(Path(a).read_bytes())}  {Path(a).name}\n" for a in assets))
    (out / "release-notes.md").write_text(release_notes(args.version, [Path(a).name for a in assets]))
    if git("status", "--porcelain", "--untracked-files=no"):
        print("warning: the working tree has uncommitted changes", file=sys.stderr)
    print(json.dumps({Path(a).name: Path(a).stat().st_size for a in assets}, indent=2))


if __name__ == "__main__":
    main()
