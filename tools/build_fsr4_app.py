#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the PS5 FSR4 Showcase app (PPSA99011) against the staged ps5_fsr4 SDK.

Stage the SDK first with make sdk. The HUD font and the city's signs are
rasterized here from DejaVu Sans (fonts-dejavu-core) with Pillow. Launch assets
in apps/showcase/sce_sys (icon0.png, pic0.dds, pic1.dds, snd0.at9) replace the
template's generic ones.
--host builds an off-screen binary for desktop Vulkan instead: it runs the
scripted walk and saves a frame of each step (see apps/showcase/README.md).
--release VERSION also writes ps5-fsr4-showcase-VERSION-PPSA99011.zip and its .sha256.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fsr4_paths import DIST_SDK, VULKAN_HEADERS  # noqa: E402
from build_fsr4_clear import build_native_app  # noqa: E402
from build_fsr4_showcase import build_font, compile_shaders  # noqa: E402
from fsr4_showcase_signs import build_signs  # noqa: E402

TITLE_ID = "PPSA99011"
TITLE_NAME = "PS5 FSR4 Showcase"
CONTENT_ID = "UP9000-PPSA99011_00-FSR4SHOWCASECITY"
SOURCE = ROOT / "apps/showcase"
# (name in the header, source, extra glslang arguments)
SHADERS = (("scene", SOURCE / "city.comp", ()),
           ("native", SOURCE / "city.comp", ("-DSCENE_COLOR_ONLY",)),
           ("compose", SOURCE / "compose.comp", ()),
           ("blit_vert", ROOT / "examples/fsr4_demo_blit.vert", ()),
           ("blit_frag", ROOT / "examples/fsr4_demo_blit.frag", ()))


def host_build(out, runtime, defines):
    """The off-screen desktop binary: the app, its HUD and the FSR4 runtime in one program."""
    binary = out / "fsr4_showcase"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Wno-format-truncation", "-DSHOWCASE_HOST",
                    "-D_DEFAULT_SOURCE", *("-D" + x for x in defines), "-I" + str(SOURCE), "-I" + str(out),
                    "-I" + str(VULKAN_HEADERS), "-I" + str(ROOT / "include"), "-I" + str(ROOT / "src"),
                    "-I" + str(runtime), str(SOURCE / "main.c"), str(SOURCE / "hud.c"), str(ROOT / "src/ps5_fsr4.c"),
                    "/usr/lib/x86_64-linux-gnu/libvulkan.so.1", "-lm", "-o", str(binary)], check=True)
    return binary


def release(out, version):
    """The package as ps5-fsr4-showcase-VERSION-PPSA99011.zip, with a README, notices/ and
    SHA256SUMS inside (tools/package_fsr4_release.py), and the zip's own SHA-256 beside it."""
    import package_fsr4_release
    name = f"ps5-fsr4-showcase-{version}-{TITLE_ID}.zip"
    archive = package_fsr4_release.showcase_zip(out / TITLE_ID, out / name, version)
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        for required in (*(f"{TITLE_ID}/{f}" for f in ("eboot.bin", "sce_module/libc.prx", "sce_sys/param.json",
                                                          "sce_sys/icon0.png", "sce_sys/pic0.dds", "sce_sys/pic1.dds",
                                                          "sce_sys/snd0.at9", "assets/pipeline-cache.bin",
                                                          "assets/signs.bin")),
                         "README.md", "SHA256SUMS", "notices/AMD-SDK-LICENSE.md", "notices/PROVENANCE.md"):
            if required not in names:
                raise SystemExit(f"{name} lacks {required}")
        param = json.loads(z.read(f"{TITLE_ID}/sce_sys/param.json"))
    if param["titleId"] != TITLE_ID or param["localizedParameters"]["en-US"]["titleName"] != TITLE_NAME:
        raise SystemExit("unexpected param.json")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / (name + ".sha256")).write_text(f"{digest}  {name}\n")
    return str(archive)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pipeline-cache", type=Path, default=DIST_SDK / "share/ps5fsr4/pipeline-cache-ps5.bin",
                        help="Pipeline cache data from an earlier run on this driver")
    parser.add_argument("--out", type=Path, help="Default: build/fsr4-app, or build/fsr4-app-host with --host")
    parser.add_argument("--host", action="store_true", help="Build the off-screen desktop binary")
    parser.add_argument("--runtime", type=Path, default=ROOT / "build/fsr4-runtime",
                        help="The generated FSR4 runtime tables a --host build compiles in")
    parser.add_argument("--selftest", action="store_true",
                        help="Replace the pad with a scripted walk that logs timings, saves frames and exits")
    parser.add_argument("--release", metavar="VERSION", help="Also write the release zip and its SHA-256")
    args = parser.parse_args()
    out = (args.out or ROOT / ("build/fsr4-app-host" if args.host else "build/fsr4-app")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    compile_shaders(out, SHADERS)
    font = build_font(out, SOURCE)
    assets = out / "assets" if args.host else out / TITLE_ID / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "signs.bin").write_bytes(build_signs().tobytes())
    defines = [*(["SHOWCASE_SELFTEST=1"] if args.selftest else []),
               *([f'SHOWCASE_VERSION="{args.release}"'] if args.release else [])]
    if args.host:
        print(json.dumps(dict(binary=str(host_build(out, args.runtime.resolve(), defines)), assets=str(assets),
                              font=font), indent=2))
        return
    library = DIST_SDK / "lib/libps5_fsr4.a"
    if not library.is_file():
        raise SystemExit(f"{library} is missing; run make sdk")
    cache = assets / "pipeline-cache.bin"
    if args.pipeline_cache and args.pipeline_cache.is_file():
        cache.write_bytes(args.pipeline_cache.read_bytes())
    elif cache.exists():
        cache.unlink()
    build_native_app(out, SOURCE / "main.c", TITLE_NAME, extra_sources=[SOURCE / "hud.c"],
                     include_dirs=[SOURCE], libraries=[library], defines=defines,
                     title_id=TITLE_ID, content_id=CONTENT_ID, sce_sys=SOURCE / "sce_sys")
    own_assets = sorted(p.name for p in (SOURCE / "sce_sys").glob("*") if p.is_file())
    result = dict(package=str(out / TITLE_ID), pipeline_cache=cache.exists(), selftest=args.selftest,
                  launch_assets=own_assets or "generic", font=font)
    if args.release:
        if args.selftest:
            raise SystemExit("a release is built without --selftest")
        result["release"] = release(out, args.release)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
