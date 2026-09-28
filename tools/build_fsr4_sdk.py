#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage the ps5_fsr4 SDK into dist-sdk and verify relocated C and C++ consumers.

Requires the staged Vulkan SDK (tools/build_sdk.py) and the generated FSR4 pass
tables (tools/build_fsr4_runtime.py). Adds to dist-sdk:
  include/ps5fsr4/ps5_fsr4.h
  lib/libps5_fsr4.a               native runtime; shaders and model data built in
  share/ps5fsr4/                  provenance, notices and an optional warmed
                                  pipeline cache for the staged libps5vk.a
The consumers compile against a relocated copy with only its include directory
and link with only its archives.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_consumer import DIST_SDK, native_inputs  # noqa: E402
from build_sdk import archive  # noqa: E402

CONSUMERS = (("fsr4_sdk_consumer.c", "-std=c11"), ("fsr4_sdk_consumer.cpp", "-std=c++20"))
NOTICE = """# ps5_fsr4 provenance and notices

`libps5_fsr4.a` is GPL-3.0-or-later code by BlackBearReloaded (see LICENSE and
LICENSING.md at the SDK root). It embeds FSR 4.1.1 INT8 shaders and model data
translated from the AMD FidelityFX SDK upscaler as modified by the BC250 FSR4
project (dmoraza and daniel-h-0 with contributors). AMD / GPUOpen created FSR4.
That material keeps its original terms; `AMD-SDK-LICENSE.md` reproduces the AMD
notice. `passes.json` identifies every embedded pass by DXIL and SPIR-V digest.
"""


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compiler_version():
    text = (ROOT / "src/compilation_cache.h").read_text()
    return int(re.search(r"#define PS5VK_COMPILER_VERSION\s+UINT32_C\((\d+)\)", text).group(1))


def link_consumer(sdk, env, lib_dir, crt, obj, out_elf):
    subprocess.run([str(sdk / "bin/prospero-lld"), "-L" + str(sdk / "target/lib"),
                    "-T", str(lib_dir / "ps5-pie.ld"), "--eh-frame-hdr",
                    "--version-script", str(lib_dir / "app-symbols.map"),
                    "-e", "_start", "-o", str(out_elf), str(crt), str(obj),
                    str(lib_dir / "libps5_fsr4.a"), str(lib_dir / "libps5vk.a"), str(lib_dir / "libpsbc.a"),
                    *[str(sdk / "target/lib" / n) for n in ("libc++.a", "libc++abi.a", "libunwind.a", "libc.a")],
                    "--as-needed", *sorted(str(x) for x in (sdk / "target/lib").glob("*.so")),
                    str(lib_dir / "libSceAgc.so"), str(lib_dir / "libSceAgcDriver.so")],
                   env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT / "build/fsr4-runtime",
                        help="Generated pass tables (tools/build_fsr4_runtime.py)")
    parser.add_argument("--pipeline-cache", type=Path, default=ROOT / "build/fsr4-runtime/pipeline-cache-ps5.bin",
                        help="Pipeline cache saved on the PS5 by this libps5vk build (optional)")
    parser.add_argument("--amd-notice", type=Path,
                        default=ROOT.parent / "references/bc250-fsr4-fork/dll/notices/AMD-SDK-LICENSE.md",
                        help="AMD FidelityFX SDK notice from the pinned BC250 FSR4 checkout")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    required = [DIST_SDK / "lib/libps5vk.a", DIST_SDK / "lib/libpsbc.a", runtime / "fsr4_passes.h",
                runtime / "manifest.json", args.amd_notice]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise SystemExit("Missing inputs: " + ", ".join(missing) +
                         " (run tools/build_sdk.py and tools/build_fsr4_runtime.py first)")
    foundation, sdk, compiler, _, _ = native_inputs()
    env = dict(os.environ, PS5_PAYLOAD_SDK=str(sdk))
    work = ROOT / "build/fsr4-sdk"
    work.mkdir(parents=True, exist_ok=True)

    # 1. Public header, then the native runtime archive built against it.
    (DIST_SDK / "include/ps5fsr4").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "include/ps5fsr4/ps5_fsr4.h", DIST_SDK / "include/ps5fsr4/ps5_fsr4.h")
    obj = work / "ps5_fsr4.o"
    subprocess.run([str(compiler), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                    "-ffunction-sections", "-fdata-sections", "-I" + str(DIST_SDK / "include"),
                    "-I" + str(ROOT / "src/fsr4"), "-I" + str(runtime),
                    "-c", str(ROOT / "src/fsr4/ps5_fsr4.c"), "-o", str(obj)], env=env, check=True)
    ar = sdk / "bin/prospero-ar"
    archive(str(ar) if ar.is_file() else "ar", DIST_SDK / "lib/libps5_fsr4.a", [str(obj)])

    # 2. Provenance and notices.
    share = DIST_SDK / "share/ps5fsr4"
    if share.exists():
        shutil.rmtree(share)
    share.mkdir(parents=True)
    shutil.copyfile(runtime / "manifest.json", share / "passes.json")
    shutil.copyfile(args.amd_notice, share / "AMD-SDK-LICENSE.md")
    (share / "NOTICE.md").write_text(NOTICE)
    identity = dict(schema=1, compiler_version=compiler_version(),
                    libps5vk_sha256=sha(DIST_SDK / "lib/libps5vk.a"),
                    libps5_fsr4_sha256=sha(DIST_SDK / "lib/libps5_fsr4.a"))
    if args.pipeline_cache.is_file():
        shutil.copyfile(args.pipeline_cache, share / "pipeline-cache-ps5.bin")
        identity["pipeline_cache_sha256"] = sha(share / "pipeline-cache-ps5.bin")
    (share / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")

    # 3. Relocated installation: independent C and C++ consumers.
    with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
        relocated = Path(temp) / "sdk"
        shutil.copytree(DIST_SDK, relocated)
        crt = Path(temp) / "crt.o"
        subprocess.run([str(sdk / "bin/prospero-clang++"), "-std=c++20", "-O2", "-fno-exceptions", "-fno-rtti",
                        "-c", str(foundation / "tooling/native/app_crt.cpp"), "-o", str(crt)], env=env, check=True)
        for source, standard in CONSUMERS:
            driver = compiler if source.endswith(".c") else sdk / "bin/prospero-clang++"
            consumer_obj, dep = Path(temp) / (source + ".o"), Path(temp) / (source + ".d")
            subprocess.run([str(driver), standard, "-O2", "-Wall", "-Wextra", "-Werror", "-MD", "-MF", str(dep),
                            "-I" + str(relocated / "include"), "-c", str(ROOT / "tests" / source),
                            "-o", str(consumer_obj)], env=env, check=True)
            leaked = [line for line in dep.read_text().split() if str(ROOT) in line and
                      not line.startswith(str(relocated)) and not line.endswith((".o:", source))]
            if leaked:
                raise SystemExit(f"{source} used headers outside the relocated SDK: {leaked}")
            link_consumer(sdk, env, relocated / "lib", crt, consumer_obj, work / (source + ".elf"))
            print(f"FSR4 SDK consumer {source}: linked against the relocated SDK only")
    print(json.dumps(dict(sdk=str(DIST_SDK), **identity), indent=2))


if __name__ == "__main__":
    main()
