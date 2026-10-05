# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
#
# FSR4 for PS5 on the ps5-vulkan driver (the external/ps5-vulkan submodule).
PYTHON ?= python3
CC ?= cc
DRIVER ?= external/ps5-vulkan
DRIVER_URL ?= https://github.com/blackbearreloaded/ps5-vulkan.git
# The driver revision this checkout pins (its submodule commit); empty outside git.
DRIVER_REV ?= $(shell git ls-tree HEAD $(DRIVER) 2>/dev/null | awk '{print $$3}')
DXIL_SPIRV_DIR ?= ../references/dxil-spirv
# The driver profile FSR4 needs: INT8/INT16 shader arithmetic, the subgroup vote,
# subgroup size control and target-size sampled storage images.
DRIVER_PROFILE = PS5VK_SHADER_INT8_DIAGNOSTIC=1 PS5VK_SHADER_INT16_DIAGNOSTIC=1 \
	PS5VK_SUBGROUP_ALL_DIAGNOSTIC=1 PS5VK_EXTENDED_COMPUTE_DIAGNOSTIC=1
VULKAN_CFLAGS = -I$(DRIVER)/third_party/vulkan-headers/include

.PHONY: all driver driver-source driver-headers driver-deps driver-psbc driver-sdk fsr4-dxil-converter runtime sdk demo showcase inputs release check
all: sdk

# Fetch the driver when it is missing: the pinned submodule in a git checkout,
# otherwise a clone of DRIVER_URL at DRIVER_REV (or its default branch).
driver-source: $(DRIVER)/Makefile
$(DRIVER)/Makefile:
	@if [ -n "$(DRIVER_REV)" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
		git submodule update --init -- $(DRIVER); \
	else \
		rmdir $(DRIVER) 2>/dev/null || true; \
		git clone $(DRIVER_URL) $(DRIVER) && \
		{ [ -z "$(DRIVER_REV)" ] || git -C $(DRIVER) checkout -q $(DRIVER_REV); }; \
	fi
driver-headers: | $(DRIVER)/Makefile
	$(MAKE) -C $(DRIVER) vulkan-headers

# Driver: pinned dependencies, the PSBC compiler (PS5 and host), then its staged SDK.
driver: driver-deps driver-psbc driver-sdk
driver-deps: | $(DRIVER)/Makefile
	$(MAKE) -C $(DRIVER) vulkan-headers native-deps compiler-deps
driver-psbc: | $(DRIVER)/Makefile
	cd $(DRIVER) && $(PYTHON) tools/build_psbc.py --target ps5 && $(PYTHON) tools/build_psbc.py --host
driver-sdk: | $(DRIVER)/Makefile
	cd $(DRIVER) && $(DRIVER_PROFILE) $(PYTHON) tools/build_sdk.py
$(DRIVER)/build/libpsbc.host.a: | $(DRIVER)/Makefile
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

# Pass tables from the BC250 RC11 DLL, then dist-sdk: the driver SDK plus libps5_fsr4.a.
# The driver SDK is restaged first because the driver's own tests restage it without the profile.
runtime: fsr4-dxil-converter
	$(PYTHON) tools/build_fsr4_runtime.py
sdk: driver runtime
	$(PYTHON) tools/build_fsr4_sdk.py
demo:
	$(PYTHON) tools/build_fsr4_demo.py
# PPSA99011; SHOWCASE_ARGS takes --release VERSION, --selftest or --host.
showcase:
	$(PYTHON) tools/build_fsr4_showcase.py $(SHOWCASE_ARGS)
# Pinned public inputs (tools/build_inputs.json) into build/; eval "$$(python3 tools/fetch_build_inputs.py --env)"
# then points the builds at them.
inputs:
	$(PYTHON) tools/fetch_build_inputs.py
# VERSION=01.000.000, after make sdk: the release assets in build/release (docs/RELEASING.md).
release:
	test -n "$(VERSION)"
	$(PYTHON) tools/fetch_build_inputs.py --check-cache
	$(PYTHON) tools/build_fsr4_showcase.py --release $(VERSION)
	$(PYTHON) tools/package_fsr4_release.py --version $(VERSION)

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

build/fsr4_compile_probe: tools/fsr4_compile_probe.c | $(DRIVER)/Makefile $(DRIVER)/build/libpsbc.host.a
	mkdir -p build
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror $(VULKAN_CFLAGS) -I$(DRIVER)/src -I$(DRIVER)/include \
		-I$(DRIVER)/third_party/opengnm/include -I$(DRIVER)/third_party/psbc-reference \
		$(DRIVER)/src/ps5vk_compiler.c $(DRIVER)/src/ps5_compiler_shims.c tools/fsr4_compile_probe.c \
		$(DRIVER)/build/libpsbc.host.a -lstdc++ -lm -lpthread -o $@

# Host tests. Tests that need the local reference exports, the converter or the
# payload SDK skip when those inputs are absent.
check: driver-headers
	$(PYTHON) -m unittest discover -s tests -p "test_fsr4_*.py"
