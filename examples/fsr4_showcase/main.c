/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * PS5 FSR4 Showcase (PPSA99011): a city at dusk, ray cast at a fraction of the
 * output size and upscaled by ps5_fsr4. The settings menu chooses what is
 * shown and how:
 *
 *   - the six render and output sizes of the README's performance table, or
 *     any quality mode (Native AA to Ultra Performance) at 1920x1080,
 *     2560x1440 or 3840x2160;
 *   - FSR 4 alone or split against a bilinear upscale of the same frame or a
 *     native render without anti-aliasing, with a lens on the output's pixels;
 *   - RCAS sharpening, dynamic resolution, a cinematic or a free camera;
 *   - a benchmark that times the upscaler on this console.
 *
 * A guided tour runs until a button is pressed and resumes after a minute
 * without input.
 *   Options      settings                  Cross        next comparison
 *   Square       lens                      Triangle     cinematic or free camera
 *   L1 / R1      previous / next scenario  Touchpad     on-screen display
 *   Left stick   move (free camera)        Right stick  look (free camera)
 *   L2 / R2      down / up (free camera)   D-pad        move the divider or the lens
 */
#ifdef SHOWCASE_HOST
#include <vulkan/vulkan.h>
#else
#include <ps5vk/ps5vk.h>
#include <ps5vk/ps5vk_present.h>
#endif
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
#include "fsr4_showcase_shaders.h"
#include "hud.h"

#ifdef SHOWCASE_HOST  /* an off-screen build for desktop Vulkan: runs the scripted walk and saves its frames */
static int sceKernelDebugOutText(int level, const char *text) { (void)level; return fputs(text, stdout); }
static int fsr4_native_heap_init(void) { return 0; }
#define ASSET_ROOT "assets"
#else
extern int sceKernelDebugOutText(int level, const char *text);
extern int sceUserServiceInitialize(void *params);
extern int sceUserServiceGetInitialUser(int32_t *user);
extern int scePadInit(void);
extern int scePadOpen(int32_t user, int32_t type, int32_t index, const void *params);
extern int scePadReadState(int32_t handle, void *data);
extern int sceSystemServiceLoadExec(const char *path, char *const argv[]);
extern int fsr4_native_heap_init(void);
#define ASSET_ROOT "/app0/assets"
#endif

#ifndef SHOWCASE_SELFTEST
#define SHOWCASE_SELFTEST 0  /* 1: replace the pad with a scripted walk that logs timings and saves frames */
#endif
#ifndef SHOWCASE_VERSION
#define SHOWCASE_VERSION "dev"
#endif

enum { DISPLAY_W = HUD_W, DISPLAY_H = HUD_H, LENS_RADIUS = 200, LENS_X = DISPLAY_W / 2, LENS_Y = DISPLAY_H / 2 };
static const double PI = 3.14159265358979323846;
enum { PAD_L3 = 0x2, PAD_R3 = 0x4, PAD_OPTIONS = 0x8, PAD_UP = 0x10, PAD_RIGHT = 0x20, PAD_DOWN = 0x40,
       PAD_LEFT = 0x80, PAD_L1 = 0x400, PAD_R1 = 0x800, PAD_TRIANGLE = 0x1000, PAD_CIRCLE = 0x2000,
       PAD_CROSS = 0x4000, PAD_SQUARE = 0x8000, PAD_TOUCH = 0x100000 };

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
    report("FSR4_SHOWCASE_ERROR %s = %d\n", #call, (int)rc_); return 1; } } while (0)

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static VkPhysicalDevice physical;
static VkDevice device;
static VkQueue queue;
static VkCommandBuffer cmd;
static VkFence fence;
static VkPhysicalDeviceMemoryProperties memory_properties;
static VkPipelineCache pipeline_cache;
static int wave64;

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

static void destroy_image(struct image *img)
{
    if (img->view) vkDestroyImageView(device, img->view, NULL);
    if (img->image) vkDestroyImage(device, img->image, NULL);
    if (img->memory) vkFreeMemory(device, img->memory, NULL);
    *img = (struct image){0};
}

/* A host-visible buffer, mapped for the application's lifetime. It is non-coherent, so
 * writes need vkFlushMappedMemoryRanges and reads vkInvalidateMappedMemoryRanges. */
struct buffer { VkBuffer buffer; VkDeviceMemory memory; void *mapped; };

static int create_buffer(struct buffer *b, VkDeviceSize bytes)
{
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = bytes,
                             .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT};
    CHECK(vkCreateBuffer(device, &bi, NULL, &b->buffer));
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(device, b->buffer, &req);
    VkMemoryAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
        .memoryTypeIndex = (uint32_t)memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)};
    CHECK(vkAllocateMemory(device, &ai, NULL, &b->memory));
    CHECK(vkBindBufferMemory(device, b->buffer, b->memory, 0));
    CHECK(vkMapMemory(device, b->memory, 0, VK_WHOLE_SIZE, 0, &b->mapped));
    return 0;
}

struct compute {
    VkDescriptorSetLayout set_layout;
    VkPipelineLayout layout;
    VkPipeline pipeline;
    VkDescriptorSet set;
};

static int create_compute(struct compute *c, const uint32_t *code, size_t bytes, const VkDescriptorType *types,
                          uint32_t count, uint32_t push_bytes, VkDescriptorPool pool)
{
    VkDescriptorSetLayoutBinding bindings[8];
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
    CHECK(vkCreateComputePipelines(device, pipeline_cache, 1, &ci, NULL, &c->pipeline));
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

static void bind_buffer(VkDescriptorSet set, uint32_t binding, VkBuffer buffer)
{
    VkDescriptorBufferInfo info = {buffer, 0, VK_WHOLE_SIZE};
    VkWriteDescriptorSet w = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = binding,
        .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &info};
    vkUpdateDescriptorSets(device, 1, &w, 0, NULL);
}

static void memory_barrier(VkAccessFlags src, VkAccessFlags dst, VkPipelineStageFlags src_stage,
                           VkPipelineStageFlags dst_stage)
{
    VkMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = src, .dstAccessMask = dst};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 1, &b, 0, NULL, 0, NULL);
}

static void image_barrier(VkImage image, VkImageLayout from, VkImageLayout to, VkAccessFlags src, VkAccessFlags dst,
                          VkPipelineStageFlags src_stage, VkPipelineStageFlags dst_stage)
{
    VkImageMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, .srcAccessMask = src,
        .dstAccessMask = dst, .oldLayout = from, .newLayout = to,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .image = image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
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
        report("FSR4_SHOWCASE_ERROR fence=%d\n", (int)wait);
        for (;;) usleep(100000);  /* never release resources with unknown completion */
    }
    if (elapsed) *elapsed = now_ms() - t0;
    CHECK(vkResetFences(device, 1, &fence));
    CHECK(vkResetCommandBuffer(cmd, 0));
    return 0;
}

#ifndef SHOWCASE_HOST
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

static int create_blit(struct blit *b, const VkImage display[2], VkImageView frame, VkDescriptorPool pool)
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
            .attachmentCount = 1, .pAttachments = &b->views[i], .width = DISPLAY_W, .height = DISPLAY_H, .layers = 1};
        CHECK(vkCreateFramebuffer(device, &fi, NULL, &b->framebuffers[i]));
    }

    VkShaderModule vs, fs;
    if (create_shader(fsr4_showcase_blit_vert_spv, sizeof(fsr4_showcase_blit_vert_spv), &vs) ||
        create_shader(fsr4_showcase_blit_frag_spv, sizeof(fsr4_showcase_blit_frag_spv), &fs))
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
    VkViewport viewport = {0.0f, 0.0f, (float)DISPLAY_W, (float)DISPLAY_H, 0.0f, 1.0f};
    VkRect2D scissor = {{0, 0}, {DISPLAY_W, DISPLAY_H}};
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
    CHECK(vkCreateGraphicsPipelines(device, pipeline_cache, 1, &gi, NULL, &b->pipeline));
    vkDestroyShaderModule(device, vs, NULL);
    vkDestroyShaderModule(device, fs, NULL);
    return 0;
}

#endif

static const char *output_root(void)
{
    static const char *chosen;
#ifdef SHOWCASE_HOST
    static const char *const candidates[] = {"frames", ".", "."};
#else
    static const char *const candidates[] = {"/data/fsr4-results", "/app0/results", "/download0"};
#endif
    for (unsigned i = 0; !chosen && i < sizeof(candidates) / sizeof(candidates[0]); ++i) {
        char probe[128];
        mkdir(candidates[i], 0777);
        snprintf(probe, sizeof(probe), "%s/.writable", candidates[i]);
        FILE *f = fopen(probe, "wb");
        if (f && fputc('1', f) != EOF && !fclose(f)) chosen = candidates[i];
        else if (f) fclose(f);
    }
    return chosen ? chosen : candidates[2];
}

static void *read_asset(const char *name, size_t *bytes)
{
    char path[256];
    snprintf(path, sizeof(path), ASSET_ROOT "/%s", name);
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


/* ---- What can be chosen. */
static const struct { uint32_t w, h; const char *name; int lens_zoom; } OUTPUTS[] = {
    {1920, 1080, "1080p", 4}, {2560, 1440, "1440p", 3}, {3840, 2160, "4K", 2}};
enum { OUT_1080P, OUT_1440P, OUT_4K, OUTPUT_COUNT };
static const struct { const char *name, *ratio; float scale; } QUALITIES[] = {
    {"Native AA", "1×", 1.0f}, {"Quality", "1.5×", 1.5f}, {"Balanced", "1.7×", 1.7f},
    {"Performance", "2×", 2.0f}, {"Ultra Performance", "3×", 3.0f}};
enum { Q_NATIVE, Q_QUALITY, Q_BALANCED, Q_PERFORMANCE, Q_ULTRA, QUALITY_COUNT };
enum { SRC_FSR4, SRC_BILINEAR, SRC_NATIVE };
static const char *const SOURCES[] = {"FSR 4", "Bilinear", "Native, no AA"};
static const struct { int left, right; const char *name; } VIEWS[] = {
    {SRC_FSR4, SRC_FSR4, "FSR 4"}, {SRC_FSR4, SRC_BILINEAR, "FSR 4 | bilinear"},
    {SRC_FSR4, SRC_NATIVE, "FSR 4 | native"}, {SRC_BILINEAR, SRC_BILINEAR, "Bilinear"},
    {SRC_NATIVE, SRC_NATIVE, "Native, no AA"}};
enum { V_FSR4, V_VS_BILINEAR, V_VS_NATIVE, V_BILINEAR, V_NATIVE, VIEW_COUNT };
static const int LENS_ZOOMS[] = {0, 2, 3, 4, 6, 8};
enum { LENS_ZOOM_COUNT = sizeof(LENS_ZOOMS) / sizeof(LENS_ZOOMS[0]) };

/* 4K goes up to Quality (2560x1440): Native AA would render the scene itself at 4K. */
static int quality_allowed(int output, int quality) { return !(output == OUT_4K && quality == Q_NATIVE); }

static void render_size(int output, int quality, uint32_t *w, uint32_t *h)
{
    *w = (uint32_t)((float)OUTPUTS[output].w / QUALITIES[quality].scale);  /* FidelityFX truncates */
    *h = (uint32_t)((float)OUTPUTS[output].h / QUALITIES[quality].scale);
}

/* The six cases of the README's performance table, with the times measured there. */
static const struct scenario { int output, quality; double readme_ms; } SCENARIOS[] = {
    {OUT_1080P, Q_ULTRA, 2.92}, {OUT_1080P, Q_QUALITY, 2.95}, {OUT_1440P, Q_PERFORMANCE, 4.92},
    {OUT_1440P, Q_QUALITY, 4.95}, {OUT_4K, Q_PERFORMANCE, 10.86}, {OUT_4K, Q_ULTRA, 10.94}};
enum { SCENARIO_COUNT = sizeof(SCENARIOS) / sizeof(SCENARIOS[0]) };

static int scenario_of(int output, int quality)
{
    for (int i = 0; i < SCENARIO_COUNT; ++i)
        if (SCENARIOS[i].output == output && SCENARIOS[i].quality == quality) return i;
    return -1;
}

static void sizes_text(char *text, size_t size, int output, int quality)
{
    uint32_t w, h;
    render_size(output, quality, &w, &h);
    snprintf(text, size, "%u×%u → %u×%u", w, h, OUTPUTS[output].w, OUTPUTS[output].h);
}

/* ---- The camera: scripted shots through the city, or free flight. */
struct pose { float x, y, z, yaw, pitch, tan_half; };
static const struct shot { float from[3], to[3], look_from[3], look_to[3], fov; double seconds; } SHOTS[] = {
    {{6, 4.4f, -176}, {6, 4.8f, -72}, {6, 6, -100}, {24, 34, 36}, 62, 18},            /* along the avenue, over the trams */
    {{15, 3, 15}, {13, 86, 13}, {36, 16, 36}, {36, 92, 36}, 64, 18},                  /* up the lattice tower */
    {{-300, 215, -330}, {-150, 185, -190}, {20, 40, 20}, {36, 60, 36}, 58, 20},       /* over the roofs */
    {{24, 2.5f, -47}, {19, 7.6f, -24}, {18, 8.5f, -6}, {18, 8.5f, -6}, 40, 14},        /* the reading chart */
    {{24, 2.5f, -47}, {29.5f, 8, -21}, {30, 8.5f, -6}, {30, 8.5f, -6}, 44, 14},        /* the resolution chart */
    {{-58, 2.4f, 3}, {-106, 6, 5}, {-84, 22, 36}, {-84, 27, 36}, 70, 18},             /* the big wheel */
    {{44, 1.9f, -51.5f}, {-36, 2.8f, -52.5f}, {-200, 6, -54}, {-200, 9, -54}, 60, 18},/* a street with traffic */
    {{6, 4, -142}, {4.5f, 7.2f, -119}, {-6, 8.5f, -102}, {-6, 8.5f, -102}, 55, 14},   /* the title sign */
};
enum { SHOT_COUNT = sizeof(SHOTS) / sizeof(SHOTS[0]) };

static struct pose shot_pose(int index, double t)
{
    const struct shot *s = &SHOTS[index];
    const float e = (float)fmin(1.0, fmax(0.0, t));
    float at[3], look[3];
    for (int i = 0; i < 3; ++i) {
        at[i] = s->from[i] + (s->to[i] - s->from[i]) * e;
        look[i] = s->look_from[i] + (s->look_to[i] - s->look_from[i]) * e - at[i];
    }
    return (struct pose){at[0], at[1], at[2], atan2f(look[2], look[0]), atan2f(look[1], hypotf(look[0], look[2])),
                         tanf(s->fov * (float)PI / 360.0f)};
}

/* ---- The guided tour. */
static const struct chapter {
    const char *title, *caption;
    int output, quality, view, lens, drs, sharpen, shot;
    double seconds;
} TOUR[] = {
    {"FSR 4 on PlayStation 5", "A city at dusk, drawn at 1280×720 and upscaled to 1920×1080 by a neural network",
     OUT_1080P, Q_QUALITY, V_FSR4, 0, 0, 0, 0, 16},
    {"Against a bilinear upscale", "Left: FSR 4. Right: the same 1280×720 frame, scaled bilinearly",
     OUT_1080P, Q_QUALITY, V_VS_BILINEAR, 1, 0, 0, 7, 12},
    {"One pixel in nine", "640×360 → 1920×1080 with the Ultra Performance model, on a lattice of thin steel",
     OUT_1080P, Q_ULTRA, V_VS_BILINEAR, 1, 0, 0, 1, 14},
    {"Small print", "Text that the render resolution cannot hold becomes readable",
     OUT_1080P, Q_PERFORMANCE, V_VS_BILINEAR, 1, 0, 0, 3, 12},
    {"A resolution chart", "Spokes and line pairs finer than a render pixel",
     OUT_1080P, Q_PERFORMANCE, V_VS_BILINEAR, 1, 0, 0, 4, 12},
    {"Things that move", "The wheel, its lights and its gondolas keep their detail while they turn",
     OUT_1080P, Q_QUALITY, V_FSR4, 0, 0, 0, 5, 14},
    {"Against native rendering", "FSR 4 from 960×540 against a native 1920×1080 frame without anti-aliasing",
     OUT_1080P, Q_PERFORMANCE, V_VS_NATIVE, 1, 0, 0, 6, 14},
    {"1440p output", "1280×720 → 2560×1440; the lens shows real 1440p pixels",
     OUT_1440P, Q_PERFORMANCE, V_FSR4, 1, 0, 0, 2, 12},
    {"4K output", "1920×1080 → 3840×2160; the lens shows real 4K pixels",
     OUT_4K, Q_PERFORMANCE, V_VS_BILINEAR, 1, 0, 0, 0, 12},
    {"4K from 720p", "1280×720 → 3840×2160 with the Ultra Performance model",
     OUT_4K, Q_ULTRA, V_VS_BILINEAR, 1, 0, 0, 1, 12},
    {"Dynamic resolution", "The render size changes every frame; FSR 4 keeps its history",
     OUT_1080P, Q_QUALITY, V_FSR4, 0, 1, 0, 2, 12},
    {"RCAS sharpening", "Contrast-adaptive sharpening after upscaling, strength 0 to 1",
     OUT_1080P, Q_QUALITY, V_FSR4, 1, 0, 1, 7, 10},
};
enum { CHAPTERS = sizeof(TOUR) / sizeof(TOUR[0]), CHAPTER_SHARPENING = CHAPTERS - 1 };

/* ---- The pipelines and the per-output resources: render targets up to the largest
 * render size of the output, the FSR4 output, a native render at output size and the context. */
static struct compute scene, native_scene, compose;
static struct target {
    int output, drs;
    uint32_t max_w, max_h;
    struct image color, depth, motion, native, upscaled;
    ps5fsr4_context *context;
} target = {.output = -1};

static int setup_target(int output, int drs)
{
    double t0 = now_ms();
    if (target.context) ps5fsr4_context_destroy(target.context);
    target.context = NULL;
    struct image *images[5] = {&target.color, &target.depth, &target.motion, &target.native, &target.upscaled};
    for (int i = 0; i < 5; ++i) destroy_image(images[i]);
    target.output = output;
    target.drs = drs;
    target.max_w = target.max_h = 0;
    for (int q = 0; q < QUALITY_COUNT; ++q)
        if (quality_allowed(output, q)) {
            uint32_t w, h;
            render_size(output, q, &w, &h);
            if (w > target.max_w) target.max_w = w;
            if (h > target.max_h) target.max_h = h;
        }
    const uint32_t ow = OUTPUTS[output].w, oh = OUTPUTS[output].h;
    if (create_image(&target.color, VK_FORMAT_R16G16B16A16_SFLOAT, target.max_w, target.max_h, 1) ||
        create_image(&target.depth, VK_FORMAT_R32_SFLOAT, target.max_w, target.max_h, 1) ||
        create_image(&target.motion, VK_FORMAT_R32G32B32A32_SFLOAT, target.max_w, target.max_h, 1) ||
        create_image(&target.native, VK_FORMAT_R16G16B16A16_SFLOAT, ow, oh, 0) ||
        create_image(&target.upscaled, VK_FORMAT_R32G32B32A32_SFLOAT, ow, oh, 0))
        return 1;
    bind_image(scene.set, 0, target.color.view);
    bind_image(scene.set, 2, target.depth.view);
    bind_image(scene.set, 3, target.motion.view);
    bind_image(native_scene.set, 0, target.native.view);
    bind_image(compose.set, 0, target.upscaled.view);
    bind_image(compose.set, 1, target.color.view);
    bind_image(compose.set, 2, target.native.view);
    if (begin()) return 1;
    for (int i = 0; i < 5; ++i)
        image_barrier(images[i]->image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0,
                      VK_ACCESS_SHADER_WRITE_BIT, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                      VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    if (submit(NULL)) return 1;
    ps5fsr4_context_desc cd = {sizeof(cd), physical, device, target.max_w, target.max_h, ow, oh,
                               PS5FSR4_FLAG_HIGH_DYNAMIC_RANGE | PS5FSR4_FLAG_AUTO_EXPOSURE |
                               (wave64 ? PS5FSR4_FLAG_SUBGROUP_SIZE_CONTROL : 0u) |
                               (drs ? PS5FSR4_FLAG_DYNAMIC_RESOLUTION : 0u),
                               NULL, pipeline_cache};
    ps5fsr4_result fr = ps5fsr4_context_create(&cd, &target.context);
    report("FSR4_SHOWCASE_OUTPUT output=%ux%u max_render=%ux%u dynamic=%d result=%d setup_ms=%.1f\n", ow, oh,
           target.max_w, target.max_h, drs, (int)fr, now_ms() - t0);
    return fr != PS5FSR4_OK;
}

/* ---- What the viewer sees, the menu and the benchmark. */
enum { CAMERA_CINEMATIC, CAMERA_FREE };
enum { ROW_SCENARIO, ROW_OUTPUT, ROW_QUALITY, ROW_VIEW, ROW_LENS, ROW_SHARPEN, ROW_DRS, ROW_CAMERA, ROW_PAUSE,
       ROW_HUD, ROW_BENCH, ROW_BENCH_ALL, ROW_TOUR, ROW_COUNT };
static const char *const ROW_NAMES[ROW_COUNT] = {"Scenario", "Output size", "Quality mode", "Compare", "Lens",
    "RCAS sharpening", "Dynamic resolution", "Camera", "Animation", "On-screen display", "Benchmark this setting",
    "Benchmark the six scenarios", "Guided tour"};

static struct state {
    int output, quality, view, lens /* index into LENS_ZOOMS */, drs, sharpen /* tenths, 0: off */, hud /* 0 to 2 */;
    int camera, paused, menu, row, tour, chapter, shot;
    float split, lens_x, lens_y;
    double chapter_start, shot_start;
} state = {.output = OUT_1080P, .quality = Q_QUALITY, .hud = 2, .tour = 1, .split = DISPLAY_W / 2,
           .lens_x = LENS_X, .lens_y = LENS_Y};

static struct stats { double fsr4_ms, scene_ms, compose_ms, frame_ms; uint32_t render_w, render_h; } stats;

enum { BENCH_IDLE, BENCH_RUNNING, BENCH_RESULTS };
#ifdef SHOWCASE_HOST
enum { BENCH_WARMUP = 1, BENCH_FRAMES = 2, BENCH_BATCH = 1 };
#else
enum { BENCH_WARMUP = 30, BENCH_FRAMES = 300, BENCH_BATCH = 15 };
#endif
static struct bench {
    int phase, all, index, frame;
    int output, quality;                    /* what a single run measures */
    double sum, single_ms, ms[SCENARIO_COUNT];
    int measured[SCENARIO_COUNT];
    struct state saved;
} bench;

static void bench_start(int all)
{
    bench.saved = state;
    bench.phase = BENCH_RUNNING;
    bench.all = all;
    bench.index = bench.frame = 0;
    bench.sum = 0;
    bench.output = state.output;
    bench.quality = state.quality;
    if (all) memset(bench.measured, 0, sizeof(bench.measured));
    state.menu = state.tour = 0;
}

static void row_value(int row, char *text, size_t size)
{
    const int scenario = state.drs ? -1 : scenario_of(state.output, state.quality);
    char sizes[64];
    sizes_text(sizes, sizeof(sizes), state.output, state.quality);
    switch (row) {
    case ROW_SCENARIO:
        if (scenario < 0) snprintf(text, size, "Custom");
        else snprintf(text, size, "%d of %d · %s", scenario + 1, (int)SCENARIO_COUNT, sizes);
        break;
    case ROW_OUTPUT: snprintf(text, size, "%u×%u", OUTPUTS[state.output].w, OUTPUTS[state.output].h); break;
    case ROW_QUALITY: snprintf(text, size, "%s %s", QUALITIES[state.quality].name, QUALITIES[state.quality].ratio); break;
    case ROW_VIEW: snprintf(text, size, "%s", VIEWS[state.view].name); break;
    case ROW_LENS:
        if (state.lens) snprintf(text, size, "%d×", LENS_ZOOMS[state.lens]);
        else snprintf(text, size, "Off");
        break;
    case ROW_SHARPEN:
        if (state.sharpen) snprintf(text, size, "%.1f", state.sharpen / 10.0);
        else snprintf(text, size, "Off");
        break;
    case ROW_DRS: snprintf(text, size, "%s", state.drs ? "On" : "Off"); break;
    case ROW_CAMERA: snprintf(text, size, "%s", state.camera == CAMERA_FREE ? "Free flight" : "Cinematic"); break;
    case ROW_PAUSE: snprintf(text, size, "%s", state.paused ? "Paused" : "Running"); break;
    case ROW_HUD: snprintf(text, size, "%s", state.hud == 2 ? "Full" : state.hud == 1 ? "Compact" : "Off"); break;
    case ROW_TOUR: snprintf(text, size, "%d chapters", (int)CHAPTERS); break;
    default: snprintf(text, size, "✕"); break;
    }
}

/* A step of -1 or +1 changes the row's value; 0 is Cross. Returns 1 when the tour should start. */
static int row_change(int row, int step)
{
    const int by = step ? step : 1;
    switch (row) {
    case ROW_SCENARIO: {
        int index = state.drs ? -1 : scenario_of(state.output, state.quality);
        index = index < 0 ? (by > 0 ? 0 : SCENARIO_COUNT - 1) : (index + by + SCENARIO_COUNT) % SCENARIO_COUNT;
        state.output = SCENARIOS[index].output;
        state.quality = SCENARIOS[index].quality;
        state.drs = 0;
        break;
    }
    case ROW_OUTPUT:
        state.output = (state.output + by + OUTPUT_COUNT) % OUTPUT_COUNT;
        if (!quality_allowed(state.output, state.quality)) state.quality = Q_QUALITY;
        break;
    case ROW_QUALITY:
        do state.quality = (state.quality + by + QUALITY_COUNT) % QUALITY_COUNT;
        while (!quality_allowed(state.output, state.quality));
        break;
    case ROW_VIEW: state.view = (state.view + by + VIEW_COUNT) % VIEW_COUNT; break;
    case ROW_LENS: state.lens = (state.lens + by + LENS_ZOOM_COUNT) % LENS_ZOOM_COUNT; break;
    case ROW_SHARPEN: state.sharpen = (state.sharpen + by + 11) % 11; break;
    case ROW_DRS: state.drs = !state.drs; break;
    case ROW_CAMERA: state.camera = !state.camera; break;
    case ROW_PAUSE: state.paused = !state.paused; break;
    case ROW_HUD: state.hud = (state.hud + by + 3) % 3; break;
    case ROW_BENCH: if (!step) bench_start(0); break;
    case ROW_BENCH_ALL: if (!step) bench_start(1); break;
    case ROW_TOUR: if (!step) return 1; break;
    default: break;
    }
    return 0;
}

/* Copies what changed in the HUD's damage rectangles into the overlay buffer and
 * flushes only that from the CPU caches: the whole 8 MB overlay takes milliseconds. */
static VkDeviceSize overlay_atom = 256;
static VkResult upload_hud(const uint32_t *hud, const struct buffer *overlay)
{
    enum { MAX_RANGES = 1024 };
    static VkMappedMemoryRange ranges[MAX_RANGES];
    struct hud_rect damage[128];
    const int rects = hud_damage(damage, 128);
    uint32_t *mapped = overlay->mapped;
    uint32_t count = 0;
    for (int r = 0; r < rects; ++r)
        for (int y = damage[r].y0; y < damage[r].y1; ++y) {
            const size_t first = (size_t)y * HUD_W + damage[r].x0, bytes = (size_t)(damage[r].x1 - damage[r].x0) * 4;
            if (!memcmp(mapped + first, hud + first, bytes)) continue;
            memcpy(mapped + first, hud + first, bytes);
            const VkDeviceSize begin = first * 4 / overlay_atom * overlay_atom;
            const VkDeviceSize end = (first * 4 + bytes + overlay_atom - 1) / overlay_atom * overlay_atom;
            if (count == MAX_RANGES) {
                VkResult result = vkFlushMappedMemoryRanges(device, count, ranges);
                if (result != VK_SUCCESS) return result;
                count = 0;
            }
            ranges[count++] = (VkMappedMemoryRange){.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
                                                    .memory = overlay->memory, .offset = begin, .size = end - begin};
        }
    return count ? vkFlushMappedMemoryRanges(device, count, ranges) : VK_SUCCESS;
}

static const uint32_t WHITE = HUD_RGBA(255, 255, 255, 255), GREY = HUD_RGBA(190, 198, 210, 255);
static const uint32_t ACCENT = HUD_RGBA(255, 132, 44, 255), PANEL = HUD_RGBA(12, 14, 20, 216);
static const uint32_t ROW_LIT = HUD_RGBA(255, 132, 44, 70), TRACK = HUD_RGBA(255, 255, 255, 40);

static void panel(uint32_t *overlay, int x, int y, int w, int h)
{
    hud_fill(overlay, x, y, w, h, 18, PANEL);
    hud_fill(overlay, x, y, 8, h, 4, ACCENT);
}

static void label(uint32_t *overlay, int cx, int y, enum hud_face face, const char *text)
{
    const int w = hud_text_width(face, text);
    hud_fill(overlay, cx - w / 2 - 14, y, w + 28, hud_line_height(face) + 10, 12, PANEL);
    hud_text(overlay, cx - w / 2, y + 5, face, text, WHITE);
}

static void timings_text(char *text, size_t size)
{
    snprintf(text, size, "FSR 4 %.2f ms · scene %.2f ms · %.0f fps", stats.fsr4_ms, stats.scene_ms,
             stats.frame_ms > 0 ? 1000.0 / stats.frame_ms : 0.0);
}

static void draw_status(uint32_t *overlay)
{
    enum { LINES = 6 };
    char text[LINES][160], badge[32] = "";
    const enum hud_face faces[LINES] = {HUD_TITLE, HUD_BODY, HUD_BODY, HUD_BODY, HUD_BODY, HUD_BODY};
    const uint32_t colors[LINES] = {WHITE, GREY, WHITE, WHITE, WHITE, ACCENT};
    const struct chapter *c = &TOUR[state.chapter];
    const int lines = state.hud == 2 || state.tour ? LINES : 3;
    snprintf(text[0], sizeof(text[0]), "%s", state.tour ? c->title : "PS5 FSR4 Showcase");
    snprintf(text[1], sizeof(text[1]), "%s", state.tour ? c->caption : "OPTIONS opens the settings");
    if (state.drs)
        snprintf(text[2], sizeof(text[2]), "Dynamic resolution · %u×%u → %u×%u", stats.render_w, stats.render_h,
                 OUTPUTS[state.output].w, OUTPUTS[state.output].h);
    else
        snprintf(text[2], sizeof(text[2]), "%s %s · %u×%u → %u×%u", QUALITIES[state.quality].name,
                 QUALITIES[state.quality].ratio, stats.render_w, stats.render_h, OUTPUTS[state.output].w,
                 OUTPUTS[state.output].h);
    if (state.lens)
        snprintf(text[3], sizeof(text[3]), "Compare: %s · lens %d×", VIEWS[state.view].name, LENS_ZOOMS[state.lens]);
    else
        snprintf(text[3], sizeof(text[3]), "Compare: %s", VIEWS[state.view].name);
    if (state.sharpen) snprintf(text[4], sizeof(text[4]), "RCAS sharpening %.1f", state.sharpen / 10.0);
    else snprintf(text[4], sizeof(text[4]), "RCAS sharpening off");
    timings_text(text[5], sizeof(text[5]));
    if (lines == 3) timings_text(text[1], sizeof(text[1]));
    if (state.tour) snprintf(badge, sizeof(badge), "%d / %d", state.chapter + 1, (int)CHAPTERS);
    int width = hud_text_width(HUD_TITLE, text[0]) + (badge[0] ? 40 + hud_text_width(HUD_SMALL, badge) : 0);
    int height = 36;
    for (int i = 0; i < lines; ++i) {
        const int w = hud_text_width(faces[i], text[i]);
        if (w > width) width = w;
        height += hud_line_height(faces[i]) + (i == 1 ? 12 : 0);
    }
    const int px = 36, py = 36, pw = width + 64;
    panel(overlay, px, py, pw, height);
    int y = py + 18;
    for (int i = 0; i < lines; ++i) {
        hud_text(overlay, px + 32, y, faces[i], text[i], lines == 3 && i == 1 ? ACCENT : colors[i]);
        y += hud_line_height(faces[i]) + (i == 1 ? 12 : 0);
    }
    if (badge[0])
        hud_text(overlay, px + pw - 24 - hud_text_width(HUD_SMALL, badge), py + 26, HUD_SMALL, badge, GREY);
}

static void draw_menu(uint32_t *overlay)
{
    const int px = 36, py = 36, pw = 820, row_h = hud_line_height(HUD_BODY) + 12;
    const int height = 30 + hud_line_height(HUD_TITLE) + 14 + ROW_COUNT * row_h + 20 + 2 * hud_line_height(HUD_SMALL) + 26;
    char text[160];
    panel(overlay, px, py, pw, height);
    hud_text(overlay, px + 32, py + 18, HUD_TITLE, "Settings", WHITE);
    hud_text(overlay, px + pw - 28 - hud_text_width(HUD_SMALL, "PS5 FSR4 Showcase " SHOWCASE_VERSION), py + 30,
             HUD_SMALL, "PS5 FSR4 Showcase " SHOWCASE_VERSION, GREY);
    int y = py + 18 + hud_line_height(HUD_TITLE) + 14;
    for (int row = 0; row < ROW_COUNT; ++row, y += row_h) {
        const int selected = row == state.row;
        const int action = row >= ROW_BENCH;
        if (row == ROW_BENCH) hud_fill(overlay, px + 28, y - 1, pw - 56, 2, 0, TRACK);
        if (selected) hud_fill(overlay, px + 18, y + 2, pw - 36, row_h - 4, 10, ROW_LIT);
        hud_text(overlay, px + 32, y + 6, HUD_BODY, ROW_NAMES[row], selected ? WHITE : GREY);
        char value[120];
        row_value(row, value, sizeof(value));
        if (action) snprintf(text, sizeof(text), "%s", selected ? "✕ start" : "");
        else if (selected) snprintf(text, sizeof(text), "‹  %s  ›", value);
        else snprintf(text, sizeof(text), "%s", value);
        hud_text(overlay, px + pw - 32 - hud_text_width(HUD_BODY, text), y + 6, HUD_BODY, text,
                 selected ? ACCENT : WHITE);
    }
    y += 14;
    timings_text(text, sizeof(text));
    hud_text(overlay, px + 32, y, HUD_SMALL, text, ACCENT);
    hud_text(overlay, px + 32, y + hud_line_height(HUD_SMALL) + 4, HUD_SMALL,
             "↑ ↓ select     ← → change     ✕ start     ○ or OPTIONS close", GREY);
}

static void bench_row(uint32_t *overlay, int x, int y, int output, int quality, double ms, double readme_ms)
{
    char text[96];
    sizes_text(text, sizeof(text), output, quality);
    hud_text(overlay, x, y, HUD_BODY, text, WHITE);
    hud_text(overlay, x + 420, y, HUD_BODY, QUALITIES[quality].name, GREY);
    if (ms > 0) {
        snprintf(text, sizeof(text), "%.2f ms", ms);
        hud_text(overlay, x + 820 - hud_text_width(HUD_BODY, text), y, HUD_BODY, text, ACCENT);
        snprintf(text, sizeof(text), "%.0f%%", ms / (1000.0 / 60.0) * 100.0);
        hud_text(overlay, x + 940 - hud_text_width(HUD_BODY, text), y, HUD_BODY, text, WHITE);
    } else {
        hud_text(overlay, x + 820 - hud_text_width(HUD_BODY, "…"), y, HUD_BODY, "…", GREY);
    }
    if (readme_ms > 0) {
        snprintf(text, sizeof(text), "%.2f ms", readme_ms);
        hud_text(overlay, x + 1110 - hud_text_width(HUD_BODY, text), y, HUD_BODY, text, GREY);
    }
}

static void draw_bench(uint32_t *overlay)
{
    const int rows = bench.all ? SCENARIO_COUNT : 1, row_h = hud_line_height(HUD_BODY) + 10;
    const int pw = 1210, ph = 34 + hud_line_height(HUD_TITLE) + 16 + hud_line_height(HUD_SMALL) + 12 + rows * row_h +
                               (bench.phase == BENCH_RUNNING ? 60 : 24) + 2 * hud_line_height(HUD_SMALL) + 24;
    const int px = (DISPLAY_W - pw) / 2, py = (DISPLAY_H - ph) / 2, x = px + 40;
    panel(overlay, px, py, pw, ph);
    hud_text(overlay, x, py + 20, HUD_TITLE, bench.phase == BENCH_RUNNING ? "Measuring FSR 4 on this console"
                                                                         : "FSR 4 on this console", WHITE);
    int y = py + 20 + hud_line_height(HUD_TITLE) + 16;
    hud_text(overlay, x, y, HUD_SMALL, "Render → output", GREY);
    hud_text(overlay, x + 420, y, HUD_SMALL, "Mode", GREY);
    hud_text(overlay, x + 820 - hud_text_width(HUD_SMALL, "Time per frame"), y, HUD_SMALL, "Time per frame", GREY);
    hud_text(overlay, x + 940 - hud_text_width(HUD_SMALL, "of 60 fps"), y, HUD_SMALL, "of 60 fps", GREY);
    hud_text(overlay, x + 1110 - hud_text_width(HUD_SMALL, "README"), y, HUD_SMALL, "README", GREY);
    y += hud_line_height(HUD_SMALL) + 12;
    for (int i = 0; i < rows; ++i, y += row_h) {
        if (bench.all) {
            bench_row(overlay, x, y, SCENARIOS[i].output, SCENARIOS[i].quality, bench.measured[i] ? bench.ms[i] : 0,
                      SCENARIOS[i].readme_ms);
        } else {
            const int known = scenario_of(bench.output, bench.quality);
            bench_row(overlay, x, y, bench.output, bench.quality, bench.phase == BENCH_RESULTS ? bench.single_ms : 0,
                      known >= 0 ? SCENARIOS[known].readme_ms : 0);
        }
    }
    if (bench.phase == BENCH_RUNNING) {
        const int total = rows * (BENCH_WARMUP + BENCH_FRAMES), done = bench.index * (BENCH_WARMUP + BENCH_FRAMES) + bench.frame;
        hud_fill(overlay, x, y + 18, pw - 80, 12, 6, TRACK);
        hud_fill(overlay, x, y + 18, (pw - 80) * done / total, 12, 6, ACCENT);
        y += 60;
    } else {
        y += 24;
    }
    char note[200];
    snprintf(note, sizeof(note), "The mean of %d frames submitted back to back, each timed from submission to completion "
             "on the GPU.", (int)BENCH_FRAMES);
    hud_text(overlay, x, y, HUD_SMALL, note, GREY);
    hud_text(overlay, x, y + hud_line_height(HUD_SMALL) + 2, HUD_SMALL,
             bench.phase == BENCH_RUNNING ? "○ cancel" : "README: the project's own measurements.     ○ close", GREY);
}

static void draw_hud(uint32_t *overlay)
{
    hud_begin(overlay);
    if (bench.phase != BENCH_IDLE) { draw_bench(overlay); return; }
    if (state.menu) draw_menu(overlay);
    else if (state.hud) draw_status(overlay);
    if (!state.hud && !state.menu) return;
    char line[160];
    const int left = VIEWS[state.view].left, right = VIEWS[state.view].right, zoom = LENS_ZOOMS[state.lens];
    if (left != right) {  /* the sources of a split, next to the divider */
        const int split = (int)state.split, ly = DISPLAY_H - 150;
        const int lw = hud_text_width(HUD_BODY, SOURCES[left]), rw = hud_text_width(HUD_BODY, SOURCES[right]);
        label(overlay, split - lw / 2 - 30, ly, HUD_BODY, SOURCES[left]);
        label(overlay, split + rw / 2 + 30, ly, HUD_BODY, SOURCES[right]);
    }
    if (zoom) {  /* under each lens circle: its source, zoom and pixels */
        const int pair = left != right, y = (int)state.lens_y + LENS_RADIUS + 12;
        for (int side = 0; side <= pair; ++side) {
            const int cx = (int)state.lens_x + (pair ? (side ? 1 : -1) * (LENS_RADIUS + 4) : 0);
            snprintf(line, sizeof(line), "%s · %d× · %s pixels", SOURCES[side ? right : left], zoom,
                     OUTPUTS[state.output].name);
            label(overlay, cx, y, HUD_SMALL, line);
        }
    }
    if (state.hud == 2 && !state.menu)
        label(overlay, DISPLAY_W / 2, DISPLAY_H - 70, HUD_SMALL,
              "OPTIONS settings     ✕ compare     □ lens     △ camera     L1 R1 scenario     touchpad display");
}

#ifndef SHOWCASE_HOST
static float stick(uint8_t v)
{
    float x = ((float)v - 128.0f) / 127.0f;
    return fabsf(x) < 0.15f ? 0.0f : x;
}
#endif

/* The scripted walk of a self-test or host build: each step is a setup that runs for
 * a while, is timed and saved as a picture; the last one runs the benchmark. */
static const struct step { int scenario, view, lens, drs, sharpen, menu, shot, bench; const char *name; } STEPS[] = {
    {0, V_FSR4, 0, 0, 0, 0, 1, 0, "ultra-1080p"},
    {1, V_FSR4, 0, 0, 0, 0, 0, 0, "quality-1080p"},
    {2, V_FSR4, 0, 0, 0, 0, 5, 0, "performance-1440p"},
    {3, V_FSR4, 0, 0, 0, 0, 2, 0, "quality-1440p"},
    {4, V_FSR4, 0, 0, 0, 0, 6, 0, "performance-4k"},
    {5, V_FSR4, 0, 0, 0, 0, 7, 0, "ultra-4k"},
    {1, V_VS_BILINEAR, 3, 0, 0, 0, 3, 0, "reading-split-bilinear"},
    {0, V_VS_BILINEAR, 3, 0, 0, 0, 4, 0, "chart-ultra-split-bilinear"},
    {1, V_VS_NATIVE, 3, 0, 0, 0, 1, 0, "tower-split-native"},
    {1, V_FSR4, 0, 1, 5, 1, 0, 0, "menu-dynamic-sharpen"},
    {1, V_FSR4, 0, 0, 0, 0, 0, 1, "benchmark"},
};
enum { STEP_COUNT = sizeof(STEPS) / sizeof(STEPS[0]) };

#ifdef SHOWCASE_HOST
/* SHOWCASE_KEYS plays button presses instead of the walk, one letter every four frames:
 * o Options, u d l r the D-pad, x Cross, c Circle, s Square, t Triangle, 1 L1, 2 R1, p Touchpad, . nothing. */
static uint32_t key_press(char key)
{
    switch (key) {
    case 'o': return PAD_OPTIONS; case 'u': return PAD_UP; case 'd': return PAD_DOWN; case 'l': return PAD_LEFT;
    case 'r': return PAD_RIGHT; case 'x': return PAD_CROSS; case 'c': return PAD_CIRCLE; case 's': return PAD_SQUARE;
    case 't': return PAD_TRIANGLE; case '1': return PAD_L1; case '2': return PAD_R1; case 'p': return PAD_TOUCH;
    default: return 0;
    }
}
#endif

static int scripted(void)
{
#ifdef SHOWCASE_HOST
    return getenv("SHOWCASE_KEYS") == NULL;
#else
    return SHOWCASE_SELFTEST;
#endif
}

static int step_frames(void)
{
#ifdef SHOWCASE_HOST
    return getenv("SHOWCASE_STEP_FRAMES") ? atoi(getenv("SHOWCASE_STEP_FRAMES")) : 12;
#else
    return 96;
#endif
}

static int step_wanted(int index)
{
#ifdef SHOWCASE_HOST
    const char *only = getenv("SHOWCASE_STEPS");  /* for example "0,6,9" */
    if (only) {
        for (const char *p = only; *p; p = strchr(p, ',') ? strchr(p, ',') + 1 : p + strlen(p))
            if (atoi(p) == index) return 1;
        return 0;
    }
#endif
    (void)index;
    return 1;
}

static void save_frame(const struct buffer *screenshot, const char *name)
{
    char path[256];
    VkMappedMemoryRange range = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, .memory = screenshot->memory,
                                 .size = VK_WHOLE_SIZE};
    if (vkInvalidateMappedMemoryRanges(device, 1, &range) != VK_SUCCESS) return;
    snprintf(path, sizeof(path), "%s/fsr4-showcase-%s.bgra", output_root(), name);
    FILE *f = fopen(path, "wb");
    if (f) { fwrite(screenshot->mapped, 1, (size_t)DISPLAY_W * DISPLAY_H * 4, f); fclose(f); }
    report("FSR4_SHOWCASE_SCREENSHOT name=%s saved=%d\n", name, f != NULL);
}

static int run(void)
{
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_3};
    VkInstanceCreateInfo ii = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app};
    VkInstance instance;
    CHECK(vkCreateInstance(&ii, NULL, &instance));
    uint32_t count = 1;
    CHECK(vkEnumeratePhysicalDevices(instance, &count, &physical));
    float priority = 1;
    VkDeviceQueueCreateInfo qi = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueCount = 1,
                                  .pQueuePriorities = &priority};
    VkPhysicalDeviceFeatures features = {.shaderInt16 = VK_TRUE};
    const char *extension = VK_KHR_STORAGE_BUFFER_STORAGE_CLASS_EXTENSION_NAME;
    VkDeviceCreateInfo di = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1,
        .pQueueCreateInfos = &qi, .pEnabledFeatures = &features, .enabledExtensionCount = 1,
        .ppEnabledExtensionNames = &extension};
#ifdef SHOWCASE_HOST  /* conformant desktop drivers need the features the shaders declare */
    VkPhysicalDeviceVulkan13Features f13 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    VkPhysicalDeviceVulkan12Features f12 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES, .pNext = &f13};
    VkPhysicalDeviceVulkan11Features f11 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_1_FEATURES, .pNext = &f12};
    VkPhysicalDeviceFeatures2 all = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, .pNext = &f11};
    vkGetPhysicalDeviceFeatures2(physical, &all);
    f13.robustImageAccess = VK_FALSE; all.features.robustBufferAccess = VK_FALSE;
    di.pNext = &all; di.pEnabledFeatures = NULL; di.enabledExtensionCount = 0;
    wave64 = f13.subgroupSizeControl;
#else  /* let FSR4 run each pass at its faster wave size where the driver offers it */
    VkPhysicalDeviceVulkan13Features offered = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    VkPhysicalDeviceFeatures2 query = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, .pNext = &offered};
    vkGetPhysicalDeviceFeatures2(physical, &query);
    VkPhysicalDeviceVulkan13Features enable13 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES,
                                                 .subgroupSizeControl = offered.subgroupSizeControl};
    wave64 = offered.subgroupSizeControl;
    if (wave64) di.pNext = &enable13;
#endif
    CHECK(vkCreateDevice(physical, &di, NULL, &device));
    vkGetDeviceQueue(device, 0, 0, &queue);
    vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);

    VkCommandPoolCreateInfo cpi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                   .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    VkCommandPool command_pool;
    CHECK(vkCreateCommandPool(device, &cpi, NULL, &command_pool));
    VkCommandBufferAllocateInfo cai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = command_pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    CHECK(vkAllocateCommandBuffers(device, &cai, &cmd));
    VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    CHECK(vkCreateFence(device, &fi, NULL, &fence));

    /* The displayed frame, the HUD overlay, a packed BGRA8 copy for saved frames and the signs. */
    struct image composed;
    struct buffer overlay, screenshot, signs;
    size_t sign_bytes = 0;
    void *sign_data = read_asset("signs.bin", &sign_bytes);
    if (!sign_data) { report("FSR4_SHOWCASE_ERROR signs.bin is missing\n"); return 1; }
    if (create_image(&composed, VK_FORMAT_R8G8B8A8_UNORM, DISPLAY_W, DISPLAY_H, 1) ||
        create_buffer(&overlay, (VkDeviceSize)DISPLAY_W * DISPLAY_H * 4) ||
        create_buffer(&screenshot, (VkDeviceSize)DISPLAY_W * DISPLAY_H * 4) ||
        create_buffer(&signs, sign_bytes))
        return 1;
    memcpy(signs.mapped, sign_data, sign_bytes);
    free(sign_data);
    /* The CPU draws the HUD here; upload_hud copies what changed into the overlay. */
    uint32_t *hud = calloc((size_t)HUD_W * HUD_H, 4);
    if (!hud) { report("FSR4_SHOWCASE_ERROR hud allocation\n"); return 1; }
    VkPhysicalDeviceProperties properties;
    vkGetPhysicalDeviceProperties(physical, &properties);
    if (properties.limits.nonCoherentAtomSize > overlay_atom) overlay_atom = properties.limits.nonCoherentAtomSize;
    memset(overlay.mapped, 0, (size_t)HUD_W * HUD_H * 4);
    VkMappedMemoryRange whole[2] = {
        {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, .memory = overlay.memory, .size = VK_WHOLE_SIZE},
        {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, .memory = signs.memory, .size = VK_WHOLE_SIZE}};
    CHECK(vkFlushMappedMemoryRanges(device, 2, whole));

#ifndef SHOWCASE_HOST
    /* Two scanout images in the one 128 MiB envelope VideoOut registers. */
    VkImageCreateInfo di_info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
        .format = VK_FORMAT_B8G8R8A8_UNORM, .extent = {DISPLAY_W, DISPLAY_H, 1}, .mipLevels = 1, .arrayLayers = 1,
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
    struct ps5vk_present_config pconfig = {DISPLAY_W, DISPLAY_H, VK_FORMAT_B8G8R8A8_UNORM, 2};
    ps5vk_present_surface surface;
    CHECK(ps5vkCreatePresentSurface(device, &pconfig, 2, display, &surface));
#endif

    size_t cache_bytes = 0;
    void *cache_blob = read_asset("pipeline-cache.bin", &cache_bytes);
    VkPipelineCacheCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO,
                                     .initialDataSize = cache_bytes, .pInitialData = cache_blob};
    CHECK(vkCreatePipelineCache(device, &pci, NULL, &pipeline_cache));
    free(cache_blob);

    VkDescriptorPoolSize sizes[3] = {{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 8}, {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 4},
                                     {VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1}};
    VkDescriptorPoolCreateInfo dpi = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 4,
                                      .poolSizeCount = 3, .pPoolSizes = sizes};
    VkDescriptorPool pool;
    CHECK(vkCreateDescriptorPool(device, &dpi, NULL, &pool));
    const VkDescriptorType I = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, B = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    const VkDescriptorType scene_types[4] = {I, B, I, I}, compose_types[6] = {I, I, I, B, I, B};
    double t0 = now_ms();
    if (create_compute(&scene, fsr4_showcase_scene_spv, sizeof(fsr4_showcase_scene_spv), scene_types, 4, 80, pool) ||
        create_compute(&native_scene, fsr4_showcase_native_spv, sizeof(fsr4_showcase_native_spv), scene_types, 2,
                       80, pool) ||
        create_compute(&compose, fsr4_showcase_compose_spv, sizeof(fsr4_showcase_compose_spv), compose_types, 6,
                       48, pool))
        return 1;
#ifndef SHOWCASE_HOST
    struct blit blit;
    if (create_blit(&blit, display, composed.view, pool)) return 1;
#endif
    report("FSR4_SHOWCASE_PIPELINES ms=%.1f signs=%zu\n", now_ms() - t0, sign_bytes);
    bind_buffer(scene.set, 1, signs.buffer);
    bind_buffer(native_scene.set, 1, signs.buffer);
    bind_buffer(compose.set, 3, overlay.buffer);
    bind_image(compose.set, 4, composed.view);
    bind_buffer(compose.set, 5, screenshot.buffer);
    if (begin()) return 1;
    image_barrier(composed.image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0, VK_ACCESS_SHADER_WRITE_BIT,
                  VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    if (submit(NULL) || setup_target(state.output, state.drs)) return 1;

    int32_t pad = -1;
#ifndef SHOWCASE_HOST
    int32_t user = -1;
    sceUserServiceInitialize(NULL);
    if (sceUserServiceGetInitialUser(&user) >= 0 && scePadInit() >= 0)
        pad = scePadOpen(user, 0, 0, NULL);
#endif
    report("FSR4_SHOWCASE_READY version=%s display=%dx%d pad=%d cache_in=%zu\n", SHOWCASE_VERSION, DISPLAY_W,
           DISPLAY_H, pad, cache_bytes);

    struct pose pose = shot_pose(0, 0), previous = pose;
    uint32_t last_buttons = 0, jitter_index = 0, sum_frames = 0;
    int reset = 1, reported_chapter = -1, last_shot = -1, last_camera = state.camera;
    double start = now_ms(), last_input = start, last_hud = 0, last_frame_start = start, scene_time = 0, previous_time = 0;
    double sum_fsr = 0, sum_scene = 0, sum_compose = 0, sum_present = 0, sum_frame = 0, hud_ms = 0;
    double step_fsr = 0, step_scene = 0, step_compose = 0, step_frame = 0;
    int step = -1, step_at = 0, step_count = 0;
    state.chapter_start = state.shot_start = start;
    for (uint32_t frame = 0;; ++frame) {
#ifdef SHOWCASE_HOST
        const double frame_start = start + frame * (1000.0 / 60.0), dt = 1000.0 / 60.0;
#else
        const double frame_start = now_ms(), dt = frame ? fmin(100.0, frame_start - last_frame_start) : 16.7;
#endif
        last_frame_start = frame_start;
        (void)last_frame_start;
        previous = pose;
        previous_time = scene_time;

        /* Input. */
        uint32_t buttons = 0;
        float lx = 0, ly = 0, rx = 0, ry = 0, l2 = 0, r2 = 0;
#ifndef SHOWCASE_HOST
        uint8_t pad_state[128] __attribute__((aligned(16)));
        if (pad >= 0 && scePadReadState(pad, pad_state) >= 0 && pad_state[76]) {
            memcpy(&buttons, pad_state, sizeof(buttons));
            if (buttons & 0x80000000u) buttons = 0;  /* intercepted by the system */
            lx = stick(pad_state[4]); ly = stick(pad_state[5]);
            rx = stick(pad_state[6]); ry = stick(pad_state[7]);
            l2 = pad_state[8] / 255.0f; r2 = pad_state[9] / 255.0f;
        }
#endif
        int capture = 0;
#ifdef SHOWCASE_HOST
        const char *keys = getenv("SHOWCASE_KEYS");
        if (keys) {
            const size_t presses = strlen(keys), at = frame / 4;
            if (at >= presses + 2) { save_frame(&screenshot, "keys"); return 0; }
            if (frame % 4 == 0 && at < presses) buttons = key_press(keys[at]);
            capture = at >= presses;
        }
#endif
        if (scripted()) {  /* the walk replaces the pad */
            buttons = 0; lx = ly = rx = ry = l2 = r2 = 0;
            const int frames = step_frames();
            if (step < 0 || (!STEPS[step].bench && step_at == frames) ||
                (STEPS[step].bench && bench.phase == BENCH_RESULTS && ++step_at > 4)) {
                if (step >= 0) {
                    const struct step *s = &STEPS[step];
                    report("FSR4_SHOWCASE_STEP name=%s output=%ux%u render=%ux%u view=%d lens=%d dynamic=%d sharpen=%d "
                           "fsr4_ms=%.2f scene_ms=%.2f compose_ms=%.2f frame_ms=%.2f\n", s->name,
                           OUTPUTS[state.output].w, OUTPUTS[state.output].h, stats.render_w, stats.render_h, state.view,
                           LENS_ZOOMS[state.lens], state.drs, state.sharpen, step_fsr / fmax(step_count, 1),
                           step_scene / fmax(step_count, 1), step_compose / fmax(step_count, 1),
                           step_frame / fmax(step_count, 1));
                    save_frame(&screenshot, s->name);
                }
                do ++step; while (step < STEP_COUNT && !step_wanted(step));
                if (step >= STEP_COUNT) {
                    report("FSR4_SHOWCASE_SELFTEST_DONE steps=%d\n", (int)STEP_COUNT);
                    return 0;
                }
                const struct step *s = &STEPS[step];
                state.output = SCENARIOS[s->scenario].output; state.quality = SCENARIOS[s->scenario].quality;
                state.view = s->view; state.lens = s->lens; state.drs = s->drs; state.sharpen = s->sharpen;
                state.menu = s->menu; state.row = ROW_SHARPEN; state.tour = 0; state.camera = CAMERA_CINEMATIC;
                state.shot = s->shot; state.shot_start = frame_start;
                bench.phase = BENCH_IDLE;
                if (s->bench) bench_start(1);
                step_at = step_count = 0;
                step_fsr = step_scene = step_compose = step_frame = 0;
                last_hud = 0;
            }
            if (!STEPS[step].bench) ++step_at;
            capture = STEPS[step].bench ? bench.phase == BENCH_RESULTS : step_at == frames;
        }
        const uint32_t pressed = buttons & ~last_buttons;
        last_buttons = buttons;
        const int moved = lx || ly || rx || ry || l2 > 0.1f || r2 > 0.1f;
        if (pressed || moved) last_input = frame_start;
        int start_tour = 0;
        if (bench.phase != BENCH_IDLE) {
            if (pressed & (PAD_CIRCLE | PAD_OPTIONS | PAD_CROSS)) {
                if (bench.phase == BENCH_RUNNING) state = bench.saved;
                bench.phase = BENCH_IDLE;
                state.menu = 1;
                reset = 1;
            }
        } else if (state.menu) {
            if (pressed & (PAD_OPTIONS | PAD_CIRCLE)) state.menu = 0;
            if (pressed & PAD_DOWN) state.row = (state.row + 1) % ROW_COUNT;
            if (pressed & PAD_UP) state.row = (state.row + ROW_COUNT - 1) % ROW_COUNT;
            if (pressed & PAD_RIGHT) start_tour = row_change(state.row, 1);
            if (pressed & PAD_LEFT) start_tour = row_change(state.row, -1);
            if (pressed & PAD_CROSS) start_tour = row_change(state.row, 0);
        } else if (pressed & PAD_OPTIONS) {
            state.menu = 1;
            state.tour = 0;
        } else if (pressed || moved) {
            state.tour = 0;
            if (pressed & PAD_CROSS) row_change(ROW_VIEW, 1);
            if (pressed & PAD_SQUARE) state.lens = state.lens ? 0 : 3;
            if (pressed & PAD_TRIANGLE) row_change(ROW_CAMERA, 1);
            if (pressed & PAD_R1) row_change(ROW_SCENARIO, 1);
            if (pressed & PAD_L1) row_change(ROW_SCENARIO, -1);
            if (pressed & PAD_TOUCH) row_change(ROW_HUD, -1);
            if (moved && state.camera == CAMERA_CINEMATIC && (lx || ly)) state.camera = CAMERA_FREE;
        }
        if (start_tour || (!scripted() && !state.tour && !state.menu && bench.phase == BENCH_IDLE &&
                           frame_start - last_input > 60000.0)) {
            state.tour = 1;
            state.menu = 0;
            state.chapter = 0;
            state.chapter_start = frame_start;
            last_input = frame_start;
        }

        /* The tour sets the scene up chapter by chapter. */
        if (state.tour) {
            double elapsed = frame_start - state.chapter_start;
            if (elapsed > TOUR[state.chapter].seconds * 1000.0) {
                state.chapter = (state.chapter + 1) % CHAPTERS;
                state.chapter_start = frame_start;
                elapsed = 0;
            }
            const struct chapter *c = &TOUR[state.chapter];
            state.output = c->output; state.quality = c->quality; state.view = c->view;
            state.lens = c->lens ? (c->output == OUT_4K ? 1 : c->output == OUT_1440P ? 2 : 3) : 0;
            state.drs = c->drs; state.sharpen = c->sharpen ? 5 : 0; state.split = DISPLAY_W / 2;
            state.lens_x = LENS_X; state.lens_y = LENS_Y; state.camera = CAMERA_CINEMATIC; state.paused = 0;
            if (state.chapter == CHAPTER_SHARPENING)
                state.sharpen = (int)(5.5 - 5.0 * cos(elapsed / (c->seconds * 1000.0) * 2.0 * PI));
            if (state.chapter != reported_chapter) {
                report("FSR4_SHOWCASE_CHAPTER index=%d title=%s\n", state.chapter, c->title);
                reported_chapter = state.chapter;
                state.shot = c->shot;
                state.shot_start = frame_start;
            }
        } else {
            reported_chapter = -1;
        }
        if (!state.menu && bench.phase == BENCH_IDLE) {  /* the D-pad moves the lens, or the divider without one */
            const float dx = ((buttons & PAD_RIGHT) ? 10.0f : 0.0f) - ((buttons & PAD_LEFT) ? 10.0f : 0.0f);
            const float dy = ((buttons & PAD_DOWN) ? 10.0f : 0.0f) - ((buttons & PAD_UP) ? 10.0f : 0.0f);
            if (state.lens) {
                const float reach = VIEWS[state.view].left != VIEWS[state.view].right ? 2 * LENS_RADIUS + 24.0f
                                                                                      : LENS_RADIUS + 20.0f;
                state.lens_x = fminf(DISPLAY_W - reach, fmaxf(reach, state.lens_x + dx));
                state.lens_y = fminf(DISPLAY_H - LENS_RADIUS - 150.0f, fmaxf(LENS_RADIUS + 20.0f, state.lens_y + dy));
            } else {
                state.split = fminf(DISPLAY_W - 200.0f, fmaxf(200.0f, state.split + dx));
            }
        }

        /* The benchmark measures one setting after another. */
        if (bench.phase == BENCH_RUNNING) {
            state.output = bench.all ? SCENARIOS[bench.index].output : bench.output;
            state.quality = bench.all ? SCENARIOS[bench.index].quality : bench.quality;
            state.drs = 0;
        }
        if (state.output != target.output || state.drs != target.drs) {
            if (setup_target(state.output, state.drs)) return 1;
            reset = 1;
            last_hud = 0;
        }

        /* Camera and render size. */
        if (!state.paused) scene_time += dt / 1000.0;
        if (state.camera == CAMERA_CINEMATIC) {
            const double length = (state.tour ? TOUR[state.chapter].seconds : SHOTS[state.shot].seconds) * 1000.0;
            if (!state.tour && !scripted() && frame_start - state.shot_start > length) {
                state.shot = (state.shot + 1) % SHOT_COUNT;
                state.shot_start = frame_start;
            }
            const double t = scripted() ? 0.5 + 0.1 * step_at / fmax(step_frames(), 1) : (frame_start - state.shot_start) / length;
            if (!state.paused || state.shot != last_shot) pose = shot_pose(state.shot, t);
        } else {
            const float seconds = (float)(dt / 1000.0), speed = 14.0f * seconds;
            pose.yaw += rx * 1.7f * seconds;
            pose.pitch = fminf(1.45f, fmaxf(-1.45f, pose.pitch - ry * 1.3f * seconds));
            const float fx = cosf(pose.pitch) * cosf(pose.yaw), fy = sinf(pose.pitch), fz = cosf(pose.pitch) * sinf(pose.yaw);
            pose.x += (-ly * fx - lx * sinf(pose.yaw)) * speed;
            pose.y = fmaxf(0.6f, pose.y + (-ly * fy + (r2 - l2)) * speed);
            pose.z += (-ly * fz + lx * cosf(pose.yaw)) * speed;
        }
        if (state.shot != last_shot || state.camera != last_camera || frame == 0) {  /* a cut */
            if (state.camera == CAMERA_CINEMATIC || frame == 0) { previous = pose; reset = 1; }
            last_shot = state.shot;
            last_camera = state.camera;
        }
        uint32_t rw, rh;
        render_size(state.output, state.quality, &rw, &rh);
        if (state.drs) {  /* between the mode's size and half of it, once every 4 s */
            const double scale = 0.75 + 0.25 * cos(frame_start / 4000.0 * 2.0 * PI);
            rw = (uint32_t)(rw * scale); rh = (uint32_t)(rh * scale);
        }
        const uint32_t ow = OUTPUTS[state.output].w, oh = OUTPUTS[state.output].h;
        const int measuring = bench.phase == BENCH_RUNNING;
        const int view = measuring ? V_FSR4 : state.view;
        const int native = VIEWS[view].left == SRC_NATIVE || VIEWS[view].right == SRC_NATIVE;

        /* The scene at render size with jitter, and at output size without for a native view. */
        float jx, jy;
        ps5fsr4_jitter_offset(jitter_index++, ps5fsr4_jitter_phase_count(rw, ow), &jx, &jy);
        struct { float eye[4], view[4], prev_eye[4], prev_view[4], frame[4]; } scene_params = {
            {pose.x, pose.y, pose.z, (float)scene_time}, {pose.yaw, pose.pitch, pose.tan_half, 0.0f},
            {previous.x, previous.y, previous.z, (float)previous_time},
            {previous.yaw, previous.pitch, previous.tan_half, 0.0f}, {jx, jy, (float)rw, (float)rh}};
        double scene_ms, fsr_ms = 0, compose_ms;
        if (begin()) return 1;
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, scene.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, scene.layout, 0, 1, &scene.set, 0, NULL);
        vkCmdPushConstants(cmd, scene.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(scene_params), &scene_params);
        vkCmdDispatch(cmd, (rw + 7) / 8, (rh + 7) / 8, 1);
        if (native) {
            scene_params.frame[0] = scene_params.frame[1] = 0.0f;
            scene_params.frame[2] = (float)ow; scene_params.frame[3] = (float)oh;
            vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, native_scene.pipeline);
            vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, native_scene.layout, 0, 1,
                                    &native_scene.set, 0, NULL);
            vkCmdPushConstants(cmd, native_scene.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(scene_params),
                               &scene_params);
            vkCmdDispatch(cmd, (ow + 7) / 8, (oh + 7) / 8, 1);
        }
        if (submit(&scene_ms)) return 1;

        /* FSR4: once, or a batch of timed dispatches while the benchmark runs. */
        for (int pass = 0; pass < (measuring ? BENCH_BATCH : 1); ++pass) {
            if (begin()) return 1;
            memory_barrier(VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                           VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
            ps5fsr4_dispatch_desc dd = {.struct_size = sizeof(dd), .command_buffer = cmd,
                .color = target.color.view, .depth = target.depth.view, .motion_vectors = target.motion.view,
                .output = target.upscaled.view, .color_layout = VK_IMAGE_LAYOUT_GENERAL,
                .depth_layout = VK_IMAGE_LAYOUT_GENERAL, .motion_vectors_layout = VK_IMAGE_LAYOUT_GENERAL,
                .render_width = rw, .render_height = rh, .jitter_x = jx, .jitter_y = jy,
                .motion_vector_scale_x = (float)rw, .motion_vector_scale_y = (float)rh, .pre_exposure = 1.0f,
                .frame_time_delta_ms = (float)dt, .camera_near = 0.2f, .camera_far = 4000.0f,
                .camera_fov_vertical = 2.0f * atanf(pose.tan_half), .reset = reset,
                .enable_sharpening = !measuring && state.sharpen > 0, .sharpness = state.sharpen / 10.0f};
            ps5fsr4_result fr = ps5fsr4_dispatch(target.context, &dd);
            if (fr) { report("FSR4_SHOWCASE_ERROR dispatch=%d\n", (int)fr); return 1; }
            if (submit(&fsr_ms)) return 1;
            reset = 0;
            if (!measuring) break;
            if (bench.frame++ >= BENCH_WARMUP) bench.sum += fsr_ms;
            if (bench.frame == BENCH_WARMUP + BENCH_FRAMES) {
                const double mean = bench.sum / BENCH_FRAMES;
                report("FSR4_SHOWCASE_BENCH render=%ux%u output=%ux%u quality=%d frames=%d ms=%.3f\n", rw, rh, ow, oh,
                       state.quality, (int)BENCH_FRAMES, mean);
                if (bench.all) { bench.ms[bench.index] = mean; bench.measured[bench.index] = 1; }
                else bench.single_ms = mean;
                bench.frame = 0;
                bench.sum = 0;
                if (!bench.all || ++bench.index == SCENARIO_COUNT) {
                    state = bench.saved;
                    state.menu = 0;
                    bench.phase = BENCH_RESULTS;
                }
                reset = 1;
                last_hud = 0;
                break;
            }
        }

        /* The HUD, redrawn four times a second and whenever the setup changes. */
        stats.render_w = rw; stats.render_h = rh;
        if (frame_start - last_hud > 250.0 || pressed || buttons || measuring ||
            (state.tour && frame_start - state.chapter_start < 20.0)) {
            const double hud_start = now_ms();
            draw_hud(hud);
            CHECK(upload_hud(hud, &overlay));
            hud_ms = fmax(hud_ms, now_ms() - hud_start);
            last_hud = frame_start;
        }

        /* Compose and present. */
        const uint32_t slot = frame & 1;
        const int zoom = measuring || bench.phase == BENCH_RESULTS ? 0 : LENS_ZOOMS[state.lens];
        struct { int32_t view[4], lens[4], sizes[4]; } compose_params = {
            {VIEWS[view].left, VIEWS[view].right, (int32_t)state.split, zoom},
            {(int32_t)state.lens_x, (int32_t)state.lens_y, LENS_RADIUS, capture},
            {(int32_t)rw, (int32_t)rh, (int32_t)ow, (int32_t)oh}};
        if (begin()) return 1;
#ifndef SHOWCASE_HOST
        if (frame) {  /* the previous frame's draw left the composed frame read-only */
            image_barrier(composed.image, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
                          VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_SHADER_WRITE_BIT,
                          VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        }
#endif
        memory_barrier(VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                       VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, compose.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, compose.layout, 0, 1, &compose.set, 0, NULL);
        vkCmdPushConstants(cmd, compose.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(compose_params),
                           &compose_params);
        vkCmdDispatch(cmd, DISPLAY_W / 8, (DISPLAY_H + 7) / 8, 1);
        const double present_start = now_ms();
#ifdef SHOWCASE_HOST
        (void)slot;
        if (submit(&compose_ms)) return 1;
#else
        image_barrier(composed.image, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                      VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                      VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
        VkRenderPassBeginInfo rb = {.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO, .renderPass = blit.pass,
            .framebuffer = blit.framebuffers[slot], .renderArea = {{0, 0}, {DISPLAY_W, DISPLAY_H}}};
        vkCmdBeginRenderPass(cmd, &rb, VK_SUBPASS_CONTENTS_INLINE);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, blit.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, blit.layout, 0, 1, &blit.set, 0, NULL);
        vkCmdDraw(cmd, 3, 1, 0, 0);
        vkCmdEndRenderPass(cmd);
        if (submit(&compose_ms)) return 1;
        VkResult pr = ps5vkPresentFrame(surface, slot, (uint64_t)frame + 1);
        if (pr != VK_SUCCESS) { report("FSR4_SHOWCASE_ERROR present=%d\n", (int)pr); return 1; }
#endif
        sum_present += now_ms() - present_start - compose_ms;

        /* Statistics, smoothed for the HUD and logged every 120 frames. */
        const double frame_ms = now_ms() - frame_start;
        stats.fsr4_ms = frame ? stats.fsr4_ms * 0.9 + fsr_ms * 0.1 : fsr_ms;
        stats.scene_ms = frame ? stats.scene_ms * 0.9 + scene_ms * 0.1 : scene_ms;
        stats.compose_ms = frame ? stats.compose_ms * 0.9 + compose_ms * 0.1 : compose_ms;
        stats.frame_ms = frame ? stats.frame_ms * 0.9 + dt * 0.1 : dt;
        sum_fsr += fsr_ms; sum_scene += scene_ms; sum_compose += compose_ms; sum_frame += frame_ms;
        if (step >= 0 && step_at > step_frames() / 3) {
            step_fsr += fsr_ms; step_scene += scene_ms; step_compose += compose_ms; step_frame += frame_ms;
            ++step_count;
        }
        if (++sum_frames == 120) {
            report("FSR4_SHOWCASE_STATS frames=%u output=%ux%u render=%ux%u quality=%d view=%d dynamic=%d sharpen=%d "
                   "fsr4_ms=%.2f scene_ms=%.2f compose_ms=%.2f present_ms=%.2f frame_ms=%.2f hud_max_ms=%.2f\n",
                   frame + 1, ow, oh, rw, rh, state.quality, state.view, state.drs, state.sharpen, sum_fsr / 120,
                   sum_scene / 120, sum_compose / 120, sum_present / 120, sum_frame / 120, hud_ms);
            sum_fsr = sum_scene = sum_compose = sum_present = sum_frame = hud_ms = 0;
            sum_frames = 0;
        }
    }
}

int main(void)
{
    char path[256];
    snprintf(path, sizeof(path), "%s/fsr4-showcase-log.txt", output_root());
    log_file = fopen(path, "w");
    int result = fsr4_native_heap_init();
    report("FSR4_SHOWCASE_BEGIN heap=%d\n", result);
    if (!result) result = run();
    report("FSR4_SHOWCASE_END result=%s\n", result ? "FAIL" : "EXIT");
    if (log_file) fclose(log_file);
#ifdef SHOWCASE_HOST
    return result;
#else
    if (SHOWCASE_SELFTEST && !result) {  /* a self-test ends the title itself once the log can be fetched */
        sleep(15);
        sceSystemServiceLoadExec("exit", NULL);
    }
    for (;;) usleep(100000);
#endif
}
