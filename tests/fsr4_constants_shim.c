/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Host shim exposing the FSR4 constant encoders to tests/test_fsr4_runtime_constants.py.
 */
#include "../src/fsr4/fsr4_layout.h"

int fsr4_test_encode(uint32_t render_w, uint32_t render_h, uint32_t output_w, uint32_t output_h,
                     float jitter_x, float jitter_y, float mv_x, float mv_y, float pre_exposure,
                     int reset, float previous_pre_exposure,
                     uint32_t *spd, uint32_t *mlsr, uint32_t *tensor)
{
    struct fsr4_layout layout;
    if (fsr4_layout_init(&layout, render_w, render_h, output_w, output_h)) return 1;
    ps5fsr4_dispatch_desc d;
    memset(&d, 0, sizeof(d));
    d.render_width = render_w;
    d.render_height = render_h;
    d.jitter_x = jitter_x;
    d.jitter_y = jitter_y;
    d.motion_vector_scale_x = mv_x;
    d.motion_vector_scale_y = mv_y;
    d.pre_exposure = pre_exposure;
    fsr4_spd_constants(&layout, render_w, render_h, spd);
    fsr4_mlsr_constants(&layout, &d, reset, previous_pre_exposure, mlsr);
    fsr4_tensor_constants(&layout, tensor);
    return 0;
}
