/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Host-only FSR4 shader compiler witness. No GPU execution is implied. */
#include "libpsbc/psbc_compile.h"
#include "ps5vk_compiler.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    if (argc < 2 || argc > 2 + PSBC_MAX_DESCRIPTOR_BINDINGS) {
        fprintf(stderr, "usage: fsr4_compile_probe shader.spv [set:binding:type:count ...]\n"
                        "types: buffer, uniform-buffer, sampled-image, storage-image, sampler\n");
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
        .compute_buffer_spills = true,
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
    /* Optional synthetic layout: this proves descriptor-aware lowering only.
     * Actual array bounds and resource contents require the provider graph. */
    uint32_t set_bytes[PSBC_MAX_DESCRIPTOR_SETS] = {0};
    unsigned descriptor_count = 0;
    unsigned char seen[PSBC_MAX_DESCRIPTOR_SETS][PSBC_MAX_DESCRIPTOR_BINDINGS] = {{0}};
    VkDescriptorType vk_type[PSBC_MAX_DESCRIPTOR_SETS][PSBC_MAX_DESCRIPTOR_BINDINGS] = {{0}};
    uint32_t vk_count[PSBC_MAX_DESCRIPTOR_SETS][PSBC_MAX_DESCRIPTOR_BINDINGS] = {{0}};
    for (int arg = 2; arg < argc; ++arg) {
        unsigned set, binding, count;
        char type[32], trailing;
        if (sscanf(argv[arg], "%u:%u:%31[^:]:%u%c",
                   &set, &binding, type, &count, &trailing) != 4 ||
            set >= PSBC_MAX_DESCRIPTOR_SETS ||
            binding >= PSBC_MAX_DESCRIPTOR_BINDINGS ||
            !count || count > 1024 || seen[set][binding] ||
            count > 1024 - descriptor_count) {
            fprintf(stderr, "invalid descriptor declaration: %s\n", argv[arg]);
            free(words); return 2;
        }
        PsbcDescriptorType descriptor_type;
        VkDescriptorType vulkan_type;
        uint32_t stride;
        if (!strcmp(type, "buffer")) {
            descriptor_type = PSBC_DESCRIPTOR_STORAGE_BUFFER;
            vulkan_type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER; stride = 16;
        } else if (!strcmp(type, "uniform-buffer")) {
            descriptor_type = PSBC_DESCRIPTOR_UNIFORM_BUFFER;
            vulkan_type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER; stride = 16;
        } else if (!strcmp(type, "sampled-image")) {
            descriptor_type = PSBC_DESCRIPTOR_SAMPLED_IMAGE;
            vulkan_type = VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE; stride = 32;
        } else if (!strcmp(type, "storage-image")) {
            descriptor_type = PSBC_DESCRIPTOR_STORAGE_IMAGE;
            vulkan_type = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE; stride = 32;
        } else if (!strcmp(type, "sampler")) {
            descriptor_type = PSBC_DESCRIPTOR_SAMPLER;
            vulkan_type = VK_DESCRIPTOR_TYPE_SAMPLER; stride = 16;
        } else {
            fprintf(stderr, "unknown descriptor type: %s\n", type);
            free(words); return 2;
        }
        if (count > (UINT32_MAX - set_bytes[set]) / stride) {
            free(words); return 2;
        }
        PsbcDescriptorBinding *out = &options.descriptor_bindings[options.descriptor_binding_count++];
        *out = (PsbcDescriptorBinding){
            .set = (uint8_t)set, .binding = (uint8_t)binding,
            .type = descriptor_type, .array_size = count,
            .offset = set_bytes[set], .stride = stride,
        };
        set_bytes[set] += count * stride;
        descriptor_count += count;
        seen[set][binding] = 1;
        vk_type[set][binding] = vulkan_type;
        vk_count[set][binding] = count;
    }
    psbc_init();
    PsbcShaderOutput output = {0};
    PsbcResult result = psbc_compile_shader(words, (size_t)length, &options, &output);
    unsigned used_sets = 0;
    for (unsigned set = 0; set < PSBC_MAX_DESCRIPTOR_SETS; ++set)
        if (output.metadata.descriptor_set_valid[set]) used_sets |= 1u << set;
    VkResult adapter_result = VK_SUCCESS;
    struct ps5vk_compiled_program program = {0};
    uint32_t *code = NULL;
    if (argc > 2) {
        struct VkPipelineLayout_T layout = {
            .set_count = PSBC_MAX_DESCRIPTOR_SETS,
            .push_constant_size = output.metadata.push_constants_valid ?
                                  output.metadata.push_constant_size : 0,
        };
        for (unsigned set = 0; set < PSBC_MAX_DESCRIPTOR_SETS; ++set) {
            uint32_t prefix = 0;
            for (unsigned binding = 0; binding < PSBC_MAX_DESCRIPTOR_BINDINGS; ++binding) {
                struct ps5vk_binding *slot = &layout.sets[set].binding[binding];
                slot->first = prefix;
                if (seen[set][binding]) {
                    slot->count = vk_count[set][binding];
                    slot->stages = VK_SHADER_STAGE_COMPUTE_BIT;
                    layout.sets[set].type[binding] = vk_type[set][binding];
                    prefix += slot->count;
                }
            }
            layout.sets[set].count = prefix;
        }
        if (layout.push_constant_size > sizeof(layout.push_constant_stages) /
                                        sizeof(layout.push_constant_stages[0]) * 4) {
            psbc_free_output(&output);
            psbc_shutdown();
            free(words);
            return 2;
        }
        for (unsigned i = 0; i < (layout.push_constant_size + 3) / 4; ++i)
            layout.push_constant_stages[i] = VK_SHADER_STAGE_COMPUTE_BIT;
        const uint32_t features = PS5VK_FEATURE_STORAGE_BUFFER_8BIT |
            PS5VK_FEATURE_STORAGE_BUFFER_16BIT | PS5VK_FEATURE_SHADER_INT8_COMPUTE |
            PS5VK_FEATURE_SHADER_INT16 | PS5VK_FEATURE_BUFFER_DEVICE_ADDRESS;
        adapter_result = ps5vk_runtime_compile_compute_features(
            words, (size_t)length / 4, "main", &layout, NULL, features,
            &program, &code);
    }
    printf("{\"result\":\"%s\",\"code_bytes\":%zu,"
           "\"metadata_version\":%u,\"descriptors\":%u,"
           "\"used_sets\":%u,\"scratch_bytes_per_wave\":%u,"
           "\"scratch_bytes_per_thread\":%u,\"scratch_valid\":%s}\n",
           psbc_result_string(result), output.machine_code_size,
           output.metadata.version, output.metadata.descriptor_binding_count,
           used_sets, output.metadata.scratch_bytes_per_wave,
           output.metadata.scratch_size_per_thread,
           output.metadata.scratch_valid ? "true" : "false");
    if (argc > 2)
        printf("{\"adapter_result\":%d,\"adapter_code_bytes\":%zu,"
               "\"adapter_descriptors\":%u,\"adapter_push_bytes\":%u}\n",
               adapter_result, program.code_words * sizeof(uint32_t),
               program.descriptor_count, program.push_constant_size);
    const char *dump_path = getenv("PS5VK_PROBE_CODE_OUT");
    int dump_ok = 1;
    if (dump_path && result == PSBC_RESULT_OK) {
        FILE *dump = fopen(dump_path, "wb");
        if (!dump) { perror(dump_path); dump_ok = 0; }
        else {
            dump_ok = fwrite(output.machine_code, 1, output.machine_code_size, dump) ==
                      output.machine_code_size;
            if (fclose(dump)) dump_ok = 0;
        }
    }
    free(code);
    psbc_free_output(&output);
    psbc_shutdown();
    free(words);
    return dump_ok && result == PSBC_RESULT_OK && adapter_result == VK_SUCCESS ? 0 : 1;
}
