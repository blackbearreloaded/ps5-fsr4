/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Host-only FSR4 shader compiler witness. No GPU execution is implied. */
#include "libpsbc/psbc_compile.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: fsr4_compile_probe shader.spv\n");
        return 2;
    }
    FILE *file = fopen(argv[1], "rb");
    if (!file) { perror(argv[1]); return 2; }
    if (fseek(file, 0, SEEK_END)) { fclose(file); return 2; }
    long length = ftell(file);
    if (length < 20 || length > 16 * 1024 * 1024 || length % 4 ||
        fseek(file, 0, SEEK_SET)) { fclose(file); return 2; }
    uint32_t *words = malloc((size_t)length);
    if (!words) { fclose(file); return 2; }
    if (fread(words, 1, (size_t)length, file) != (size_t)length) {
        fclose(file); free(words); return 2;
    }
    fclose(file);
    if (words[0] != 0x07230203u) { free(words); return 2; }

    PsbcCompileOptions options = {
        .target = PSBC_TARGET_PS5,
        .stage = PSBC_STAGE_COMPUTE,
        .entrypoint = "main",
        .optimise = true,
        .address32_hi = 2,
        .robust_buffer_access2 = true,
        .static_descriptor_use = true,
        .enable_int8 = true,
        .enable_int16 = true,
        .enable_storage_buffer_8bit_access = true,
        .enable_uniform_and_storage_buffer_8bit_access = true,
        .enable_storage_buffer_16bit_access = true,
        .enable_uniform_and_storage_buffer_16bit_access = true,
        .enable_physical_storage_buffer_addresses = true,
    };
    psbc_init();
    PsbcShaderOutput output = {0};
    PsbcResult result = psbc_compile_shader(words, (size_t)length, &options, &output);
    unsigned used_sets = 0;
    for (unsigned set = 0; set < PSBC_MAX_DESCRIPTOR_SETS; ++set)
        if (output.metadata.descriptor_set_valid[set]) used_sets |= 1u << set;
    printf("{\"result\":\"%s\",\"code_bytes\":%zu,"
           "\"metadata_version\":%u,\"descriptors\":%u,"
           "\"used_sets\":%u,\"scratch_bytes_per_wave\":%u,"
           "\"scratch_bytes_per_thread\":%u,\"scratch_valid\":%s}\n",
           psbc_result_string(result), output.machine_code_size,
           output.metadata.version, output.metadata.descriptor_binding_count,
           used_sets, output.metadata.scratch_bytes_per_wave,
           output.metadata.scratch_size_per_thread,
           output.metadata.scratch_valid ? "true" : "false");
    psbc_free_output(&output);
    psbc_shutdown();
    free(words);
    return result == PSBC_RESULT_OK ? 0 : 1;
}
