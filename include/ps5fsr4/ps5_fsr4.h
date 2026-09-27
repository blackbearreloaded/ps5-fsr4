/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ps5_fsr4: native FSR 4.1.1 INT8 upscaling on Vulkan (ps5vk on PS5, or any
 * conformant Vulkan 1.3 driver for host validation). Experimental.
 *
 * The application owns the device, queue, command buffers, inputs, output and
 * presentation. The context owns pipelines, model data, constants,
 * intermediates and history. ps5fsr4_dispatch() records commands into the
 * application's command buffer; the application submits and waits. A context
 * may be dispatched again only after its previous dispatch completed.
 *
 * Conventions (matching the FidelityFX upscaler API):
 * - color: linear scene color at render resolution, sampled image;
 * - depth: device depth at render resolution, sampled R32 float image;
 * - motion vectors: RG float, current-to-previous, in the units selected by
 *   motion_vector_scale (the scale converts stored values to render pixels);
 * - output: RGBA32F storage image at output resolution in VK_IMAGE_LAYOUT_GENERAL;
 * - jitter: sub-pixel offset in render pixels, see ps5fsr4_jitter_offset().
 * Inputs must be in the declared layouts and visible to compute shader reads.
 */
#ifndef PS5FSR4_PS5_FSR4_H
#define PS5FSR4_PS5_FSR4_H

#include <stdint.h>
#include <vulkan/vulkan_core.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PS5FSR4_VERSION_MAJOR 0
#define PS5FSR4_VERSION_MINOR 1
#define PS5FSR4_VERSION_PATCH 0

typedef enum ps5fsr4_result {
    PS5FSR4_OK = 0,
    PS5FSR4_ERROR_INVALID_ARGUMENT = -1,
    PS5FSR4_ERROR_UNSUPPORTED = -2,
    PS5FSR4_ERROR_OUT_OF_MEMORY = -3,
    PS5FSR4_ERROR_VULKAN = -4
} ps5fsr4_result;

typedef enum ps5fsr4_flags {
    /* Color is high dynamic range (not tonemapped to [0,1]). */
    PS5FSR4_FLAG_HIGH_DYNAMIC_RANGE = 1u << 0,
    /* Compute exposure from the color input instead of using pre_exposure only. */
    PS5FSR4_FLAG_AUTO_EXPOSURE = 1u << 1
} ps5fsr4_flags;

typedef struct ps5fsr4_context ps5fsr4_context;

typedef struct ps5fsr4_context_desc {
    uint32_t struct_size;               /* sizeof(ps5fsr4_context_desc) */
    VkPhysicalDevice physical_device;
    VkDevice device;
    uint32_t max_render_width, max_render_height;
    uint32_t output_width, output_height;
    uint32_t flags;                     /* ps5fsr4_flags */
    const VkAllocationCallbacks *allocator;
} ps5fsr4_context_desc;

typedef struct ps5fsr4_dispatch_desc {
    uint32_t struct_size;               /* sizeof(ps5fsr4_dispatch_desc) */
    VkCommandBuffer command_buffer;
    VkImageView color, depth, motion_vectors, output;
    VkImageLayout color_layout, depth_layout, motion_vectors_layout;
    uint32_t render_width, render_height;
    float jitter_x, jitter_y;
    float motion_vector_scale_x, motion_vector_scale_y;
    float pre_exposure;                 /* > 0; 1 when color is not pre-exposed */
    float frame_time_delta_ms;
    float camera_near, camera_far, camera_fov_vertical;
    uint32_t reset;                     /* nonzero discards history (first frame, camera cut) */
} ps5fsr4_dispatch_desc;

typedef struct ps5fsr4_memory_requirements {
    VkDeviceSize device_bytes;          /* intermediates, history and model data */
    VkDeviceSize host_visible_bytes;    /* per-dispatch constants */
} ps5fsr4_memory_requirements;

ps5fsr4_result ps5fsr4_context_create(const ps5fsr4_context_desc *desc, ps5fsr4_context **context);
void ps5fsr4_context_destroy(ps5fsr4_context *context);
ps5fsr4_result ps5fsr4_get_memory_requirements(const ps5fsr4_context_desc *desc,
                                               ps5fsr4_memory_requirements *requirements);
ps5fsr4_result ps5fsr4_dispatch(ps5fsr4_context *context, const ps5fsr4_dispatch_desc *desc);

/* Halton(2,3) jitter sequence used by FidelityFX upscalers. */
uint32_t ps5fsr4_jitter_phase_count(uint32_t render_width, uint32_t output_width);
void ps5fsr4_jitter_offset(uint32_t index, uint32_t phase_count, float *x, float *y);

#ifdef __cplusplus
}
#endif

#endif
