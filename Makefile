# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
#
# FSR4 for PS5 on the ps5-vulkan driver (the external/ps5-vulkan submodule).
PYTHON ?= python3
CC ?= cc
DRIVER ?= external/ps5-vulkan
DXIL_SPIRV_DIR ?= ../references/dxil-spirv
# The driver profile FSR4 needs: INT8/INT16 shader arithmetic, the subgroup vote,
# subgroup size control and target-size sampled storage images.
DRIVER_PROFILE = PS5VK_SHADER_INT8_DIAGNOSTIC=1 PS5VK_SHADER_INT16_DIAGNOSTIC=1 \
	PS5VK_SUBGROUP_ALL_DIAGNOSTIC=1 PS5VK_EXTENDED_COMPUTE_DIAGNOSTIC=1
VULKAN_CFLAGS = -I$(DRIVER)/third_party/vulkan-headers/include

.PHONY: all driver driver-deps driver-psbc driver-sdk fsr4-dxil-converter runtime sdk demo check
all: sdk

# Driver: pinned dependencies, the PSBC compiler (PS5 and host), then its staged SDK.
driver: driver-deps driver-psbc driver-sdk
driver-deps:
	$(MAKE) -C $(DRIVER) vulkan-headers native-deps compiler-deps
driver-psbc:
	cd $(DRIVER) && $(PYTHON) tools/build_psbc.py --target ps5 && $(PYTHON) tools/build_psbc.py --host
driver-sdk:
	cd $(DRIVER) && $(DRIVER_PROFILE) $(PYTHON) tools/build_sdk.py
$(DRIVER)/build/libpsbc.host.a:
	cd $(DRIVER) && $(PYTHON) tools/build_psbc.py --host

# Offline converter for the exact captured shaders. This dependency is host-only.
fsr4-dxil-converter:
	test "$$(git -C "$(DXIL_SPIRV_DIR)" rev-parse HEAD)" = f2d1b5541eac934e9c32e8aa664328915336a213
	$(PYTHON) tools/prepare_fsr4_converter.py "$(DXIL_SPIRV_DIR)"
	cmake -S "$(DXIL_SPIRV_DIR)" -B build/dxil-spirv -G Ninja -DCMAKE_BUILD_TYPE=Release -DSPIRV_SKIP_TESTS=ON -DCMAKE_CXX_FLAGS=-DPS5_FSR4_REFERENCE_FP16=1
	cmake --build build/dxil-spirv --target dxil-spirv-c-shared -j 4
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -I"$(DXIL_SPIRV_DIR)" \
		tools/fsr4_dxil_to_spirv.c -Lbuild/dxil-spirv -ldxil-spirv-c-shared \
		-Wl,-rpath,'$$ORIGIN/dxil-spirv' -o build/fsr4_dxil_to_spirv
	build/fsr4_dxil_to_spirv --self-test

# Pass tables from the local reference exports, then dist-sdk: the driver SDK plus libps5_fsr4.a.
runtime: fsr4-dxil-converter
	$(PYTHON) tools/build_fsr4_runtime.py
sdk: runtime
	$(PYTHON) tools/build_fsr4_sdk.py
demo:
	$(PYTHON) tools/build_fsr4_demo.py

# Compile captured FSR4 SPIR-V against the pinned PS5 compiler on the host.
.PHONY: fsr4-compile-probe fsr4-family-inventory fsr4-provider-inventory fsr4-poststage-audit fsr4-initializers
fsr4-compile-probe: build/fsr4_compile_probe
fsr4-family-inventory: fsr4-compile-probe
	$(PYTHON) tools/fsr4_compile_inventory.py
fsr4-initializers:
	$(PYTHON) tools/fsr4_initializers.py
fsr4-provider-inventory:
	$(PYTHON) tools/fsr4_provider_inventory.py
fsr4-poststage-audit:
	$(PYTHON) tools/fsr4_poststage_audit.py

build/fsr4_compile_probe: tools/fsr4_compile_probe.c $(DRIVER)/src/ps5vk_compiler.c $(DRIVER)/build/libpsbc.host.a
	mkdir -p build
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror $(VULKAN_CFLAGS) -I$(DRIVER)/src -I$(DRIVER)/include \
		-I$(DRIVER)/third_party/opengnm/include -I$(DRIVER)/third_party/psbc-reference \
		$(DRIVER)/src/ps5vk_compiler.c $(DRIVER)/src/ps5_compiler_shims.c tools/fsr4_compile_probe.c \
		$(DRIVER)/build/libpsbc.host.a -lstdc++ -lm -lpthread -o $@

# Host tests. Tests that need the local reference exports, the converter or the
# payload SDK skip when those inputs are absent.
check:
	$(PYTHON) -m unittest discover -s tests -t . -p "test_fsr4_*.py"
