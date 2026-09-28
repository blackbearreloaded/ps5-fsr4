/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Independent C consumer of the staged ps5_fsr4 SDK: it includes only the
 * public header and references every public entry point. tools/build_fsr4_sdk.py
 * links it against a relocated copy of the SDK; it is not executed.
 */
#include <ps5fsr4/ps5_fsr4.h>

int main(void)
{
    ps5fsr4_context_desc desc = {.struct_size = sizeof(desc), .max_render_width = 1280, .max_render_height = 720,
                                 .output_width = 1920, .output_height = 1080,
                                 .flags = PS5FSR4_FLAG_HIGH_DYNAMIC_RANGE};
    ps5fsr4_memory_requirements requirements;
    ps5fsr4_context *context = NULL;
    ps5fsr4_dispatch_desc dispatch = {.struct_size = sizeof(dispatch)};
    float x, y;
    if (ps5fsr4_get_memory_requirements(&desc, &requirements) != PS5FSR4_OK ||
        ps5fsr4_context_create(&desc, &context) != PS5FSR4_OK)
        return 1;
    ps5fsr4_jitter_offset(0, ps5fsr4_jitter_phase_count(desc.max_render_width, desc.output_width), &x, &y);
    dispatch.jitter_x = x;
    dispatch.jitter_y = y;
    ps5fsr4_result result = ps5fsr4_dispatch(context, &dispatch);
    if (result == PS5FSR4_OK)
        result = ps5fsr4_dispatch_passes(context, &dispatch, 0, ps5fsr4_pass_count());
    ps5fsr4_context_destroy(context);
    return result != PS5FSR4_OK;
}
