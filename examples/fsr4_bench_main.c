/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Headless native FSR4 benchmark: inputs at the render size upscaled to the
 * output size (tools/build_fsr4_bench.py; 1280x720 to 1920x1080 by default) by
 * ps5_fsr4 every frame, timed from submission to fence, with one profiled frame
 * that times each pass. No display, scene or present path: it only needs the
 * compute queue, and it writes nothing but a small log.
 */
#include <ps5vk/ps5vk.h>
#include <ps5fsr4/ps5_fsr4.h>
#include "fsr4_bench_config.h"
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

extern int sceKernelDebugOutText(int level, const char *text);
extern int fsr4_native_heap_init(void);

enum { RENDER_W = FSR4_BENCH_RENDER_W, RENDER_H = FSR4_BENCH_RENDER_H,
       OUTPUT_W = FSR4_BENCH_OUTPUT_W, OUTPUT_H = FSR4_BENCH_OUTPUT_H };
enum { FRAMES = 1800, PROFILED = 200, WINDOW = 120, PROFILE_REPEAT = 8 };

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
    report("FSR4_BENCH_ERROR %s = %d\n", #call, (int)rc_); return 1; } } while (0)

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

static int create_image(struct image *img, VkFormat format, uint32_t w, uint32_t h, VkImageUsageFlags usage)
{
    VkImageCreateInfo info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
        .format = format, .extent = {w, h, 1}, .mipLevels = 1, .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL, .usage = usage,
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

static void image_barrier(VkImage image, VkImageLayout from, VkImageLayout to, VkAccessFlags src, VkAccessFlags dst,
                          VkPipelineStageFlags src_stage, VkPipelineStageFlags dst_stage)
{
    VkImageMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, .srcAccessMask = src,
        .dstAccessMask = dst, .oldLayout = from, .newLayout = to, .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .image = image,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 0, NULL, 0, NULL, 1, &b);
}

static int begin(void)
{
    VkCommandBufferBeginInfo bi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    CHECK(vkBeginCommandBuffer(cmd, &bi));
    return 0;
}

static int submit(double *elapsed)
{
    CHECK(vkEndCommandBuffer(cmd));
    VkSubmitInfo s = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &cmd};
    double t0 = now_ms();
    CHECK(vkQueueSubmit(queue, 1, &s, fence));
    VkResult wait = vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_C(10000000000));
    if (wait != VK_SUCCESS) {
        report("FSR4_BENCH_ERROR fence=%d\n", (int)wait);
        for (;;) usleep(100000);  /* never release resources with unknown completion */
    }
    *elapsed = now_ms() - t0;
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

static uint16_t half(float v)  /* finite, normal inputs only */
{
    uint32_t bits;
    memcpy(&bits, &v, 4);
    uint32_t exponent = ((bits >> 23) & 0xff) - 127 + 15;
    return (uint16_t)(((bits >> 16) & 0x8000) | (exponent << 10) | ((bits >> 13) & 0x3ff));
}

/* A smooth, textured HDR scene with a small pan, so every pass does its usual work. */
static void fill_inputs(unsigned char *mapped, size_t color_bytes, size_t depth_bytes)
{
    uint16_t *color = (uint16_t *)mapped;
    float *depth = (float *)(mapped + color_bytes);
    float *motion = (float *)(mapped + color_bytes + depth_bytes);
    for (uint32_t y = 0; y < RENDER_H; ++y)
        for (uint32_t x = 0; x < RENDER_W; ++x) {
            const size_t i = (size_t)y * RENDER_W + x;
            const float u = x / (float)RENDER_W, v = y / (float)RENDER_H;
            const float pattern = 0.5f + 0.5f * sinf(u * 83.0f) * cosf(v * 57.0f);
            color[i * 4 + 0] = half(0.2f + 1.5f * pattern * u);
            color[i * 4 + 1] = half(0.2f + 1.2f * pattern * v);
            color[i * 4 + 2] = half(0.3f + 0.8f * (1.0f - pattern));
            color[i * 4 + 3] = half(1.0f);
            depth[i] = 0.1f + 0.8f * v + 0.05f * pattern;
            motion[i * 2 + 0] = 0.75f / RENDER_W;
            motion[i * 2 + 1] = -0.25f / RENDER_H;
        }
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

    struct image color, depth, motion, output;
    const VkImageUsageFlags input = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    if (create_image(&color, VK_FORMAT_R16G16B16A16_SFLOAT, RENDER_W, RENDER_H, input) ||
        create_image(&depth, VK_FORMAT_R32_SFLOAT, RENDER_W, RENDER_H, input) ||
        create_image(&motion, VK_FORMAT_R32G32_SFLOAT, RENDER_W, RENDER_H, input) ||
        create_image(&output, VK_FORMAT_R32G32B32A32_SFLOAT, OUTPUT_W, OUTPUT_H,
                     VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT))
        return 1;
    const size_t pixels = (size_t)RENDER_W * RENDER_H;
    const size_t color_bytes = pixels * 8, depth_bytes = pixels * 4, motion_bytes = pixels * 8;
    VkBuffer staging;
    VkDeviceMemory staging_memory;
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = color_bytes + depth_bytes + motion_bytes, .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT};
    CHECK(vkCreateBuffer(device, &bi, NULL, &staging));
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(device, staging, &req);
    VkMemoryAllocateInfo sai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
        .memoryTypeIndex = (uint32_t)memory_type(req.memoryTypeBits,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)};
    CHECK(vkAllocateMemory(device, &sai, NULL, &staging_memory));
    CHECK(vkBindBufferMemory(device, staging, staging_memory, 0));
    unsigned char *mapped;
    CHECK(vkMapMemory(device, staging_memory, 0, VK_WHOLE_SIZE, 0, (void **)&mapped));
    fill_inputs(mapped, color_bytes, depth_bytes);

    size_t cache_bytes = 0;
    void *cache_blob = read_asset("pipeline-cache.bin", &cache_bytes);
    VkPipelineCacheCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO,
                                     .initialDataSize = cache_bytes, .pInitialData = cache_blob};
    VkPipelineCache pipeline_cache;
    CHECK(vkCreatePipelineCache(device, &pci, NULL, &pipeline_cache));
    free(cache_blob);
    ps5fsr4_context_desc cd = {sizeof(cd), physical, device, RENDER_W, RENDER_H, OUTPUT_W, OUTPUT_H,
                               PS5FSR4_FLAG_HIGH_DYNAMIC_RANGE | PS5FSR4_FLAG_AUTO_EXPOSURE |
                               (offered.subgroupSizeControl ? PS5FSR4_FLAG_SUBGROUP_SIZE_CONTROL : 0u),
                               NULL, pipeline_cache};
    ps5fsr4_context *context;
    double t0 = now_ms();
    ps5fsr4_result fr = ps5fsr4_context_create(&cd, &context);
    report("FSR4_BENCH_CONTEXT result=%d create_ms=%.1f cache_in=%zu\n", (int)fr, now_ms() - t0, cache_bytes);
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

    /* Upload the inputs once; every frame reads them again with a moving jitter. */
    if (begin()) return 1;
    struct image *inputs[3] = {&color, &depth, &motion};
    const uint32_t texel[3] = {8, 4, 8};
    VkDeviceSize offset = 0;
    for (int i = 0; i < 3; ++i) {
        image_barrier(inputs[i]->image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
                      VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT);
        VkBufferImageCopy region = {offset, 0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, {0, 0, 0},
                                    {RENDER_W, RENDER_H, 1}};
        vkCmdCopyBufferToImage(cmd, staging, inputs[i]->image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
        image_barrier(inputs[i]->image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                      VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                      VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        offset += pixels * texel[i];
    }
    image_barrier(output.image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0, VK_ACCESS_SHADER_WRITE_BIT,
                  VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    double upload_ms;
    if (submit(&upload_ms)) return 1;
    /* ps5vk writes back and invalidates every mapped HOST_COHERENT range around each
     * submission: left mapped, the uploaded inputs would add that to every timed frame. */
    vkUnmapMemory(device, staging_memory);
    report("FSR4_BENCH_READY render=%dx%d output=%dx%d upload_ms=%.1f\n", RENDER_W, RENDER_H, OUTPUT_W, OUTPUT_H,
           upload_ms);

    const uint32_t phases = ps5fsr4_jitter_phase_count(RENDER_W, OUTPUT_W);
    double sum = 0, best = INFINITY, total = 0;
    uint32_t timed = 0;
    int reset = 1;
    for (uint32_t frame = 0; frame < FRAMES; ++frame) {
        float jx, jy;
        ps5fsr4_jitter_offset(frame, phases, &jx, &jy);
        ps5fsr4_dispatch_desc dd = {.struct_size = sizeof(dd), .command_buffer = cmd,
            .color = color.view, .depth = depth.view, .motion_vectors = motion.view, .output = output.view,
            .color_layout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            .depth_layout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            .motion_vectors_layout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            .render_width = RENDER_W, .render_height = RENDER_H, .jitter_x = jx, .jitter_y = jy,
            .motion_vector_scale_x = RENDER_W, .motion_vector_scale_y = RENDER_H, .pre_exposure = 1.0f,
            .frame_time_delta_ms = 16.666667f, .camera_near = 0.1f, .camera_far = 200.0f,
            .camera_fov_vertical = 1.0471976f, .reset = reset};
        double fsr_ms;
        if (frame == PROFILED) {
            /* Each pass once, then PROFILE_REPEAT times in one submission: the
             * difference over the extra runs is its GPU time without the round
             * trip. Pass 0 writes the frame's descriptors, so it runs only once. */
            for (uint32_t pass = 0; pass < ps5fsr4_pass_count(); ++pass) {
                const int repeat = pass ? PROFILE_REPEAT : 1;
                double once_ms = 0, repeated_ms = 0;
                for (int runs = 1; runs <= repeat; runs += PROFILE_REPEAT - 1) {
                    if (begin()) return 1;
                    for (int i = 0; i < runs; ++i) {
                        fr = ps5fsr4_dispatch_passes(context, &dd, pass, 1);
                        if (fr) { report("FSR4_BENCH_ERROR dispatch=%d\n", (int)fr); return 1; }
                        if (i + 1 < runs) {
                            VkMemoryBarrier b = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL, VK_ACCESS_SHADER_WRITE_BIT,
                                                 VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT};
                            vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                                                 VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, 0, 1, &b, 0, NULL, 0, NULL);
                        }
                    }
                    if (submit(runs == 1 ? &once_ms : &repeated_ms)) return 1;
                }
                const double gpu_ms = pass ? (repeated_ms - once_ms) / (PROFILE_REPEAT - 1) : -1.0;
                report("FSR4_BENCH_PASS index=%u gpu_ms=%.3f submit_ms=%.3f\n", pass, gpu_ms, once_ms - gpu_ms);
            }
            reset = 1;  /* the repeated passes disturbed the history */
            continue;
        }
        if (begin()) return 1;
        fr = ps5fsr4_dispatch(context, &dd);
        if (fr) { report("FSR4_BENCH_ERROR dispatch=%d\n", (int)fr); return 1; }
        if (submit(&fsr_ms)) return 1;
        reset = 0;
        sum += fsr_ms;
        total += fsr_ms;
        ++timed;
        if (fsr_ms < best) best = fsr_ms;
        if ((frame + 1) % WINDOW == 0) {
            report("FSR4_BENCH_STATS frames=%u fsr4_ms=%.3f best_ms=%.3f\n", frame + 1, sum / WINDOW, best);
            sum = 0;
            best = INFINITY;
        }
    }
    report("FSR4_BENCH_SUMMARY frames=%u mean_ms=%.3f\n", timed, total / timed);
    return 0;
}

int main(void)
{
    char path[256];
    snprintf(path, sizeof(path), "%s/fsr4-bench-log.txt", output_root());
    log_file = fopen(path, "w");
    int result = fsr4_native_heap_init();
    report("FSR4_BENCH_BEGIN heap=%d\n", result);
    if (!result) result = run();
    report("FSR4_BENCH_END result=%s\n", result ? "FAIL" : "COMPLETE");
    if (log_file) fclose(log_file);
    for (;;) usleep(100000);
}
