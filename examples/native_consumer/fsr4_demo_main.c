/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Interactive native FSR4 demo: a compute-rendered scene at 1280x720 with
 * sub-pixel jitter and motion vectors, upscaled to 1920x1080 by ps5_fsr4 and
 * shown on VideoOut. A compute pass composes the displayed frame and a
 * full-screen draw writes it into the display image.
 *
 * Controls: left stick orbits, right stick zooms, Cross cycles FSR4 / split /
 * bilinear, Square toggles the automatic camera, Triangle resets history.
 * Without input the camera orbits by itself and the view mode cycles.
 */
#include <ps5vk/ps5vk.h>
#include <ps5vk/ps5vk_present.h>
#include <ps5fsr4/ps5_fsr4.h>
#include <errno.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "fsr4_demo_shaders.h"

extern int sceKernelDebugOutText(int level, const char *text);
extern int sceUserServiceInitialize(void *params);
extern int sceUserServiceGetInitialUser(int32_t *user);
extern int scePadInit(void);
extern int scePadOpen(int32_t user, int32_t type, int32_t index, const void *params);
extern int scePadReadState(int32_t handle, void *data);
extern int fsr4_native_heap_init(void);

enum { RENDER_W = 1280, RENDER_H = 720, OUTPUT_W = 1920, OUTPUT_H = 1080 };
enum { PAD_CROSS = 0x4000, PAD_SQUARE = 0x8000, PAD_TRIANGLE = 0x1000, PAD_R1 = 0x800, PAD_L1 = 0x400 };

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
    report("FSR4_DEMO_ERROR %s = %d\n", #call, (int)rc_); return 1; } } while (0)

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

struct image { VkImage image; VkDeviceMemory memory; VkImageView view; };

static int create_image(struct image *img, VkFormat format, uint32_t w, uint32_t h, int sampled)
{
    VkImageCreateInfo info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    info.imageType = VK_IMAGE_TYPE_2D; info.format = format; info.extent = (VkExtent3D){w, h, 1};
    info.mipLevels = 1; info.arrayLayers = 1; info.samples = VK_SAMPLE_COUNT_1_BIT;
    info.tiling = VK_IMAGE_TILING_OPTIMAL; info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    info.usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT |
                 (sampled ? VK_IMAGE_USAGE_SAMPLED_BIT : 0u);
    CHECK(vkCreateImage(device, &info, NULL, &img->image));
    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(device, img->image, &req);
    VkMemoryAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
                               .memoryTypeIndex = (uint32_t)memory_type(req.memoryTypeBits, 0)};
    CHECK(vkAllocateMemory(device, &ai, NULL, &img->memory));
    CHECK(vkBindImageMemory(device, img->image, img->memory, 0));
    VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = img->image,
        .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = format,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    CHECK(vkCreateImageView(device, &vi, NULL, &img->view));
    return 0;
}

struct compute {
    VkDescriptorSetLayout set_layout;
    VkPipelineLayout layout;
    VkPipeline pipeline;
    VkDescriptorSet set;
};

static int create_compute(struct compute *c, const uint32_t *code, size_t bytes, const VkDescriptorType *types,
                          uint32_t count, uint32_t push_bytes, VkDescriptorPool pool, VkPipelineCache cache)
{
    VkDescriptorSetLayoutBinding bindings[4];
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
    VkDescriptorSetAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                      .descriptorPool = pool, .descriptorSetCount = 1, .pSetLayouts = &c->set_layout};
    CHECK(vkAllocateDescriptorSets(device, &ai, &c->set));
    return 0;
}

static void bind_image(VkDescriptorSet set, uint32_t binding, VkImageView view)
{
    VkDescriptorImageInfo info = {VK_NULL_HANDLE, view, VK_IMAGE_LAYOUT_GENERAL};
    VkWriteDescriptorSet w = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = binding,
        .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, .pImageInfo = &info};
    vkUpdateDescriptorSets(device, 1, &w, 0, NULL);
}

static void memory_barrier(VkCommandBuffer cmd, VkAccessFlags src, VkAccessFlags dst,
                           VkPipelineStageFlags src_stage, VkPipelineStageFlags dst_stage)
{
    VkMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = src, .dstAccessMask = dst};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 1, &b, 0, NULL, 0, NULL);
}

static void image_barrier(VkCommandBuffer cmd, VkImage image, VkImageLayout from, VkImageLayout to,
                          VkAccessFlags src, VkAccessFlags dst, VkPipelineStageFlags src_stage,
                          VkPipelineStageFlags dst_stage)
{
    VkImageMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, .srcAccessMask = src,
        .dstAccessMask = dst, .oldLayout = from, .newLayout = to,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .image = image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 0, NULL, 0, NULL, 1, &b);
}

/* Full-screen draw that copies the composed frame into a display image. */
struct blit {
    VkSampler sampler;
    VkDescriptorSetLayout set_layout;
    VkPipelineLayout layout;
    VkDescriptorSet set;
    VkRenderPass pass;
    VkImageView views[2];
    VkFramebuffer framebuffers[2];
    VkPipeline pipeline;
};

static int create_shader(const uint32_t *code, size_t bytes, VkShaderModule *module)
{
    VkShaderModuleCreateInfo mi = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = bytes, .pCode = code};
    CHECK(vkCreateShaderModule(device, &mi, NULL, module));
    return 0;
}

static int create_blit(struct blit *b, const VkImage display[2], VkImageView frame, VkDescriptorPool pool,
                       VkPipelineCache cache)
{
    VkSamplerCreateInfo si = {.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO, .magFilter = VK_FILTER_NEAREST,
        .minFilter = VK_FILTER_NEAREST, .mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST,
        .addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, .addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE};
    CHECK(vkCreateSampler(device, &si, NULL, &b->sampler));
    VkDescriptorSetLayoutBinding binding = {0, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1,
                                            VK_SHADER_STAGE_FRAGMENT_BIT, NULL};
    VkDescriptorSetLayoutCreateInfo sl = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                          .bindingCount = 1, .pBindings = &binding};
    CHECK(vkCreateDescriptorSetLayout(device, &sl, NULL, &b->set_layout));
    VkPipelineLayoutCreateInfo pl = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
                                     .pSetLayouts = &b->set_layout};
    CHECK(vkCreatePipelineLayout(device, &pl, NULL, &b->layout));
    VkDescriptorSetAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
                                      .descriptorPool = pool, .descriptorSetCount = 1, .pSetLayouts = &b->set_layout};
    CHECK(vkAllocateDescriptorSets(device, &ai, &b->set));
    VkDescriptorImageInfo ii = {b->sampler, frame, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
    VkWriteDescriptorSet w = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = b->set, .dstBinding = 0,
        .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .pImageInfo = &ii};
    vkUpdateDescriptorSets(device, 1, &w, 0, NULL);

    VkAttachmentDescription attachment = {.format = VK_FORMAT_B8G8R8A8_UNORM, .samples = VK_SAMPLE_COUNT_1_BIT,
        .loadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE, .storeOp = VK_ATTACHMENT_STORE_OP_STORE,
        .stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE, .stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE,
        .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED, .finalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkAttachmentReference color_ref = {0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkSubpassDescription subpass = {.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS,
                                    .colorAttachmentCount = 1, .pColorAttachments = &color_ref};
    VkRenderPassCreateInfo rp = {.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO, .attachmentCount = 1,
                                 .pAttachments = &attachment, .subpassCount = 1, .pSubpasses = &subpass};
    CHECK(vkCreateRenderPass(device, &rp, NULL, &b->pass));
    for (int i = 0; i < 2; ++i) {
        VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = display[i],
            .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = VK_FORMAT_B8G8R8A8_UNORM,
            .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
        CHECK(vkCreateImageView(device, &vi, NULL, &b->views[i]));
        VkFramebufferCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO, .renderPass = b->pass,
            .attachmentCount = 1, .pAttachments = &b->views[i], .width = OUTPUT_W, .height = OUTPUT_H, .layers = 1};
        CHECK(vkCreateFramebuffer(device, &fi, NULL, &b->framebuffers[i]));
    }

    VkShaderModule vs, fs;
    if (create_shader(fsr4_demo_blit_vert_spv, sizeof(fsr4_demo_blit_vert_spv), &vs) ||
        create_shader(fsr4_demo_blit_frag_spv, sizeof(fsr4_demo_blit_frag_spv), &fs))
        return 1;
    VkPipelineShaderStageCreateInfo stages[2] = {
        {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_VERTEX_BIT,
         .module = vs, .pName = "main"},
        {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_FRAGMENT_BIT,
         .module = fs, .pName = "main"}};
    VkPipelineVertexInputStateCreateInfo vertex = {.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};
    VkPipelineInputAssemblyStateCreateInfo assembly = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
        .topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST};
    VkViewport viewport = {0.0f, 0.0f, (float)OUTPUT_W, (float)OUTPUT_H, 0.0f, 1.0f};
    VkRect2D scissor = {{0, 0}, {OUTPUT_W, OUTPUT_H}};
    VkPipelineViewportStateCreateInfo viewports = {.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,
        .viewportCount = 1, .pViewports = &viewport, .scissorCount = 1, .pScissors = &scissor};
    VkPipelineRasterizationStateCreateInfo raster = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO, .lineWidth = 1.0f};
    VkPipelineMultisampleStateCreateInfo multisample = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,
        .rasterizationSamples = VK_SAMPLE_COUNT_1_BIT};
    VkPipelineColorBlendAttachmentState blend_attachment = {.colorWriteMask = 0xf};
    VkPipelineColorBlendStateCreateInfo blend = {.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO,
        .attachmentCount = 1, .pAttachments = &blend_attachment};
    VkGraphicsPipelineCreateInfo gi = {.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO, .stageCount = 2,
        .pStages = stages, .pVertexInputState = &vertex, .pInputAssemblyState = &assembly,
        .pViewportState = &viewports, .pRasterizationState = &raster, .pMultisampleState = &multisample,
        .pColorBlendState = &blend, .layout = b->layout, .renderPass = b->pass};
    CHECK(vkCreateGraphicsPipelines(device, cache, 1, &gi, NULL, &b->pipeline));
    vkDestroyShaderModule(device, vs, NULL);
    vkDestroyShaderModule(device, fs, NULL);
    return 0;
}

static VkQueue queue;
static VkCommandBuffer cmd;
static VkFence fence;

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
        report("FSR4_DEMO_ERROR fence=%d\n", (int)wait);
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

static float stick(uint8_t v)
{
    float x = ((float)v - 128.0f) / 127.0f;
    return fabsf(x) < 0.15f ? 0.0f : x;
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
    VkDeviceCreateInfo di = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1,
        .pQueueCreateInfos = &qi, .pEnabledFeatures = &features, .enabledExtensionCount = 1,
        .ppEnabledExtensionNames = &extension};
    CHECK(vkCreateDevice(physical, &di, NULL, &device));
    vkGetDeviceQueue(device, 0, 0, &queue);
    vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);

    struct image color, depth, motion, upscaled, composed;
    if (create_image(&color, VK_FORMAT_R16G16B16A16_SFLOAT, RENDER_W, RENDER_H, 1) ||
        create_image(&depth, VK_FORMAT_R32_SFLOAT, RENDER_W, RENDER_H, 1) ||
        create_image(&motion, VK_FORMAT_R32G32B32A32_SFLOAT, RENDER_W, RENDER_H, 1) ||
        create_image(&upscaled, VK_FORMAT_R32G32B32A32_SFLOAT, OUTPUT_W, OUTPUT_H, 0) ||
        create_image(&composed, VK_FORMAT_R8G8B8A8_UNORM, OUTPUT_W, OUTPUT_H, 1))
        return 1;

    /* Packed BGRA8 screenshots, written by the present shader when asked. */
    VkBuffer staging;
    VkDeviceMemory staging_memory;
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = (VkDeviceSize)OUTPUT_W * OUTPUT_H * 4,
        .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT};
    CHECK(vkCreateBuffer(device, &bi, NULL, &staging));
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(device, staging, &req);
    VkMemoryAllocateInfo sai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
        .memoryTypeIndex = (uint32_t)memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)};
    CHECK(vkAllocateMemory(device, &sai, NULL, &staging_memory));
    CHECK(vkBindBufferMemory(device, staging, staging_memory, 0));
    void *staging_mapped;
    CHECK(vkMapMemory(device, staging_memory, 0, VK_WHOLE_SIZE, 0, &staging_mapped));

    /* Two scanout images in the one 128 MiB envelope VideoOut registers. */
    VkImageCreateInfo di_info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
        .format = VK_FORMAT_B8G8R8A8_UNORM, .extent = {OUTPUT_W, OUTPUT_H, 1}, .mipLevels = 1, .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT};
    VkImage display[2];
    CHECK(vkCreateImage(device, &di_info, NULL, &display[0]));
    CHECK(vkCreateImage(device, &di_info, NULL, &display[1]));
    VkMemoryAllocateInfo dai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                .allocationSize = UINT64_C(0x08000000), .memoryTypeIndex = 0};
    VkDeviceMemory display_memory;
    CHECK(vkAllocateMemory(device, &dai, NULL, &display_memory));
    CHECK(vkBindImageMemory(device, display[0], display_memory, 0));
    CHECK(vkBindImageMemory(device, display[1], display_memory, UINT64_C(0x04000000)));
    struct ps5vk_present_config pconfig = {OUTPUT_W, OUTPUT_H, VK_FORMAT_B8G8R8A8_UNORM, 2};
    ps5vk_present_surface surface;
    CHECK(ps5vkCreatePresentSurface(device, &pconfig, 2, display, &surface));

    size_t cache_bytes = 0;
    void *cache_blob = read_asset("pipeline-cache.bin", &cache_bytes);
    VkPipelineCacheCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO,
                                     .initialDataSize = cache_bytes, .pInitialData = cache_blob};
    VkPipelineCache pipeline_cache;
    CHECK(vkCreatePipelineCache(device, &pci, NULL, &pipeline_cache));
    free(cache_blob);

    VkDescriptorPoolSize sizes[3] = {{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 6}, {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1},
                                     {VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1}};
    VkDescriptorPoolCreateInfo dpi = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 3,
                                      .poolSizeCount = 3, .pPoolSizes = sizes};
    VkDescriptorPool pool;
    CHECK(vkCreateDescriptorPool(device, &dpi, NULL, &pool));
    const VkDescriptorType scene_types[3] = {VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                                             VK_DESCRIPTOR_TYPE_STORAGE_IMAGE};
    const VkDescriptorType present_types[4] = {VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                                               VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE};
    struct compute scene, present;
    struct blit blit;
    double t0 = now_ms();
    if (create_compute(&scene, fsr4_demo_scene_spv, sizeof(fsr4_demo_scene_spv), scene_types, 3, 48, pool,
                       pipeline_cache) ||
        create_compute(&present, fsr4_demo_present_spv, sizeof(fsr4_demo_present_spv), present_types, 4, 32,
                       pool, pipeline_cache) ||
        create_blit(&blit, display, composed.view, pool, pipeline_cache))
        return 1;
    bind_image(scene.set, 0, color.view);
    bind_image(scene.set, 1, depth.view);
    bind_image(scene.set, 2, motion.view);
    bind_image(present.set, 0, upscaled.view);
    bind_image(present.set, 1, color.view);
    bind_image(present.set, 3, composed.view);
    VkDescriptorBufferInfo sbi = {staging, 0, VK_WHOLE_SIZE};
    VkWriteDescriptorSet sw = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = present.set,
        .dstBinding = 2, .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        .pBufferInfo = &sbi};
    vkUpdateDescriptorSets(device, 1, &sw, 0, NULL);

    ps5fsr4_context_desc cd = {sizeof(cd), physical, device, RENDER_W, RENDER_H, OUTPUT_W, OUTPUT_H,
                               PS5FSR4_FLAG_HIGH_DYNAMIC_RANGE | PS5FSR4_FLAG_AUTO_EXPOSURE, NULL, pipeline_cache};
    ps5fsr4_context *context;
    ps5fsr4_result fr = ps5fsr4_context_create(&cd, &context);
    report("FSR4_DEMO_CONTEXT result=%d create_ms=%.1f cache_in=%zu\n", (int)fr, now_ms() - t0, cache_bytes);
    if (fr) return 1;
    if (!cache_bytes) {  /* keep a cache for the next launch */
        size_t bytes = 0;
        void *blob = NULL;
        char path[256];
        snprintf(path, sizeof(path), "%s/fsr4-demo-pipeline-cache.bin", output_root());
        if (vkGetPipelineCacheData(device, pipeline_cache, &bytes, NULL) == VK_SUCCESS && (blob = malloc(bytes)) &&
            vkGetPipelineCacheData(device, pipeline_cache, &bytes, blob) == VK_SUCCESS) {
            FILE *f = fopen(path, "wb");
            if (f) { fwrite(blob, 1, bytes, f); fclose(f); }
        }
        free(blob);
    }

    VkCommandPoolCreateInfo cpi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                   .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    VkCommandPool command_pool;
    CHECK(vkCreateCommandPool(device, &cpi, NULL, &command_pool));
    VkCommandBufferAllocateInfo cai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = command_pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    CHECK(vkAllocateCommandBuffers(device, &cai, &cmd));
    VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    CHECK(vkCreateFence(device, &fi, NULL, &fence));

    int32_t pad = -1, user = -1;
    sceUserServiceInitialize(NULL);
    if (sceUserServiceGetInitialUser(&user) >= 0 && scePadInit() >= 0)
        pad = scePadOpen(user, 0, 0, NULL);
    report("FSR4_DEMO_READY render=%dx%d output=%dx%d pad=%d\n", RENDER_W, RENDER_H, OUTPUT_W, OUTPUT_H, pad);

    const uint32_t phases = ps5fsr4_jitter_phase_count(RENDER_W, OUTPUT_W);
    float camera[4] = {0.6f, 0.35f, 9.0f, 0.0f}, previous[4];
    int mode = 1, automatic = 1, reset = 1;
    uint32_t last_buttons = 0;
    double start = now_ms(), idle_since = start, last_mode = start;
    double sum_scene = 0, sum_fsr = 0, sum_present = 0, sum_frame = 0, last_fsr = 0, last_frame = 0;
    for (uint32_t frame = 0;; ++frame) {
        double frame_start = now_ms();
        memcpy(previous, camera, sizeof(camera));
        camera[3] = (float)((frame_start - start) / 1000.0);
        if (frame == 0) previous[3] = camera[3];
        uint8_t state[128] __attribute__((aligned(16)));
        uint32_t buttons = 0;
        float lx = 0, ly = 0, ry = 0;
        if (pad >= 0 && scePadReadState(pad, state) >= 0 && state[76]) {
            memcpy(&buttons, state, sizeof(buttons));
            if (buttons & 0x80000000u) buttons = 0;  /* intercepted by the system */
            lx = stick(state[4]); ly = stick(state[5]); ry = stick(state[7]);
        }
        const uint32_t pressed = buttons & ~last_buttons;
        last_buttons = buttons;
        if (buttons || lx || ly || ry) { idle_since = frame_start; automatic = automatic && !(lx || ly); }
        if (pressed & PAD_CROSS) { mode = (mode + 1) % 3; last_mode = frame_start; }
        if (pressed & PAD_SQUARE) automatic = !automatic;
        if (pressed & PAD_TRIANGLE) reset = 1;
        if (!automatic && frame_start - idle_since > 30000.0) automatic = 1;
        if (automatic) {
            camera[0] += 0.004f;
            if (frame_start - last_mode > 8000.0 && frame_start - idle_since > 8000.0) {
                mode = (mode + 1) % 3;
                last_mode = frame_start;
            }
        }
        camera[0] += lx * 0.03f;
        camera[1] = fminf(1.3f, fmaxf(0.05f, camera[1] - ly * 0.02f));
        camera[2] = fminf(30.0f, fmaxf(3.0f, camera[2] * (1.0f + ry * 0.02f)));

        float jx, jy;
        ps5fsr4_jitter_offset(frame, phases, &jx, &jy);
        struct { float camera[4], previous[4], frame[4]; } scene_params = {
            {camera[0], camera[1], camera[2], camera[3]}, {previous[0], previous[1], previous[2], previous[3]},
            {jx, jy, (float)RENDER_W, (float)RENDER_H}};

        double scene_ms, fsr_ms, present_ms;
        if (begin()) return 1;
        if (frame == 0) {
            const VkImage images[5] = {color.image, depth.image, motion.image, upscaled.image, composed.image};
            for (int i = 0; i < 5; ++i)
                image_barrier(cmd, images[i], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0,
                              VK_ACCESS_SHADER_WRITE_BIT, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                              VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        } else {  /* the previous frame's draw left the composed frame read-only */
            image_barrier(cmd, composed.image, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
                          VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_SHADER_WRITE_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                          VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        }
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, scene.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, scene.layout, 0, 1, &scene.set, 0, NULL);
        vkCmdPushConstants(cmd, scene.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(scene_params), &scene_params);
        vkCmdDispatch(cmd, (RENDER_W + 7) / 8, (RENDER_H + 7) / 8, 1);
        if (submit(&scene_ms)) return 1;

        if (begin()) return 1;
        ps5fsr4_dispatch_desc dd = {.struct_size = sizeof(dd), .command_buffer = cmd,
            .color = color.view, .depth = depth.view, .motion_vectors = motion.view, .output = upscaled.view,
            .color_layout = VK_IMAGE_LAYOUT_GENERAL, .depth_layout = VK_IMAGE_LAYOUT_GENERAL,
            .motion_vectors_layout = VK_IMAGE_LAYOUT_GENERAL, .render_width = RENDER_W, .render_height = RENDER_H,
            .jitter_x = jx, .jitter_y = jy, .motion_vector_scale_x = RENDER_W, .motion_vector_scale_y = RENDER_H,
            .pre_exposure = 1.0f, .frame_time_delta_ms = (float)last_frame, .camera_near = 0.1f,
            .camera_far = 200.0f, .camera_fov_vertical = 1.0471976f, .reset = reset};
        memory_barrier(cmd, VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT,
                       VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        if (frame == 200) {  /* one profiled frame: each pass in its own submission */
            fsr_ms = 0;
            for (uint32_t pass = 0; pass < ps5fsr4_pass_count(); ++pass) {
                double pass_ms;
                if (pass && begin()) return 1;
                fr = ps5fsr4_dispatch_passes(context, &dd, pass, 1);
                if (fr) { report("FSR4_DEMO_ERROR dispatch=%d\n", (int)fr); return 1; }
                if (submit(&pass_ms)) return 1;
                fsr_ms += pass_ms;
                report("FSR4_DEMO_PASS index=%u ms=%.3f\n", pass, pass_ms);
            }
        } else {
            fr = ps5fsr4_dispatch(context, &dd);
            if (fr) { report("FSR4_DEMO_ERROR dispatch=%d\n", (int)fr); return 1; }
            if (submit(&fsr_ms)) return 1;
        }
        reset = 0;

        const uint32_t slot = frame & 1;
        const int screenshot = frame >= 300 && frame <= 302;
        struct { float view[4], status[4]; } present_params = {
            {(float)mode, OUTPUT_W * 0.5f, (float)RENDER_W, (float)RENDER_H},
            {(float)last_fsr, (float)last_frame, (float)screenshot, 0}};
        if (begin()) return 1;
        memory_barrier(cmd, VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT,
                       VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, present.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, present.layout, 0, 1, &present.set, 0, NULL);
        vkCmdPushConstants(cmd, present.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(present_params),
                           &present_params);
        vkCmdDispatch(cmd, OUTPUT_W / 8, (OUTPUT_H + 7) / 8, 1);
        image_barrier(cmd, composed.image, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                      VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                      VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
        VkRenderPassBeginInfo rb = {.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO, .renderPass = blit.pass,
            .framebuffer = blit.framebuffers[slot], .renderArea = {{0, 0}, {OUTPUT_W, OUTPUT_H}}};
        vkCmdBeginRenderPass(cmd, &rb, VK_SUBPASS_CONTENTS_INLINE);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, blit.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, blit.layout, 0, 1, &blit.set, 0, NULL);
        vkCmdDraw(cmd, 3, 1, 0, 0);
        vkCmdEndRenderPass(cmd);
        if (submit(&present_ms)) return 1;
        VkResult pr = ps5vkPresentFrame(surface, slot, (uint64_t)frame + 1);
        if (pr != VK_SUCCESS) { report("FSR4_DEMO_ERROR present=%d\n", (int)pr); return 1; }

        last_fsr = fsr_ms;
        last_frame = now_ms() - frame_start;
        sum_scene += scene_ms; sum_fsr += fsr_ms; sum_present += present_ms; sum_frame += last_frame;
        if ((frame + 1) % 120 == 0) {
            report("FSR4_DEMO_STATS frames=%u mode=%d auto=%d scene_ms=%.2f fsr4_ms=%.2f present_ms=%.2f frame_ms=%.2f\n",
                   frame + 1, mode, automatic, sum_scene / 120, sum_fsr / 120, sum_present / 120, sum_frame / 120);
            sum_scene = sum_fsr = sum_present = sum_frame = 0;
        }
        /* Native screenshots of the three view modes (frames 300..302) for validation. */
        if (frame == 299) mode = 0;
        if (screenshot) {
            char path[256];
            snprintf(path, sizeof(path), "%s/fsr4-demo-mode%d.bgra", output_root(), mode);
            VkMappedMemoryRange range = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, .memory = staging_memory,
                                         .size = VK_WHOLE_SIZE};
            CHECK(vkInvalidateMappedMemoryRanges(device, 1, &range));
            FILE *f = fopen(path, "wb");
            if (f) { fwrite(staging_mapped, 1, (size_t)OUTPUT_W * OUTPUT_H * 4, f); fclose(f); }
            report("FSR4_DEMO_SCREENSHOT frame=%u mode=%d\n", frame, mode);
            mode = (mode + 1) % 3;
            last_mode = now_ms();
        }
    }
}

int main(void)
{
    char path[256];
    snprintf(path, sizeof(path), "%s/fsr4-demo-log.txt", output_root());
    log_file = fopen(path, "w");
    int result = fsr4_native_heap_init();
    report("FSR4_DEMO_BEGIN heap=%d\n", result);
    if (!result) result = run();
    report("FSR4_DEMO_END result=%s\n", result ? "FAIL" : "EXIT");
    if (log_file) fclose(log_file);
    for (;;) usleep(100000);
}
