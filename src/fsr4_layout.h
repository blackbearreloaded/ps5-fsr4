/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Resolution-dependent FSR4 geometry and constant encoding.
 * Rules are established against captured reference constants and dispatches
 * (tests/test_fsr4_runtime_constants.py); unsupported geometry is rejected.
 */
#ifndef PS5FSR4_FSR4_LAYOUT_H
#define PS5FSR4_FSR4_LAYOUT_H

#include <math.h>
#include <stdint.h>
#include <string.h>

#include <ps5fsr4/ps5_fsr4.h>
#include "fsr4_tables.h"

#define FSR4_TENSORS 17u
/* The model-v07 shaders come in resolution bands with fixed scratch layouts:
 * band 0 for outputs up to 1920x1080, band 1 up to 3840x2160. */
#define FSR4_BAND0_MAX_WIDTH 1920u
#define FSR4_BAND0_MAX_HEIGHT 1080u
#define FSR4_MAX_OUTPUT_WIDTH 3840u
#define FSR4_MAX_OUTPUT_HEIGHT 2160u

struct fsr4_layout {
    uint32_t max_render_width, max_render_height, output_width, output_height;
    uint32_t luma_width, luma_height;
    uint32_t tensor[FSR4_TENSORS][2];
    uint32_t band;
};

/* CsTensorSizes rows as pyramid levels: the output rounded up to a multiple of
 * eight, halved once per level. */
static const uint8_t fsr4_tensor_level[FSR4_TENSORS] = {1,0,1,1,2,2,2,3,3,3,2,2,1,1,0,0,0};

static inline uint32_t fsr4_div_up(uint32_t value, uint32_t divisor)
{
    return (value + divisor - 1) / divisor;
}

static inline void fsr4_tensor_extent(const struct fsr4_layout *l, uint32_t level, uint32_t extent[2])
{
    extent[0] = ((l->output_width + 7u) & ~7u) >> level;
    extent[1] = ((l->output_height + 7u) & ~7u) >> level;
}

static inline int fsr4_layout_init(struct fsr4_layout *l, uint32_t max_render_width, uint32_t max_render_height,
                                   uint32_t output_width, uint32_t output_height)
{
    memset(l, 0, sizeof(*l));
    if (!max_render_width || !max_render_height || max_render_width > output_width ||
        max_render_height > output_height || output_width > FSR4_MAX_OUTPUT_WIDTH ||
        output_height > FSR4_MAX_OUTPUT_HEIGHT || output_width < 16 || output_height < 16)
        return 1;
    l->max_render_width = max_render_width;
    l->max_render_height = max_render_height;
    l->output_width = output_width;
    l->output_height = output_height;
    for (uint32_t i = 0; i < FSR4_TENSORS; ++i)
        fsr4_tensor_extent(l, fsr4_tensor_level[i], l->tensor[i]);
    /* Auto-exposure luma mip 5 of the half-resolution luminance. */
    l->luma_width = fsr4_div_up(max_render_width, 16);
    l->luma_height = fsr4_div_up(max_render_height, 16);
    l->band = output_width > FSR4_BAND0_MAX_WIDTH || output_height > FSR4_BAND0_MAX_HEIGHT;
    return 0;
}

/* FidelityFX quality mode of an output-to-render width ratio: 0 native AA,
 * 1 quality, 2 balanced, 3 performance, 5 ultra performance (4 is dynamic
 * resolution, which the context flag selects). */
static inline uint32_t fsr4_mode(uint32_t output_width, uint32_t render_width)
{
    const float ratio = (float)output_width / (float)render_width;
    return ratio >= 2.99f ? 5u : ratio >= 1.99f ? 3u : ratio >= 1.6900001f ? 2u : ratio >= 1.5f ? 1u : 0u;
}

static inline int fsr4_layout_supports_render(const struct fsr4_layout *l, uint32_t width, uint32_t height)
{
    return !(width && height && width <= l->max_render_width && height <= l->max_render_height);
}

static inline uint32_t fsr4_padding_elements(uint32_t width, uint32_t height,
                                             uint32_t limit_width, uint32_t limit_height)
{
    const uint32_t right = limit_width - width < 5u ? limit_width - width : 5u;
    const uint32_t bottom = limit_height - height < 1u ? limit_height - height : 1u;
    const uint32_t top = width + 1u + right;
    return top + height + height * right + top * bottom;
}

static inline void fsr4_pass_groups(const struct fsr4_layout *l, uint8_t rule, uint8_t tensor,
                                    uint16_t limit_width, uint16_t limit_height,
                                    uint32_t render_width, uint32_t render_height, uint32_t groups[3])
{
    uint32_t extent[2];
    groups[1] = groups[2] = 1;
    switch (rule) {
    case FSR4_GROUPS_NONE:
        groups[0] = groups[1] = groups[2] = 0;
        break;
    case FSR4_GROUPS_SPD:
        groups[0] = fsr4_div_up(render_width, 64);
        groups[1] = fsr4_div_up(render_height, 64);
        break;
    case FSR4_GROUPS_PREPASS:
        groups[0] = fsr4_div_up(l->output_width, 16);
        groups[1] = fsr4_div_up(l->output_height, 16);
        break;
    case FSR4_GROUPS_POSTPASS:
        groups[0] = fsr4_div_up(l->output_width, 32);
        groups[1] = fsr4_div_up(l->output_height, 32);
        break;
    case FSR4_GROUPS_NETWORK:
        fsr4_tensor_extent(l, tensor, extent);
        groups[0] = fsr4_div_up(extent[0], 64);
        groups[1] = extent[1];
        if (limit_width) groups[2] = limit_width;  /* output-channel banks of a generated kernel */
        break;
    case FSR4_GROUPS_RCAS:
        groups[0] = fsr4_div_up(l->output_width, 16);
        groups[1] = fsr4_div_up(l->output_height, 16);
        break;
    default:
        extent[0] = l->tensor[tensor][0];
        extent[1] = l->tensor[tensor][1];
        groups[0] = fsr4_div_up(fsr4_padding_elements(extent[0], extent[1], limit_width, limit_height), 32);
        break;
    }
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
    out[0] = fsr4_log2_floor(render_width > render_height ? render_width : render_height);
    out[1] = fsr4_div_up(render_width, 64) * fsr4_div_up(render_height, 64);
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
    out[12] = fsr4_float_bits((float)l->max_render_width);   /* the context maximum */
    out[13] = fsr4_float_bits((float)l->max_render_height);
    out[16] = l->output_width;
    out[17] = l->output_height;
    out[18] = reset ? 1u : 0u;
    out[19] = d->render_width;
    out[20] = d->render_height;
    out[21] = fsr4_float_bits(d->pre_exposure);
    out[22] = fsr4_float_bits(previous_pre_exposure);
    if (d->enable_sharpening) {
        out[23] = 1u;
        out[24] = fsr4_float_bits(d->sharpness);
    }
}

/* Half-precision bits of a normal float in [2^-14, 65504], truncated toward
 * zero as the provider's host-side packing does. */
static inline uint32_t fsr4_half_bits(float value)
{
    const uint32_t bits = fsr4_float_bits(value);
    const uint32_t exponent = ((bits >> 23) & 0xffu) - 127u + 15u;
    return ((bits >> 16) & 0x8000u) | (exponent << 10) | ((bits >> 13) & 0x3ffu);
}

/* cbRCAS: sharpness in [0, 1] maps to exp2(2 * sharpness - 2), stored as a
 * float and as a pair of halves, followed by the pre-exposure. */
static inline void fsr4_rcas_constants(float sharpness, float pre_exposure,
                                       uint32_t out[FSR4_CONSTANT_BLOCK_BYTES / 4])
{
    memset(out, 0, FSR4_CONSTANT_BLOCK_BYTES);
    const float x = exp2f(2.0f * sharpness - 2.0f);  /* single precision, as the provider computes it */
    out[0] = fsr4_float_bits(x);
    out[1] = fsr4_half_bits(x) * 0x10001u;
    out[4] = fsr4_float_bits(pre_exposure);
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
