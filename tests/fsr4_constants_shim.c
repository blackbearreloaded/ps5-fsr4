/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Host shim exposing the FSR4 constant encoders to tests/test_fsr4_runtime_constants.py.
 */
#include "../src/fsr4_layout.h"

int fsr4_test_groups(uint32_t max_render_w, uint32_t max_render_h, uint32_t output_w, uint32_t output_h,
                     uint32_t render_w, uint32_t render_h, uint32_t rule, uint32_t tensor,
                     uint32_t limit_w, uint32_t limit_h, uint32_t *groups, uint32_t *luma)
{
    struct fsr4_layout layout;
    if (fsr4_layout_init(&layout, max_render_w, max_render_h, output_w, output_h)) return 1;
    fsr4_pass_groups(&layout, (uint8_t)rule, (uint8_t)tensor, (uint16_t)limit_w, (uint16_t)limit_h,
                     render_w, render_h, groups);
    luma[0] = layout.luma_width;
    luma[1] = layout.luma_height;
    return 0;
}

uint32_t fsr4_test_mode(uint32_t output_w, uint32_t render_w)
{
    return fsr4_mode(output_w, render_w);
}

int fsr4_test_band(uint32_t output_w, uint32_t output_h)
{
    struct fsr4_layout layout;
    return fsr4_layout_init(&layout, 16, 16, output_w, output_h) ? -1 : (int)layout.band;
}

int fsr4_test_encode(uint32_t max_render_w, uint32_t max_render_h,
                     uint32_t render_w, uint32_t render_h, uint32_t output_w, uint32_t output_h,
                     float jitter_x, float jitter_y, float mv_x, float mv_y, float pre_exposure,
                     int reset, float previous_pre_exposure, int sharpen, float sharpness,
                     uint32_t *spd, uint32_t *mlsr, uint32_t *tensor, uint32_t *rcas)
{
    struct fsr4_layout layout;
    if (fsr4_layout_init(&layout, max_render_w, max_render_h, output_w, output_h)) return 1;
    ps5fsr4_dispatch_desc d;
    memset(&d, 0, sizeof(d));
    d.render_width = render_w;
    d.render_height = render_h;
    d.jitter_x = jitter_x;
    d.jitter_y = jitter_y;
    d.motion_vector_scale_x = mv_x;
    d.motion_vector_scale_y = mv_y;
    d.pre_exposure = pre_exposure;
    d.enable_sharpening = (uint32_t)sharpen;
    d.sharpness = sharpness;
    fsr4_spd_constants(&layout, render_w, render_h, spd);
    fsr4_rcas_constants(sharpness, pre_exposure, rcas);
    fsr4_mlsr_constants(&layout, &d, reset, previous_pre_exposure, mlsr);
    fsr4_tensor_constants(&layout, tensor);
    return 0;
}
