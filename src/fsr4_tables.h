/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Declarations shared by the FSR4 runtime and its generated pass tables.
 */
#ifndef PS5FSR4_FSR4_TABLES_H
#define PS5FSR4_FSR4_TABLES_H

#include <stddef.h>
#include <stdint.h>

#define FSR4_PASS_COUNT 28u
#define FSR4_DESCRIPTOR_SETS 4u   /* dxil-spirv: 0 SRV, 1 UAV, 2 CBV, 3 sampler */
#define FSR4_CONSTANT_BLOCK_BYTES 272u

enum fsr4_descriptor {
    FSR4_DESCRIPTOR_SAMPLED_IMAGE,
    FSR4_DESCRIPTOR_STORAGE_IMAGE,
    FSR4_DESCRIPTOR_STORAGE_BUFFER,
    FSR4_DESCRIPTOR_UNIFORM_BUFFER,
    FSR4_DESCRIPTOR_SAMPLER
};

enum fsr4_role {
    FSR4_ROLE_COLOR,
    FSR4_ROLE_DEPTH,
    FSR4_ROLE_MOTION_VECTORS,
    FSR4_ROLE_OUTPUT,
    FSR4_ROLE_SPD_COUNTER,
    FSR4_ROLE_LUMA_MIP5,
    FSR4_ROLE_AUTO_EXPOSURE,
    FSR4_ROLE_WEIGHTS,
    FSR4_ROLE_HISTORY,
    FSR4_ROLE_RECURRENT,
    FSR4_ROLE_SCRATCH,
    FSR4_ROLE_HISTORY_REPROJECTED,
    FSR4_ROLE_CONSTANTS,
    FSR4_ROLE_SAMPLER,
    FSR4_ROLE_RCAS_INPUT,     /* reconstruction written for sharpening */
    FSR4_ROLE_COUNT
};

enum fsr4_constants {
    FSR4_CONSTANTS_NONE,
    FSR4_CONSTANTS_SPD,      /* auto-exposure single-pass downsampler */
    FSR4_CONSTANTS_MLSR,     /* MLSR_Optimized_Constants: preparation and reconstruction */
    FSR4_CONSTANTS_TENSOR,   /* CsTensorSizes: network layers */
    FSR4_CONSTANTS_RCAS      /* cbRCAS: sharpening */
};

/* How a pass derives its dispatch size (verified against captured references). */
enum fsr4_groups {
    FSR4_GROUPS_SPD,          /* ceil(render / 64) */
    FSR4_GROUPS_PREPASS,      /* ceil(output / 16) */
    FSR4_GROUPS_POSTPASS,     /* ceil(output / 32) */
    FSR4_GROUPS_NETWORK,      /* (ceil(tensor width / 64), tensor height) at a pyramid level */
    FSR4_GROUPS_PADDING,      /* border elements of one tensor, 32 per group */
    FSR4_GROUPS_RCAS          /* ceil(output / 16) */
};

/* Generated tables define FSR4_HAS_RCAS and fsr4_rcas_pass when the sharpening
 * pass was captured; it runs after the postpass as pipeline FSR4_PASS_COUNT. */

struct fsr4_binding {
    uint8_t set, binding, descriptor, role;
};

struct fsr4_pass_info {
    const uint32_t *code;
    size_t code_bytes;
    uint8_t constants;
    uint8_t binding_count;
    const struct fsr4_binding *bindings;
    uint8_t groups;           /* enum fsr4_groups */
    uint8_t tensor;           /* NETWORK: pyramid level; PADDING: CsTensorSizes row */
    uint16_t limit_width;     /* PADDING: allocated tensor width + 1 compiled into the shader;
                                 NETWORK: output-channel banks (workgroup z) when nonzero */
    uint16_t limit_height;    /* PADDING: allocated tensor height + 1 */
};

#endif
