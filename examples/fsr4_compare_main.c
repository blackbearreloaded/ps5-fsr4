/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Headless FSR4 comparison capture. For a few fixed shots of the demo scene it
 * writes, tonemapped like the demo and packed as BGRA8:
 *   - the render-resolution frame (no jitter) and a bilinear upscale of it,
 *   - the FSR4 output after the static shot has converged,
 *   - a native output-resolution render (no anti-aliasing),
 *   - a 64-sample (8x8 stratified) supersampled output-resolution reference,
 * at 1280x720 and 960x540 to 1920x1080; then a short orbiting clip (crops of
 * bilinear, FSR4 and native frames) for temporal stability. The host builds the
 * comparison images from these files with tools/build_fsr4_comparisons.py.
 */
#include <ps5vk/ps5vk.h>
#include <ps5fsr4/ps5_fsr4.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "fsr4_compare_shaders.h"

extern int sceKernelDebugOutText(int level, const char *text);
extern int fsr4_native_heap_init(void);

enum { OUTPUT_W = 1920, OUTPUT_H = 1080, MAX_RENDER_W = 1280, MAX_RENDER_H = 720 };
enum { CONVERGE_FRAMES = 48, REFERENCE_GRID = 8 };
enum { CLIP_WARMUP = 16, CLIP_FRAMES = 90 };

struct shot { const char *name; float camera[4]; };  /* yaw, pitch, distance, time */
static const struct shot shots[] = {
    {"overview", {0.6f, 0.35f, 9.0f, 2.0f}},
    {"fence", {-1.9f, 0.18f, 12.0f, 2.0f}},
    {"horizon", {0.3f, 0.12f, 18.0f, 2.0f}},
};
static const uint32_t render_sizes[][2] = {{1280, 720}, {960, 540}};
/* The clip orbits from the overview camera; its crop is the frame's central 640x360. */
static const int32_t clip_rect[4] = {640, 360, 640, 360};

static FILE *log_file;
static void report(const char *format, ...)
{
    char message[1024];
    va_list args;
    va_start(args, format);
    vsnprintf(message, sizeof(message), format, args);
    va_end(args);
    sceKernelDebugOutText(0, message);
    if (log_file) { fputs(message, log_file); fflush(log_file); }
}

#define CHECK(call) do { VkResult rc_ = (call); if (rc_ != VK_SUCCESS) { \
    report("FSR4_COMPARE_ERROR %s = %d\n", #call, (int)rc_); return 1; } } while (0)

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static VkDevice device;
static VkQueue queue;
static VkCommandBuffer cmd;
static VkFence fence;
static VkPhysicalDeviceMemoryProperties memory_properties;

static int memory_type(uint32_t bits, VkMemoryPropertyFlags flags)
{
    for (uint32_t i = 0; i < memory_properties.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (memory_properties.memoryTypes[i].propertyFlags & flags) == flags)
            return (int)i;
    return -1;
}

struct image { VkImage image; VkDeviceMemory memory; VkImageView view; };

static int create_image(struct image *img, VkFormat format, uint32_t w, uint32_t h)
{
    VkImageCreateInfo info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
        .format = format, .extent = {w, h, 1}, .mipLevels = 1, .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
                 VK_IMAGE_USAGE_TRANSFER_DST_BIT,
        .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED};
    CHECK(vkCreateImage(device, &info, NULL, &img->image));
    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(device, img->image, &req);
    VkMemoryAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
                               .memoryTypeIndex = (uint32_t)memory_type(req.memoryTypeBits, 0)};
    CHECK(vkAllocateMemory(device, &ai, NULL, &img->memory));
    CHECK(vkBindImageMemory(device, img->image, img->memory, 0));
    VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = img->image,
        .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = format, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    CHECK(vkCreateImageView(device, &vi, NULL, &img->view));
    return 0;
}

struct compute {
    VkDescriptorSetLayout set_layout;
    VkPipelineLayout layout;
    VkPipeline pipeline;
    VkDescriptorSet sets[2];
};

/* A compute pipeline with `set_count` descriptor sets of the same layout. */
static int create_compute(struct compute *c, const uint32_t *code, size_t bytes, const VkDescriptorType *types,
                          uint32_t count, uint32_t push_bytes, uint32_t set_count, VkDescriptorPool pool,
                          VkPipelineCache cache)
{
    VkDescriptorSetLayoutBinding bindings[5];
    for (uint32_t i = 0; i < count; ++i)
        bindings[i] = (VkDescriptorSetLayoutBinding){i, types[i], 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL};
    VkDescriptorSetLayoutCreateInfo sl = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                          .bindingCount = count, .pBindings = bindings};
    CHECK(vkCreateDescriptorSetLayout(device, &sl, NULL, &c->set_layout));
    VkPushConstantRange range = {VK_SHADER_STAGE_COMPUTE_BIT, 0, push_bytes};
    VkPipelineLayoutCreateInfo pl = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
                                     .pSetLayouts = &c->set_layout, .pushConstantRangeCount = 1,
                                     .pPushConstantRanges = &range};
    CHECK(vkCreatePipelineLayout(device, &pl, NULL, &c->layout));
    VkShaderModuleCreateInfo mi = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = bytes, .pCode = code};
    VkShaderModule module;
    CHECK(vkCreateShaderModule(device, &mi, NULL, &module));
    VkComputePipelineCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                  .stage = VK_SHADER_STAGE_COMPUTE_BIT, .module = module, .pName = "main"},
        .layout = c->layout};
    CHECK(vkCreateComputePipelines(device, cache, 1, &ci, NULL, &c->pipeline));
    vkDestroyShaderModule(device, module, NULL);
    const VkDescriptorSetLayout layouts[2] = {c->set_layout, c->set_layout};
    VkDescriptorSetAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                      .descriptorPool = pool, .descriptorSetCount = set_count, .pSetLayouts = layouts};
    CHECK(vkAllocateDescriptorSets(device, &ai, c->sets));
    return 0;
}

static void bind_image(VkDescriptorSet set, uint32_t binding, VkImageView view)
{
    VkDescriptorImageInfo info = {VK_NULL_HANDLE, view, VK_IMAGE_LAYOUT_GENERAL};
    VkWriteDescriptorSet w = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = binding,
        .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, .pImageInfo = &info};
    vkUpdateDescriptorSets(device, 1, &w, 0, NULL);
}

static void memory_barrier(VkAccessFlags src, VkAccessFlags dst, VkPipelineStageFlags src_stage,
                           VkPipelineStageFlags dst_stage)
{
    VkMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = src, .dstAccessMask = dst};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 1, &b, 0, NULL, 0, NULL);
}

static void compute_barrier(void)
{
    memory_barrier(VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT,
                   VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
}

static int begin(void)
{
    VkCommandBufferBeginInfo bi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    CHECK(vkBeginCommandBuffer(cmd, &bi));
    return 0;
}

static int submit(void)
{
    CHECK(vkEndCommandBuffer(cmd));
    VkSubmitInfo s = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &cmd};
    CHECK(vkQueueSubmit(queue, 1, &s, fence));
    VkResult wait = vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_C(10000000000));
    if (wait != VK_SUCCESS) {
        report("FSR4_COMPARE_ERROR fence=%d\n", (int)wait);
        for (;;) usleep(100000);  /* never release resources with unknown completion */
    }
    CHECK(vkResetFences(device, 1, &fence));
    CHECK(vkResetCommandBuffer(cmd, 0));
    return 0;
}

static const char *output_root(void)
{
    static const char *chosen;
    static const char *const candidates[] = {"/data/fsr4-results", "/app0/results", "/download0"};
    for (unsigned i = 0; !chosen && i < sizeof(candidates) / sizeof(candidates[0]); ++i) {
        char probe[128];
        mkdir(candidates[i], 0777);
        snprintf(probe, sizeof(probe), "%s/.writable", candidates[i]);
        FILE *f = fopen(probe, "wb");
        if (f && fputc('1', f) != EOF && !fclose(f)) chosen = candidates[i];
        else if (f) fclose(f);
    }
    return chosen ? chosen : "/download0";
}

static void *read_asset(const char *name, size_t *bytes)
{
    char path[256];
    snprintf(path, sizeof(path), "/app0/assets/%s", name);
    FILE *f = fopen(path, "rb");
    void *data = NULL;
    long size = 0;
    if (f && !fseek(f, 0, SEEK_END) && (size = ftell(f)) > 0 && !fseek(f, 0, SEEK_SET) &&
        (data = malloc((size_t)size)) && fread(data, 1, (size_t)size, f) != (size_t)size) {
        free(data);
        data = NULL;
    }
    if (f) fclose(f);
    *bytes = data ? (size_t)size : 0;
    return data;
}

static struct compute scene, accumulate, exporter;
static struct image lo_color, lo_depth, lo_motion, hi_color, hi_depth, hi_motion, upscaled, accumulator;
static VkDeviceMemory staging_memory;
static void *staging_mapped;
static ps5fsr4_context *context;
enum { SCENE_LOW, SCENE_HIGH };

/* One render of the scene into the render-resolution or the output-resolution images. */
static void record_scene(int target, const float camera[4], const float previous[4], float jx, float jy,
                         uint32_t w, uint32_t h)
{
    struct { float camera[4], previous[4], frame[4]; } params = {
        {camera[0], camera[1], camera[2], camera[3]}, {previous[0], previous[1], previous[2], previous[3]},
        {jx, jy, (float)w, (float)h}};
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, scene.pipeline);
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, scene.layout, 0, 1, &scene.sets[target], 0, NULL);
    vkCmdPushConstants(cmd, scene.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(params), &params);
    vkCmdDispatch(cmd, (w + 7) / 8, (h + 7) / 8, 1);
}

/* Tonemaps a rectangle of one capture (see fsr4_compare_export.comp) and writes it to `name`. */
static int export_rect(int mode, const int32_t rect[4], uint32_t rw, uint32_t rh, const char *name)
{
    struct { int32_t region[4]; float view[4]; } params = {{rect[0], rect[1], rect[2], rect[3]},
                                                           {(float)mode, (float)rw, (float)rh, 0.0f}};
    if (begin()) return 1;
    compute_barrier();
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, exporter.pipeline);
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, exporter.layout, 0, 1, &exporter.sets[0], 0, NULL);
    vkCmdPushConstants(cmd, exporter.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(params), &params);
    vkCmdDispatch(cmd, ((uint32_t)rect[2] + 7) / 8, ((uint32_t)rect[3] + 7) / 8, 1);
    memory_barrier(VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_HOST_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                   VK_PIPELINE_STAGE_HOST_BIT);
    if (submit()) return 1;
    VkMappedMemoryRange range = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, .memory = staging_memory,
                                 .size = VK_WHOLE_SIZE};
    CHECK(vkInvalidateMappedMemoryRanges(device, 1, &range));
    char path[256];
    snprintf(path, sizeof(path), "%s/fsr4-compare-%s.bgra", output_root(), name);
    FILE *f = fopen(path, "wb");
    const size_t bytes = (size_t)rect[2] * (size_t)rect[3] * 4;
    if (!f || fwrite(staging_mapped, 1, bytes, f) != bytes) {
        if (f) fclose(f);
        report("FSR4_COMPARE_ERROR write=%s\n", name);
        return 1;
    }
    fclose(f);
    report("FSR4_COMPARE_IMAGE name=%s width=%d height=%d\n", name, rect[2], rect[3]);
    return 0;
}

/* One FSR4 frame: a jittered render-resolution render and its upscale. */
static int fsr4_frame(const float camera[4], const float previous[4], uint32_t frame, uint32_t w, uint32_t h,
                      int reset)
{
    float jx, jy;
    ps5fsr4_jitter_offset(frame, ps5fsr4_jitter_phase_count(w, OUTPUT_W), &jx, &jy);
    if (begin()) return 1;
    record_scene(SCENE_LOW, camera, previous, jx, jy, w, h);
    memory_barrier(VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                   VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    ps5fsr4_dispatch_desc dd = {.struct_size = sizeof(dd), .command_buffer = cmd,
        .color = lo_color.view, .depth = lo_depth.view, .motion_vectors = lo_motion.view, .output = upscaled.view,
        .color_layout = VK_IMAGE_LAYOUT_GENERAL, .depth_layout = VK_IMAGE_LAYOUT_GENERAL,
        .motion_vectors_layout = VK_IMAGE_LAYOUT_GENERAL, .render_width = w, .render_height = h,
        .jitter_x = jx, .jitter_y = jy, .motion_vector_scale_x = (float)w, .motion_vector_scale_y = (float)h,
        .pre_exposure = 1.0f, .frame_time_delta_ms = 16.666667f, .camera_near = 0.1f, .camera_far = 200.0f,
        .camera_fov_vertical = 1.0471976f, .reset = reset};
    ps5fsr4_result fr = ps5fsr4_dispatch(context, &dd);
    if (fr) { report("FSR4_COMPARE_ERROR dispatch=%d\n", (int)fr); return 1; }
    return submit();
}

/* A zero-jitter render at render resolution, as a game without upscaling would show it. */
static int plain_frame(const float camera[4], const float previous[4], uint32_t w, uint32_t h)
{
    if (begin()) return 1;
    record_scene(SCENE_LOW, camera, previous, 0.0f, 0.0f, w, h);
    return submit();
}

static int native_frame(const float camera[4], const float previous[4])
{
    if (begin()) return 1;
    record_scene(SCENE_HIGH, camera, previous, 0.0f, 0.0f, OUTPUT_W, OUTPUT_H);
    return submit();
}

/* 8x8 stratified samples per output pixel, averaged in linear HDR: a box-filtered reference. */
static int reference_frame(const float camera[4])
{
    for (int k = 0; k < REFERENCE_GRID * REFERENCE_GRID; ++k) {
        const float jx = ((float)(k % REFERENCE_GRID) + 0.5f) / REFERENCE_GRID - 0.5f;
        const float jy = ((float)(k / REFERENCE_GRID) + 0.5f) / REFERENCE_GRID - 0.5f;
        const float params[4] = {k == 0 ? 1.0f : 0.0f, 0.0f, (float)OUTPUT_W, (float)OUTPUT_H};
        if (begin()) return 1;
        record_scene(SCENE_HIGH, camera, camera, jx, jy, OUTPUT_W, OUTPUT_H);
        compute_barrier();
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, accumulate.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, accumulate.layout, 0, 1, &accumulate.sets[0], 0,
                                NULL);
        vkCmdPushConstants(cmd, accumulate.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(params), params);
        vkCmdDispatch(cmd, OUTPUT_W / 8, (OUTPUT_H + 7) / 8, 1);
        if (submit()) return 1;
    }
    return 0;
}

static int capture_shot(const struct shot *s)
{
    static const int32_t full[4] = {0, 0, OUTPUT_W, OUTPUT_H};
    char name[128];
    double t0 = now_ms();
    if (native_frame(s->camera, s->camera)) return 1;
    snprintf(name, sizeof(name), "%s-native", s->name);
    if (export_rect(2, full, 0, 0, name)) return 1;
    if (reference_frame(s->camera)) return 1;
    snprintf(name, sizeof(name), "%s-reference", s->name);
    if (export_rect(4, full, 0, 0, name)) return 1;
    for (unsigned r = 0; r < sizeof(render_sizes) / sizeof(render_sizes[0]); ++r) {
        const uint32_t w = render_sizes[r][0], h = render_sizes[r][1];
        const int32_t input[4] = {0, 0, (int32_t)w, (int32_t)h};
        if (plain_frame(s->camera, s->camera, w, h)) return 1;
        snprintf(name, sizeof(name), "%s-%ux%u-input", s->name, w, h);
        if (export_rect(3, input, w, h, name)) return 1;
        snprintf(name, sizeof(name), "%s-%ux%u-bilinear", s->name, w, h);
        if (export_rect(1, full, w, h, name)) return 1;
        /* A static camera: FSR4 accumulates the jittered frames into a converged image. */
        for (uint32_t f = 0; f < CONVERGE_FRAMES; ++f)
            if (fsr4_frame(s->camera, s->camera, f, w, h, f == 0)) return 1;
        snprintf(name, sizeof(name), "%s-%ux%u-fsr4", s->name, w, h);
        if (export_rect(0, full, w, h, name)) return 1;
    }
    report("FSR4_COMPARE_SHOT name=%s ms=%.0f\n", s->name, now_ms() - t0);
    return 0;
}

/* An orbit of the overview camera with the rotor spinning, 60 frames per second. */
static int capture_clip(void)
{
    const uint32_t w = render_sizes[0][0], h = render_sizes[0][1];
    float camera[4], previous[4];
    memcpy(camera, shots[0].camera, sizeof(camera));
    char name[128];
    for (uint32_t f = 0; f < CLIP_WARMUP + CLIP_FRAMES; ++f) {
        memcpy(previous, camera, sizeof(camera));
        camera[0] += 0.006f;
        camera[3] += 1.0f / 60.0f;
        if (fsr4_frame(camera, previous, f, w, h, f == 0)) return 1;
        if (f < CLIP_WARMUP) continue;
        const uint32_t n = f - CLIP_WARMUP;
        snprintf(name, sizeof(name), "clip-fsr4-%03u", n);
        if (export_rect(0, clip_rect, w, h, name)) return 1;
        if (plain_frame(camera, previous, w, h)) return 1;
        snprintf(name, sizeof(name), "clip-bilinear-%03u", n);
        if (export_rect(1, clip_rect, w, h, name)) return 1;
        if (native_frame(camera, previous)) return 1;
        snprintf(name, sizeof(name), "clip-native-%03u", n);
        if (export_rect(2, clip_rect, w, h, name)) return 1;
    }
    report("FSR4_COMPARE_CLIP frames=%u render=%ux%u crop=%d,%d,%d,%d\n", CLIP_FRAMES, w, h, clip_rect[0],
           clip_rect[1], clip_rect[2], clip_rect[3]);
    return 0;
}

static int run(void)
{
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_3};
    VkInstanceCreateInfo ii = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app};
    VkInstance instance;
    CHECK(vkCreateInstance(&ii, NULL, &instance));
    uint32_t count = 1;
    VkPhysicalDevice physical;
    CHECK(vkEnumeratePhysicalDevices(instance, &count, &physical));
    float priority = 1;
    VkDeviceQueueCreateInfo qi = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueCount = 1,
                                  .pQueuePriorities = &priority};
    VkPhysicalDeviceFeatures features = {.shaderInt16 = VK_TRUE};
    const char *extension = VK_KHR_STORAGE_BUFFER_STORAGE_CLASS_EXTENSION_NAME;
    VkPhysicalDeviceVulkan13Features offered = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    VkPhysicalDeviceFeatures2 query = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, .pNext = &offered};
    vkGetPhysicalDeviceFeatures2(physical, &query);
    VkPhysicalDeviceVulkan13Features enable13 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES,
                                                 .subgroupSizeControl = offered.subgroupSizeControl};
    VkDeviceCreateInfo di = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .pNext = offered.subgroupSizeControl ? &enable13 : NULL, .queueCreateInfoCount = 1,
        .pQueueCreateInfos = &qi, .pEnabledFeatures = &features, .enabledExtensionCount = 1,
        .ppEnabledExtensionNames = &extension};
    CHECK(vkCreateDevice(physical, &di, NULL, &device));
    vkGetDeviceQueue(device, 0, 0, &queue);
    vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);

    if (create_image(&lo_color, VK_FORMAT_R16G16B16A16_SFLOAT, MAX_RENDER_W, MAX_RENDER_H) ||
        create_image(&lo_depth, VK_FORMAT_R32_SFLOAT, MAX_RENDER_W, MAX_RENDER_H) ||
        create_image(&lo_motion, VK_FORMAT_R32G32B32A32_SFLOAT, MAX_RENDER_W, MAX_RENDER_H) ||
        create_image(&hi_color, VK_FORMAT_R16G16B16A16_SFLOAT, OUTPUT_W, OUTPUT_H) ||
        create_image(&hi_depth, VK_FORMAT_R32_SFLOAT, OUTPUT_W, OUTPUT_H) ||
        create_image(&hi_motion, VK_FORMAT_R32G32B32A32_SFLOAT, OUTPUT_W, OUTPUT_H) ||
        create_image(&upscaled, VK_FORMAT_R32G32B32A32_SFLOAT, OUTPUT_W, OUTPUT_H) ||
        create_image(&accumulator, VK_FORMAT_R32G32B32A32_SFLOAT, OUTPUT_W, OUTPUT_H))
        return 1;

    VkBuffer staging;
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = (VkDeviceSize)OUTPUT_W * OUTPUT_H * 4, .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT};
    CHECK(vkCreateBuffer(device, &bi, NULL, &staging));
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(device, staging, &req);
    VkMemoryAllocateInfo sai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
        .memoryTypeIndex = (uint32_t)memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)};
    CHECK(vkAllocateMemory(device, &sai, NULL, &staging_memory));
    CHECK(vkBindBufferMemory(device, staging, staging_memory, 0));
    CHECK(vkMapMemory(device, staging_memory, 0, VK_WHOLE_SIZE, 0, &staging_mapped));

    size_t cache_bytes = 0;
    void *cache_blob = read_asset("pipeline-cache.bin", &cache_bytes);
    VkPipelineCacheCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO,
                                     .initialDataSize = cache_bytes, .pInitialData = cache_blob};
    VkPipelineCache pipeline_cache;
    CHECK(vkCreatePipelineCache(device, &pci, NULL, &pipeline_cache));
    free(cache_blob);

    VkDescriptorPoolSize sizes[2] = {{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 12}, {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1}};
    VkDescriptorPoolCreateInfo dpi = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 4,
                                      .poolSizeCount = 2, .pPoolSizes = sizes};
    VkDescriptorPool pool;
    CHECK(vkCreateDescriptorPool(device, &dpi, NULL, &pool));
    const VkDescriptorType images3[3] = {VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                                         VK_DESCRIPTOR_TYPE_STORAGE_IMAGE};
    const VkDescriptorType export_types[5] = {VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
        VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE};
    if (create_compute(&scene, fsr4_compare_scene_spv, sizeof(fsr4_compare_scene_spv), images3, 3, 48, 2, pool,
                       pipeline_cache) ||
        create_compute(&accumulate, fsr4_compare_accumulate_spv, sizeof(fsr4_compare_accumulate_spv), images3, 2, 16,
                       1, pool, pipeline_cache) ||
        create_compute(&exporter, fsr4_compare_export_spv, sizeof(fsr4_compare_export_spv), export_types, 5, 32, 1, pool,
                       pipeline_cache))
        return 1;
    bind_image(scene.sets[SCENE_LOW], 0, lo_color.view);
    bind_image(scene.sets[SCENE_LOW], 1, lo_depth.view);
    bind_image(scene.sets[SCENE_LOW], 2, lo_motion.view);
    bind_image(scene.sets[SCENE_HIGH], 0, hi_color.view);
    bind_image(scene.sets[SCENE_HIGH], 1, hi_depth.view);
    bind_image(scene.sets[SCENE_HIGH], 2, hi_motion.view);
    bind_image(accumulate.sets[0], 0, hi_color.view);
    bind_image(accumulate.sets[0], 1, accumulator.view);
    bind_image(exporter.sets[0], 0, upscaled.view);
    bind_image(exporter.sets[0], 1, lo_color.view);
    bind_image(exporter.sets[0], 2, hi_color.view);
    bind_image(exporter.sets[0], 4, accumulator.view);
    VkDescriptorBufferInfo sbi = {staging, 0, VK_WHOLE_SIZE};
    VkWriteDescriptorSet sw = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = exporter.sets[0],
        .dstBinding = 3, .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        .pBufferInfo = &sbi};
    vkUpdateDescriptorSets(device, 1, &sw, 0, NULL);

    ps5fsr4_context_desc cd = {sizeof(cd), physical, device, MAX_RENDER_W, MAX_RENDER_H, OUTPUT_W, OUTPUT_H,
                               PS5FSR4_FLAG_HIGH_DYNAMIC_RANGE | PS5FSR4_FLAG_AUTO_EXPOSURE |
                               (offered.subgroupSizeControl ? PS5FSR4_FLAG_SUBGROUP_SIZE_CONTROL : 0u),
                               NULL, pipeline_cache};
    double t0 = now_ms();
    ps5fsr4_result fr = ps5fsr4_context_create(&cd, &context);
    report("FSR4_COMPARE_CONTEXT result=%d create_ms=%.1f cache_in=%zu\n", (int)fr, now_ms() - t0, cache_bytes);
    if (fr) return 1;

    VkCommandPoolCreateInfo cpi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                   .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    VkCommandPool command_pool;
    CHECK(vkCreateCommandPool(device, &cpi, NULL, &command_pool));
    VkCommandBufferAllocateInfo cai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = command_pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    CHECK(vkAllocateCommandBuffers(device, &cai, &cmd));
    VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    CHECK(vkCreateFence(device, &fi, NULL, &fence));

    if (begin()) return 1;
    const struct image *images[8] = {&lo_color, &lo_depth, &lo_motion, &hi_color, &hi_depth, &hi_motion, &upscaled,
                                     &accumulator};
    for (int i = 0; i < 8; ++i) {
        VkImageMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, .srcAccessMask = 0,
            .dstAccessMask = VK_ACCESS_SHADER_WRITE_BIT, .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED,
            .newLayout = VK_IMAGE_LAYOUT_GENERAL, .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
            .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .image = images[i]->image,
            .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, 0, 0, NULL,
                             0, NULL, 1, &b);
    }
    if (submit()) return 1;
    report("FSR4_COMPARE_READY output=%dx%d shots=%u\n", OUTPUT_W, OUTPUT_H,
           (unsigned)(sizeof(shots) / sizeof(shots[0])));

    for (unsigned s = 0; s < sizeof(shots) / sizeof(shots[0]); ++s)
        if (capture_shot(&shots[s])) return 1;
    return capture_clip();
}

int main(void)
{
    char path[256];
    snprintf(path, sizeof(path), "%s/fsr4-compare-log.txt", output_root());
    log_file = fopen(path, "w");
    int result = fsr4_native_heap_init();
    report("FSR4_COMPARE_BEGIN heap=%d\n", result);
    if (!result) result = run();
    report("FSR4_COMPARE_END result=%s\n", result ? "FAIL" : "COMPLETE");
    if (log_file) fclose(log_file);
    for (;;) usleep(100000);
}
