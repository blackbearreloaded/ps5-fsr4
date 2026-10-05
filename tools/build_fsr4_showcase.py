#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the PS5 FSR4 Showcase app (PPSA99010) against the staged ps5_fsr4 SDK.

Stage the SDK first with make sdk. The HUD font is rasterized here from DejaVu
Sans (fonts-dejavu-core) with Pillow. Launch assets in examples/fsr4_showcase/sce_sys
(icon0.png, pic0.dds, pic1.dds, snd0.at9) replace the template's generic ones.
--release VERSION also writes ps5-fsr4-showcase-VERSION-PPSA99010.zip and its .sha256.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fsr4_paths import DIST_SDK, TOOLCHAIN_BIN  # noqa: E402
from build_fsr4_clear import build_native_app  # noqa: E402

TITLE_ID = "PPSA99010"
TITLE_NAME = "PS5 FSR4 Showcase"
CONTENT_ID = "UP9000-PPSA99010_00-PS5FSR4SHOWCASE1"
SOURCE = ROOT / "examples/fsr4_showcase"
# (name in the header, source, extra glslang arguments)
SHADERS = (("scene", ROOT / "examples/fsr4_demo_scene.comp", ()),
           ("native", ROOT / "examples/fsr4_demo_scene.comp", ("-DSCENE_COLOR_ONLY",)),
           ("compose", SOURCE / "compose.comp", ()),
           ("blit_vert", ROOT / "examples/fsr4_demo_blit.vert", ()),
           ("blit_frag", ROOT / "examples/fsr4_demo_blit.frag", ()))
FONT_DIRS = (Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"))
# HUD faces: (name, font file, pixel size); hud.c expects these three.
FACES = (("title", "DejaVuSans-Bold.ttf", 38), ("body", "DejaVuSans.ttf", 25), ("small", "DejaVuSans.ttf", 19))
ATLAS_W = 1024


def glslang():
    local = TOOLCHAIN_BIN / "glslangValidator"
    return str(local) if local.exists() else os.environ.get("GLSLANG", "glslangValidator")


def compile_shaders(out, shaders=SHADERS):
    header = ["#include <stdint.h>"]
    for name, source, extra in shaders:
        spv = out / f"fsr4_showcase_{name}.spv"
        subprocess.run([glslang(), "-V", "--target-env", "vulkan1.1", *extra, str(source), "-o", str(spv)],
                       check=True, stdout=subprocess.DEVNULL)
        words = struct.unpack(f"<{spv.stat().st_size // 4}I", spv.read_bytes())
        header.append(f"static const uint32_t fsr4_showcase_{name}_spv[] = {{" + ",".join(map(hex, words)) + "};")
    (out / "fsr4_showcase_shaders.h").write_text("\n".join(header) + "\n")


def font_path(name):
    for folder in FONT_DIRS:
        if (folder / name).is_file():
            return folder / name
    raise SystemExit(f"{name} is missing; install fonts-dejavu-core")


def build_font(out, source=SOURCE):
    """fsr4_showcase_font.h: an 8-bit coverage atlas of every character the app draws."""
    from PIL import Image, ImageDraw, ImageFont
    text = "".join(p.read_text(encoding="utf-8") for p in sorted(source.glob("*.c")))
    codepoints = sorted(set(range(32, 127)) | {ord(c) for c in text if ord(c) > 127})
    def render(font, char):
        left, top, right, bottom = font.getbbox(char)  # from the origin at the ascent line
        image = Image.new("L", (max(right - left, 0), max(bottom - top, 0)))
        if image.width and image.height:
            ImageDraw.Draw(image).text((-left, -top), char, font=font, fill=255)
        return left, top, image

    glyphs, fonts = [], []
    for face, file, size in FACES:
        font = ImageFont.truetype(str(font_path(file)), size)
        _, _, missing = render(font, "\U0010fffd")  # the .notdef box
        ascent, descent = font.getmetrics()
        fonts.append((face, ascent + descent + 2, len(glyphs)))
        for codepoint in codepoints:
            char = chr(codepoint)
            left, top, image = render(font, char)
            if codepoint > 127 and image.tobytes() == missing.tobytes():
                raise SystemExit(f"{file} has no glyph for U+{codepoint:04X}")
            glyphs.append(dict(codepoint=codepoint, image=image, xoff=left, yoff=top,
                               advance=round(font.getlength(char))))
    # Shelf packing, tallest first.
    x = y = shelf = 0
    for glyph in sorted(glyphs, key=lambda g: -g["image"].height):
        w, h = glyph["image"].size
        if x + w > ATLAS_W:
            x, y, shelf = 0, y + shelf + 1, 0
        glyph["x"], glyph["y"] = x, y
        x, shelf = x + w + 1, max(shelf, h)
    atlas = Image.new("L", (ATLAS_W, y + shelf))
    for glyph in glyphs:
        atlas.paste(glyph["image"], (glyph["x"], glyph["y"]))
    lines = ["#include <stdint.h>",
             f"enum {{ HUD_ATLAS_W = {ATLAS_W}, HUD_ATLAS_H = {atlas.height} }};",
             "struct hud_glyph { uint32_t codepoint; uint16_t x, y, w, h; int16_t xoff, yoff, advance; };",
             "struct hud_font { int line_height; int count; const struct hud_glyph *glyphs; };",
             "static const uint8_t hud_atlas[] = {" + ",".join(map(str, atlas.tobytes())) + "};"]
    for n, (face, line_height, first) in enumerate(fonts):
        members = glyphs[first:first + len(codepoints)]
        lines.append(f"static const struct hud_glyph hud_glyphs_{face}[] = {{" + ",".join(
            "{%d,%d,%d,%d,%d,%d,%d,%d}" % (g["codepoint"], g["x"], g["y"], *g["image"].size, g["xoff"], g["yoff"],
                                           g["advance"]) for g in members) + "};")
        lines.append(f"static const struct hud_font hud_font_{face} = {{{line_height}, {len(members)}, "
                     f"hud_glyphs_{face}}};")
    (out / "fsr4_showcase_font.h").write_text("\n".join(lines) + "\n")
    return dict(glyphs=len(codepoints), atlas=f"{ATLAS_W}x{atlas.height}")


def release(out, version):
    """The package as ps5-fsr4-showcase-VERSION-PPSA99010.zip, with a README, notices/ and
    SHA256SUMS inside (tools/package_fsr4_release.py), and the zip's own SHA-256 beside it."""
    import package_fsr4_release
    name = f"ps5-fsr4-showcase-{version}-{TITLE_ID}.zip"
    archive = package_fsr4_release.showcase_zip(out / TITLE_ID, out / name, version)
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        for required in (*(f"{TITLE_ID}/{f}" for f in ("eboot.bin", "sce_module/libc.prx", "sce_sys/param.json",
                                                          "sce_sys/icon0.png", "sce_sys/pic0.dds", "sce_sys/pic1.dds",
                                                          "sce_sys/snd0.at9", "assets/pipeline-cache.bin")),
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
    parser.add_argument("--out", type=Path, default=ROOT / "build/fsr4-showcase")
    parser.add_argument("--screenshots", action="store_true",
                        help="Save a BGRA frame of each chapter during the first tour (documentation builds)")
    parser.add_argument("--selftest", action="store_true",
                        help="Replace the pad with a scripted walk over every setting (validation builds)")
    parser.add_argument("--release", metavar="VERSION", help="Also write the release zip and its SHA-256")
    args = parser.parse_args()
    library = DIST_SDK / "lib/libps5_fsr4.a"
    if not library.is_file():
        raise SystemExit(f"{library} is missing; run make sdk")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    compile_shaders(out)
    font = build_font(out)
    assets = out / TITLE_ID / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    cache = assets / "pipeline-cache.bin"
    if args.pipeline_cache and args.pipeline_cache.is_file():
        cache.write_bytes(args.pipeline_cache.read_bytes())
    elif cache.exists():
        cache.unlink()
    build_native_app(out, SOURCE / "main.c", TITLE_NAME, extra_sources=[SOURCE / "hud.c"],
                     include_dirs=[SOURCE], libraries=[library],
                     defines=[*(["FSR4_SHOWCASE_SCREENSHOTS=1"] if args.screenshots else []),
                              *(["FSR4_SHOWCASE_SELFTEST=1"] if args.selftest else [])],
                     title_id=TITLE_ID, content_id=CONTENT_ID, sce_sys=SOURCE / "sce_sys")
    own_assets = sorted(p.name for p in (SOURCE / "sce_sys").glob("*") if p.is_file())
    result = dict(package=str(out / TITLE_ID), pipeline_cache=cache.exists(), screenshots=args.screenshots,
                  selftest=args.selftest, launch_assets=own_assets or "generic", font=font)
    if args.release:
        if args.screenshots or args.selftest:
            raise SystemExit("a release is built without --screenshots or --selftest")
        result["release"] = release(out, args.release)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
