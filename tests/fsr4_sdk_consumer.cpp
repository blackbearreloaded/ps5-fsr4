// Copyright (C) 2026 BlackBearReloaded
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Independent C++ consumer of the staged ps5_fsr4 SDK: the public header must
// compile as C++ and link with C linkage. tools/build_fsr4_sdk.py links it
// against a relocated copy of the SDK; it is not executed.
#include <ps5fsr4/ps5_fsr4.h>

static_assert(PS5FSR4_VERSION_MAJOR == 0, "ps5_fsr4 major version");

int main()
{
    ps5fsr4_context_desc desc{};
    desc.struct_size = sizeof(desc);
    desc.max_render_width = 1280;
    desc.max_render_height = 720;
    desc.output_width = 1920;
    desc.output_height = 1080;
    ps5fsr4_memory_requirements requirements{};
    ps5fsr4_context *context = nullptr;
    if (ps5fsr4_get_memory_requirements(&desc, &requirements) != PS5FSR4_OK ||
        ps5fsr4_context_create(&desc, &context) != PS5FSR4_OK)
        return 1;
    ps5fsr4_dispatch_desc dispatch{};
    dispatch.struct_size = sizeof(dispatch);
    ps5fsr4_jitter_offset(0, ps5fsr4_jitter_phase_count(1280, 1920), &dispatch.jitter_x, &dispatch.jitter_y);
    ps5fsr4_result result = ps5fsr4_dispatch(context, &dispatch);
    if (result == PS5FSR4_OK)
        result = ps5fsr4_dispatch_passes(context, &dispatch, 0, ps5fsr4_pass_count());
    ps5fsr4_context_destroy(context);
    return result != PS5FSR4_OK;
}
