/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Runs the ps5_fsr4 runtime (not a captured replay) on reference inputs, frame
 * by frame, and measures every output against the reference output.
 */
#ifdef FSR4_HOST
#include <vulkan/vulkan.h>
#else
#include <ps5vk/ps5vk.h>
#include <sys/stat.h>
#endif
#include <ps5fsr4/ps5_fsr4.h>
#include <errno.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "fsr4_runtime_fixture.h"

static FILE *log_file;
#ifdef FSR4_HOST
#define ASSET_ROOT (getenv("FSR4_ASSET_DIR") ? getenv("FSR4_ASSET_DIR") : "assets")
#define OUTPUT_ROOT (getenv("FSR4_OUTPUT_DIR") ? getenv("FSR4_OUTPUT_DIR") : ".")
static int sceKernelDebugOutText(int level, const char *text) { (void)level; return fputs(text, stdout); }
#else
#define ASSET_ROOT "/app0/assets"
#define OUTPUT_ROOT output_root()
extern int sceKernelDebugOutText(int level, const char *text);
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
#endif

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
    report("FSR4_RT_ERROR %s = %d\n", #call, (int)rc_); return 1; } } while (0)

static int load(const char *name, void *data, size_t bytes)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s/%s", ASSET_ROOT, name);
    FILE *f = fopen(path, "rb");
    if (!f) { report("FSR4_RT_ERROR asset %s errno=%d\n", path, errno); return 1; }
    size_t got = fread(data, 1, bytes, f);
    int extra = fgetc(f);
    fclose(f);
    if (got != bytes || extra != EOF) { report("FSR4_RT_ERROR asset %s size\n", path); return 1; }
    return 0;
}

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static VkDevice device;
static VkPhysicalDeviceMemoryProperties memory_properties;

static int memory_type(uint32_t bits, VkMemoryPropertyFlags flags)
{
    for (uint32_t i = 0; i < memory_properties.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (memory_properties.memoryTypes[i].propertyFlags & flags) == flags)
            return (int)i;
    return -1;
}

struct image { VkImage image; VkDeviceMemory memory; VkImageView view; VkFormat format; uint32_t w, h, texel; };

static int create_image(struct image *img, VkFormat format, uint32_t texel, uint32_t w, uint32_t h,
                        VkImageUsageFlags usage)
{
    img->format = format; img->w = w; img->h = h; img->texel = texel;
    VkImageCreateInfo info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    info.imageType = VK_IMAGE_TYPE_2D; info.format = format; info.extent = (VkExtent3D){w, h, 1};
    info.mipLevels = 1; info.arrayLayers = 1; info.samples = VK_SAMPLE_COUNT_1_BIT;
    info.tiling = VK_IMAGE_TILING_OPTIMAL; info.usage = usage; info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    CHECK(vkCreateImage(device, &info, NULL, &img->image));
    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(device, img->image, &req);
    int type = memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    if (type < 0) type = memory_type(req.memoryTypeBits, 0);
    VkMemoryAllocateInfo ai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size, (uint32_t)type};
    CHECK(vkAllocateMemory(device, &ai, NULL, &img->memory));
    CHECK(vkBindImageMemory(device, img->image, img->memory, 0));
    VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vi.image = img->image; vi.viewType = VK_IMAGE_VIEW_TYPE_2D; vi.format = format;
    vi.subresourceRange = (VkImageSubresourceRange){VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    CHECK(vkCreateImageView(device, &vi, NULL, &img->view));
    return 0;
}

static void barrier(VkCommandBuffer cmd, VkImage image, VkImageLayout from, VkImageLayout to,
                    VkAccessFlags src, VkAccessFlags dst, VkPipelineStageFlags src_stage, VkPipelineStageFlags dst_stage)
{
    VkImageMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.srcAccessMask = src; b.dstAccessMask = dst; b.oldLayout = from; b.newLayout = to;
    b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = image; b.subresourceRange = (VkImageSubresourceRange){VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 0, NULL, 0, NULL, 1, &b);
}

struct metrics { double max_error, rmse, psnr; size_t nonfinite, differing; };

static struct metrics measure(const float *actual, const float *expected, size_t pixels)
{
    struct metrics m = {0};
    double sum = 0;
    for (size_t i = 0; i < pixels * 4; ++i) {
        if (actual[i] - actual[i] != 0.0f) { ++m.nonfinite; continue; }
        if (actual[i] != expected[i]) ++m.differing;
        if (i % 4 == 3) continue;
        double e = fabs((double)actual[i] - expected[i]);
        if (e > m.max_error) m.max_error = e;
        sum += e * e;
    }
    m.rmse = sqrt(sum / (pixels * 3));
    m.psnr = m.rmse > 0 ? -20 * log10(m.rmse) : INFINITY;
    return m;
}

static int run(void)
{
    VkInstanceCreateInfo ii = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.apiVersion = VK_API_VERSION_1_3;
    ii.pApplicationInfo = &app;
    VkInstance instance;
    CHECK(vkCreateInstance(&ii, NULL, &instance));
    uint32_t count = 1;
    VkPhysicalDevice physical;
    CHECK(vkEnumeratePhysicalDevices(instance, &count, &physical));
    float priority = 1;
    VkDeviceQueueCreateInfo qi = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qi.queueCount = 1; qi.pQueuePriorities = &priority;
    VkPhysicalDeviceFeatures features = {0};
#ifndef FSR4_PS5VK_HOST  /* the host ps5vk validator build does not advertise it */
    features.shaderInt16 = VK_TRUE;
#endif
    const char *extension = VK_KHR_STORAGE_BUFFER_STORAGE_CLASS_EXTENSION_NAME;
    VkDeviceCreateInfo di = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    di.queueCreateInfoCount = 1; di.pQueueCreateInfos = &qi; di.pEnabledFeatures = &features;
    di.enabledExtensionCount = 1; di.ppEnabledExtensionNames = &extension;
#if defined(FSR4_HOST) && !defined(FSR4_PS5VK_HOST)  /* conformant host drivers need the declared features */
    VkPhysicalDeviceVulkan13Features f13 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    VkPhysicalDeviceVulkan12Features f12 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES, .pNext = &f13};
    VkPhysicalDeviceVulkan11Features f11 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_1_FEATURES, .pNext = &f12};
    VkPhysicalDeviceFeatures2 all = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, .pNext = &f11};
    vkGetPhysicalDeviceFeatures2(physical, &all);
    f13.robustImageAccess = VK_FALSE; all.features.robustBufferAccess = VK_FALSE;
    di.pNext = &all; di.pEnabledFeatures = NULL; di.enabledExtensionCount = 0;
#endif
    CHECK(vkCreateDevice(physical, &di, NULL, &device));
    VkQueue queue;
    vkGetDeviceQueue(device, 0, 0, &queue);
    vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);

    const uint32_t rw = FSR4_RT_RENDER_WIDTH, rh = FSR4_RT_RENDER_HEIGHT;
    const uint32_t ow = FSR4_RT_OUTPUT_WIDTH, oh = FSR4_RT_OUTPUT_HEIGHT;
    struct image color, depth, motion, output;
    const VkImageUsageFlags input = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    if (create_image(&color, VK_FORMAT_R32G32B32A32_SFLOAT, 16, rw, rh, input) ||
        create_image(&depth, VK_FORMAT_R32_SFLOAT, 4, rw, rh, input) ||
        create_image(&motion, VK_FORMAT_R32G32_SFLOAT, 8, rw, rh, input) ||
        create_image(&output, VK_FORMAT_R32G32B32A32_SFLOAT, 16, ow, oh,
                     VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT))
        return 1;
    const size_t color_bytes = (size_t)rw * rh * 16, depth_bytes = (size_t)rw * rh * 4;
    const size_t motion_bytes = (size_t)rw * rh * 8, output_bytes = (size_t)ow * oh * 16;
    const size_t staging_bytes = color_bytes + depth_bytes + motion_bytes + output_bytes;
    VkBuffer staging;
    VkDeviceMemory staging_memory;
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = staging_bytes;
    bi.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    CHECK(vkCreateBuffer(device, &bi, NULL, &staging));
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(device, staging, &req);
    VkMemoryAllocateInfo ai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size,
        (uint32_t)memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)};
    CHECK(vkAllocateMemory(device, &ai, NULL, &staging_memory));
    CHECK(vkBindBufferMemory(device, staging, staging_memory, 0));
    unsigned char *mapped;
    CHECK(vkMapMemory(device, staging_memory, 0, VK_WHOLE_SIZE, 0, (void **)&mapped));
    float *expected = malloc(output_bytes);
    if (!expected) return 1;

    /* A pipeline cache saved by an earlier run makes context creation skip compilation. */
    VkPipelineCacheCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO};
    void *cache_blob = NULL;
    size_t cache_bytes = 0;
    {
        char path[1024];
        snprintf(path, sizeof(path), "%s/pipeline-cache.bin", ASSET_ROOT);
        FILE *f = fopen(path, "rb");
        if (f && !fseek(f, 0, SEEK_END) && (cache_bytes = (size_t)ftell(f)) && !fseek(f, 0, SEEK_SET) &&
            (cache_blob = malloc(cache_bytes)) && fread(cache_blob, 1, cache_bytes, f) == cache_bytes) {
            pci.initialDataSize = cache_bytes;
            pci.pInitialData = cache_blob;
        }
        if (f) fclose(f);
    }
    VkPipelineCache pipeline_cache;
    CHECK(vkCreatePipelineCache(device, &pci, NULL, &pipeline_cache));
    free(cache_blob);
    ps5fsr4_context_desc cd = {sizeof(cd), physical, device, rw, rh, ow, oh, FSR4_RT_FLAGS, NULL, pipeline_cache};
    ps5fsr4_context *context;
    double t0 = now_ms();
    ps5fsr4_result fr = ps5fsr4_context_create(&cd, &context);
    report("FSR4_RT_CONTEXT result=%d create_ms=%.1f cache_in=%zu\n", (int)fr, now_ms() - t0,
           (size_t)pci.initialDataSize);
    if (fr) return 1;
    {
        size_t bytes = 0;
        void *blob = NULL;
        char path[1024];
        snprintf(path, sizeof(path), "%s/fsr4-rt-pipeline-cache.bin", OUTPUT_ROOT);
        if (vkGetPipelineCacheData(device, pipeline_cache, &bytes, NULL) == VK_SUCCESS && (blob = malloc(bytes)) &&
            vkGetPipelineCacheData(device, pipeline_cache, &bytes, blob) == VK_SUCCESS) {
            FILE *f = fopen(path, "wb");
            if (f) { fwrite(blob, 1, bytes, f); fclose(f); }
            report("FSR4_RT_PIPELINE_CACHE bytes=%zu\n", bytes);
        }
        free(blob);
    }

    VkCommandPoolCreateInfo pi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pi.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    VkCommandPool pool;
    CHECK(vkCreateCommandPool(device, &pi, NULL, &pool));
    VkCommandBufferAllocateInfo cai = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, NULL, pool,
                                       VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
    VkCommandBuffer cmd;
    CHECK(vkAllocateCommandBuffers(device, &cai, &cmd));
    VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    VkFence fence;
    CHECK(vkCreateFence(device, &fi, NULL, &fence));

    int failed = 0;
    double worst_psnr = INFINITY;
    for (uint32_t f = 0; f < FSR4_RT_FRAMES; ++f) {
        char name[64];
        snprintf(name, sizeof(name), "frame%u-color.bin", f);
        if (load(name, mapped, color_bytes)) return 1;
        snprintf(name, sizeof(name), "frame%u-depth.bin", f);
        if (load(name, mapped + color_bytes, depth_bytes)) return 1;
        snprintf(name, sizeof(name), "frame%u-motion.bin", f);
        if (load(name, mapped + color_bytes + depth_bytes, motion_bytes)) return 1;
        snprintf(name, sizeof(name), "frame%u-expected.bin", f);
        if (load(name, expected, output_bytes)) return 1;

        VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        CHECK(vkBeginCommandBuffer(cmd, &begin));
        struct image *inputs[3] = {&color, &depth, &motion};
        VkDeviceSize offset = 0;
        for (int i = 0; i < 3; ++i) {
            barrier(cmd, inputs[i]->image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    0, VK_ACCESS_TRANSFER_WRITE_BIT,
                    VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT);
            VkBufferImageCopy region = {offset, 0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, {0, 0, 0},
                                        {inputs[i]->w, inputs[i]->h, 1}};
            vkCmdCopyBufferToImage(cmd, staging, inputs[i]->image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
            barrier(cmd, inputs[i]->image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                    VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT,
                    VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
            offset += (VkDeviceSize)inputs[i]->w * inputs[i]->h * inputs[i]->texel;
        }
        barrier(cmd, output.image, f ? VK_IMAGE_LAYOUT_GENERAL : VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL,
                f ? VK_ACCESS_TRANSFER_READ_BIT : 0, VK_ACCESS_SHADER_WRITE_BIT,
                VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        ps5fsr4_dispatch_desc dd = {.struct_size = sizeof(dd)};
        dd.command_buffer = cmd;
        dd.color = color.view; dd.depth = depth.view; dd.motion_vectors = motion.view; dd.output = output.view;
        dd.color_layout = dd.depth_layout = dd.motion_vectors_layout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        dd.render_width = fsr4_rt_frames[f].render_width;
        dd.render_height = fsr4_rt_frames[f].render_height;
        dd.jitter_x = fsr4_rt_frames[f].jitter_x;
        dd.jitter_y = fsr4_rt_frames[f].jitter_y;
        dd.motion_vector_scale_x = (float)dd.render_width;
        dd.motion_vector_scale_y = (float)dd.render_height;
        dd.pre_exposure = 1.0f;
        dd.frame_time_delta_ms = 16.666667f;
        dd.camera_near = 0.1f; dd.camera_far = 100.0f; dd.camera_fov_vertical = 1.04719755f;
        dd.reset = fsr4_rt_frames[f].reset;
        fr = ps5fsr4_dispatch(context, &dd);
        if (fr) { report("FSR4_RT_ERROR dispatch=%d\n", (int)fr); return 1; }
        barrier(cmd, output.image, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_GENERAL,
                VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT,
                VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT);
        VkBufferImageCopy out_region = {offset, 0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, {0, 0, 0}, {ow, oh, 1}};
        vkCmdCopyImageToBuffer(cmd, output.image, VK_IMAGE_LAYOUT_GENERAL, staging, 1, &out_region);
        VkMemoryBarrier host = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_HOST_READ_BIT};
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &host, 0, NULL, 0, NULL);
        CHECK(vkEndCommandBuffer(cmd));
        VkSubmitInfo submit = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO};
        submit.commandBufferCount = 1; submit.pCommandBuffers = &cmd;
        t0 = now_ms();
        CHECK(vkQueueSubmit(queue, 1, &submit, fence));
#ifdef FSR4_HOST
        VkResult wait = vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_MAX);
#else
        VkResult wait = vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_C(10000000000));
#endif
        double elapsed = now_ms() - t0;
        if (wait != VK_SUCCESS) {
            report("FSR4_RT_ERROR fence=%d frame=%u\n", (int)wait, f);
            for (;;) usleep(100000);  /* never release resources with unknown completion */
        }
        CHECK(vkResetFences(device, 1, &fence));
        CHECK(vkResetCommandBuffer(cmd, 0));
        const float *actual = (const float *)(mapped + offset);
        struct metrics m = measure(actual, expected, (size_t)ow * oh);
        if (m.psnr < worst_psnr) worst_psnr = m.psnr;
        failed |= m.nonfinite != 0;
        report("FSR4_RT_FRAME frame=%u reset=%u gpu_ms=%.2f max=%.6g rmse=%.6g psnr=%.2f nonfinite=%zu differing=%zu\n",
               f, fsr4_rt_frames[f].reset, elapsed, m.max_error, m.rmse, m.psnr, m.nonfinite, m.differing);
        snprintf(name, sizeof(name), "%s/fsr4-rt-frame%u.bin", OUTPUT_ROOT, f);
        FILE *o = fopen(name, "wb");
        if (!o || fwrite(actual, 1, output_bytes, o) != output_bytes) failed = 1;
        if (o) fclose(o);
    }
    ps5fsr4_context_destroy(context);
    vkDestroyFence(device, fence, NULL);
    vkDestroyCommandPool(device, pool, NULL);
    struct image *images[4] = {&color, &depth, &motion, &output};
    for (int i = 0; i < 4; ++i) {
        vkDestroyImageView(device, images[i]->view, NULL);
        vkDestroyImage(device, images[i]->image, NULL);
        vkFreeMemory(device, images[i]->memory, NULL);
    }
    vkUnmapMemory(device, staging_memory);
    vkDestroyBuffer(device, staging, NULL);
    vkFreeMemory(device, staging_memory, NULL);
    free(expected);
    vkDestroyPipelineCache(device, pipeline_cache, NULL);
    vkDestroyDevice(device, NULL);
    vkDestroyInstance(instance, NULL);
    report("FSR4_RT_SUMMARY frames=%u worst_psnr=%.2f\n", FSR4_RT_FRAMES, worst_psnr);
    return failed;
}

#ifdef FSR4_HOST
int fsr4_native_heap_init(void) { return 0; }
#else
extern int fsr4_native_heap_init(void);
#endif

int main(void)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s/fsr4-rt-result.txt", OUTPUT_ROOT);
    log_file = fopen(path, "w");
    int result = fsr4_native_heap_init();
    report("FSR4_RT_BEGIN fixture=%s scenario=%s frames=%u render=%ux%u output=%ux%u heap=%d output=%s\n",
           FSR4_RT_ID, FSR4_RT_SCENARIO, FSR4_RT_FRAMES, FSR4_RT_RENDER_WIDTH, FSR4_RT_RENDER_HEIGHT,
           FSR4_RT_OUTPUT_WIDTH, FSR4_RT_OUTPUT_HEIGHT, result, OUTPUT_ROOT);
    if (!result) result = run();
    report("FSR4_RT_END result=%s\n", result ? "FAIL" : "COMPLETE");
    if (log_file) fclose(log_file);
#ifdef FSR4_HOST
    return result;
#endif
    for (;;) usleep(100000);
}
