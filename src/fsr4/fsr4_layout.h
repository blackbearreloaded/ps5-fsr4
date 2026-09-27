/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Resolution-dependent FSR4 geometry and constant encoding.
 * Formulas are established against captured reference constants; unsupported
 * geometries are rejected rather than guessed.
 */
#ifndef PS5FSR4_FSR4_LAYOUT_H
#define PS5FSR4_FSR4_LAYOUT_H

#include <stdint.h>
#include <string.h>

#include <ps5fsr4/ps5_fsr4.h>
#include "fsr4_tables.h"

#define FSR4_TENSORS 17u

struct fsr4_layout {
    uint32_t max_render_width, max_render_height, output_width, output_height;
    uint32_t luma_width, luma_height;
    uint32_t tensor[FSR4_TENSORS][2];
    uint64_t scratch_bytes;
};

/* Pinned reference geometry: 128x96 -> 192x144 (tools/fsr4_reference_probe.py). */
static const uint32_t fsr4_reference_groups[FSR4_PASS_COUNT][3] = {
    {2,2,1},{12,9,1},{20,1,1},{2,72,1},{20,1,1},{2,72,1},{20,1,1},{1,36,1},{11,1,1},{1,36,1},
    {11,1,1},{1,36,1},{11,1,1},{1,18,1},{6,1,1},{1,18,1},{6,1,1},{1,18,1},{6,1,1},{1,18,1},
    {11,1,1},{1,36,1},{11,1,1},{1,36,1},{20,1,1},{2,72,1},{20,1,1},{6,5,1}};

/* Network tensor resolutions relative to the output: level k is output / 2^k. */
static const uint8_t fsr4_tensor_level[FSR4_TENSORS] = {1,0,1,1,2,2,2,3,3,3,2,2,1,1,0,0,0};

static inline int fsr4_layout_init(struct fsr4_layout *l, uint32_t max_render_width, uint32_t max_render_height,
                                   uint32_t output_width, uint32_t output_height)
{
    memset(l, 0, sizeof(*l));
    l->max_render_width = max_render_width;
    l->max_render_height = max_render_height;
    l->output_width = output_width;
    l->output_height = output_height;
    if (!(max_render_width == 128 && max_render_height == 96 && output_width == 192 && output_height == 144))
        return 1;
    for (uint32_t i = 0; i < FSR4_TENSORS; ++i) {
        l->tensor[i][0] = output_width >> fsr4_tensor_level[i];
        l->tensor[i][1] = output_height >> fsr4_tensor_level[i];
    }
    l->luma_width = max_render_width >> 4;
    l->luma_height = max_render_height >> 4;
    l->scratch_bytes = 20880256u;
    return 0;
}

static inline int fsr4_layout_supports_render(const struct fsr4_layout *l, uint32_t width, uint32_t height)
{
    return !(width == l->max_render_width && height == l->max_render_height);
}

static inline void fsr4_pass_groups(const struct fsr4_layout *l, uint32_t pass, uint32_t render_width,
                                    uint32_t render_height, uint32_t groups[3])
{
    (void)l; (void)render_width; (void)render_height;
    memcpy(groups, fsr4_reference_groups[pass], sizeof(uint32_t) * 3);
}

static inline uint32_t fsr4_float_bits(float value)
{
    uint32_t bits;
    memcpy(&bits, &value, sizeof(bits));
    return bits;
}

static inline uint32_t fsr4_log2_floor(uint32_t value)
{
    uint32_t n = 0;
    while (value >>= 1) ++n;
    return n;
}

/* AutoExposureSPDConstants: mips, workgroups, workgroup offset, 1/render size, 1.0. */
static inline void fsr4_spd_constants(const struct fsr4_layout *l, uint32_t render_width, uint32_t render_height,
                                      uint32_t out[FSR4_CONSTANT_BLOCK_BYTES / 4])
{
    (void)l;
    memset(out, 0, FSR4_CONSTANT_BLOCK_BYTES);
    const uint32_t gx = (render_width + 63) / 64, gy = (render_height + 63) / 64;
    out[0] = fsr4_log2_floor(render_width > render_height ? render_width : render_height);
    out[1] = gx * gy;
    out[4] = fsr4_float_bits(1.0f / (float)render_width);
    out[5] = fsr4_float_bits(1.0f / (float)render_height);
    out[6] = fsr4_float_bits(1.0f);
}

/* MLSR_Optimized_Constants (26 dwords used). */
static inline void fsr4_mlsr_constants(const struct fsr4_layout *l, const ps5fsr4_dispatch_desc *d, int reset,
                                       float previous_pre_exposure, uint32_t out[FSR4_CONSTANT_BLOCK_BYTES / 4])
{
    memset(out, 0, FSR4_CONSTANT_BLOCK_BYTES);
    const float ow = (float)l->output_width, oh = (float)l->output_height;
    const float rw = (float)d->render_width, rh = (float)d->render_height;
    out[0] = fsr4_float_bits(1.0f / ow);
    out[1] = fsr4_float_bits(1.0f / oh);
    out[2] = fsr4_float_bits(ow / rw);
    out[3] = fsr4_float_bits(oh / rh);
    out[4] = fsr4_float_bits(rw / ow);
    out[5] = fsr4_float_bits(rh / oh);
    out[6] = fsr4_float_bits(d->jitter_x);
    out[7] = fsr4_float_bits(d->jitter_y);
    out[8] = fsr4_float_bits(d->motion_vector_scale_x / rw);
    out[9] = fsr4_float_bits(d->motion_vector_scale_y / rh);
    out[10] = fsr4_float_bits(ow);
    out[11] = fsr4_float_bits(oh);
    out[12] = fsr4_float_bits(rw);
    out[13] = fsr4_float_bits(rh);
    out[16] = l->output_width;
    out[17] = l->output_height;
    out[18] = reset ? 1u : 0u;
    out[19] = d->render_width;
    out[20] = d->render_height;
    out[21] = fsr4_float_bits(d->pre_exposure);
    out[22] = fsr4_float_bits(previous_pre_exposure);
}

/* CsTensorSizes: 17 int4 tensor extents shared by every network pass. */
static inline void fsr4_tensor_constants(const struct fsr4_layout *l, uint32_t out[FSR4_CONSTANT_BLOCK_BYTES / 4])
{
    memset(out, 0, FSR4_CONSTANT_BLOCK_BYTES);
    for (uint32_t i = 0; i < FSR4_TENSORS; ++i) {
        out[4 * i] = l->tensor[i][0];
        out[4 * i + 1] = l->tensor[i][1];
    }
}

#endif
