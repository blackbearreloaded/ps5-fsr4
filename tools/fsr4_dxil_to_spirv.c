/*
 * Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Offline DXIL conversion for the captured FSR4 workload.
 * Sets 0/1/2/3 correspond to SRV/UAV/CBV/sampler, with unchanged registers.
 */
#include "dxil_spirv_c.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

static dxil_spv_bool map(const dxil_spv_d3d_binding *src,
                        dxil_spv_vulkan_binding *dst, unsigned set)
{
    /* This workload has only singleton resources in register space zero. */
    if (src->register_space || src->range_size != 1 || src->register_index >= 32)
        return DXIL_SPV_FALSE;
    *dst = (dxil_spv_vulkan_binding){0};
    dst->set = set;
    dst->binding = src->register_index;
    if (src->kind == DXIL_SPV_RESOURCE_KIND_RAW_BUFFER ||
        src->kind == DXIL_SPV_RESOURCE_KIND_STRUCTURED_BUFFER)
        dst->descriptor_type = DXIL_SPV_VULKAN_DESCRIPTOR_TYPE_SSBO;
    return DXIL_SPV_TRUE;
}

static dxil_spv_bool srv(void *unused, const dxil_spv_d3d_binding *src,
                        dxil_spv_srv_vulkan_binding *dst)
{
    (void)unused;
    *dst = (dxil_spv_srv_vulkan_binding){0};
    return map(src, &dst->buffer_binding, 0);
}

static dxil_spv_bool uav(void *unused, const dxil_spv_uav_d3d_binding *src,
                        dxil_spv_uav_vulkan_binding *dst)
{
    (void)unused;
    *dst = (dxil_spv_uav_vulkan_binding){0};
    return !src->has_counter && map(&src->d3d_binding, &dst->buffer_binding, 1);
}

static dxil_spv_bool cbv(void *unused, const dxil_spv_d3d_binding *src,
                        dxil_spv_cbv_vulkan_binding *dst)
{
    (void)unused;
    *dst = (dxil_spv_cbv_vulkan_binding){0};
    return map(src, &dst->vulkan.uniform_binding, 2);
}

static dxil_spv_bool sampler(void *unused, const dxil_spv_d3d_binding *src,
                            dxil_spv_vulkan_binding *dst)
{
    (void)unused;
    return map(src, dst, 3);
}

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--self-test")) {
        dxil_spv_d3d_binding in = {0};
        dxil_spv_vulkan_binding out;
        in.range_size = 1;
        in.register_index = 18;
        in.kind = DXIL_SPV_RESOURCE_KIND_RAW_BUFFER;
        assert(map(&in, &out, 0) && out.set == 0 && out.binding == 18 &&
               out.descriptor_type == DXIL_SPV_VULKAN_DESCRIPTOR_TYPE_SSBO);
        in.register_space = 1;
        assert(!map(&in, &out, 0));
        in.register_space = 0;
        in.range_size = 2;
        assert(!map(&in, &out, 0));
        in.range_size = 1;
        in.register_index = 32;
        assert(!map(&in, &out, 0));
        return 0;
    }
    if (argc != 3) {
        fprintf(stderr, "usage: fsr4_dxil_to_spirv input.dxil output.spv\n");
        return 2;
    }
    FILE *input = fopen(argv[1], "rb");
    if (!input) { perror(argv[1]); return 2; }
    if (fseek(input, 0, SEEK_END)) { fclose(input); return 2; }
    long size = ftell(input);
    if (size < 32 || size > 16 * 1024 * 1024 || fseek(input, 0, SEEK_SET)) {
        fclose(input);
        return 2;
    }
    void *data = malloc((size_t)size);
    if (!data) { fclose(input); return 2; }
    size_t read = fread(data, 1, (size_t)size, input);
    fclose(input);
    dxil_spv_parsed_blob parsed = NULL;
    dxil_spv_converter converter = NULL;
    int result = 1;
    if (read != (size_t)size ||
        dxil_spv_parse_dxil_blob(data, (size_t)size, &parsed) != DXIL_SPV_SUCCESS ||
        dxil_spv_create_converter(parsed, &converter) != DXIL_SPV_SUCCESS)
        goto cleanup;
    dxil_spv_converter_set_srv_remapper(converter, srv, NULL);
    dxil_spv_converter_set_uav_remapper(converter, uav, NULL);
    dxil_spv_converter_set_cbv_remapper(converter, cbv, NULL);
    dxil_spv_converter_set_sampler_remapper(converter, sampler, NULL);
    const dxil_spv_option_shader_i8_dot dot = {
        {DXIL_SPV_OPTION_SHADER_I8_DOT}, DXIL_SPV_TRUE
    };
    if (dxil_spv_converter_add_option(converter, &dot.base) != DXIL_SPV_SUCCESS)
        goto cleanup;
    /* D3D12 native FP16 preserves subnormals. Request the matching SPIR-V
     * execution mode rather than relying on a backend's default. */
    const dxil_spv_option_denorm_preserve_support denorm = {
        {DXIL_SPV_OPTION_DENORM_PRESERVE_SUPPORT}, DXIL_SPV_TRUE, DXIL_SPV_FALSE
    };
    if (dxil_spv_converter_add_option(converter, &denorm.base) != DXIL_SPV_SUCCESS)
        goto cleanup;
    dxil_spv_compiled_spirv compiled = {0};
    if (dxil_spv_converter_run(converter) != DXIL_SPV_SUCCESS ||
        dxil_spv_converter_get_compiled_spirv(converter, &compiled) != DXIL_SPV_SUCCESS)
        goto cleanup;
    FILE *output = fopen(argv[2], "wb");
    if (!output) { perror(argv[2]); goto cleanup; }
    size_t written = fwrite(compiled.data, 1, compiled.size, output);
    int closed = fclose(output);
    if (written != compiled.size || closed) {
        remove(argv[2]);
        goto cleanup;
    }
    result = 0;
cleanup:
    if (converter) dxil_spv_converter_free(converter);
    if (parsed) dxil_spv_parsed_blob_free(parsed);
    free(data);
    return result;
}
