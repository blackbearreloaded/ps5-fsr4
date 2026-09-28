# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Paths shared by the FSR4 tools: this repository and the ps5-vulkan driver.

The driver is the external/ps5-vulkan submodule unless PS5VK_ROOT names another
checkout. Importing this module makes the driver's tools importable
(build_consumer, build_sdk). dist-sdk here is the driver's staged SDK plus the
FSR4 runtime that tools/build_fsr4_sdk.py adds to it.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path(os.environ.get("PS5VK_ROOT", ROOT / "external/ps5-vulkan")).resolve()
DRIVER_SDK = DRIVER / "dist-sdk"
DIST_SDK = ROOT / "dist-sdk"
TOOLCHAIN_BIN = DRIVER / "build/runtime-graphics/toolchain/usr/bin"
if not (TOOLCHAIN_BIN / "spirv-val").exists() and shutil.which("spirv-val"):
    TOOLCHAIN_BIN = Path(shutil.which("spirv-val")).resolve().parent  # system SPIR-V tools and glslang
VULKAN_HEADERS = DRIVER / "third_party/vulkan-headers/include"
if str(DRIVER / "tools") not in sys.path:
    sys.path.append(str(DRIVER / "tools"))


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def stage_driver_sdk():
    """Copy the driver's staged SDK into dist-sdk when it changed, keeping the FSR4 files."""
    archive = DRIVER_SDK / "lib/libps5vk.a"
    if not archive.is_file():
        raise SystemExit(f"{archive} is missing; stage the driver SDK first (make driver-sdk)")
    if _digest(archive) != _digest(DIST_SDK / "lib/libps5vk.a"):
        shutil.copytree(DRIVER_SDK, DIST_SDK, dirs_exist_ok=True)
    return DIST_SDK
