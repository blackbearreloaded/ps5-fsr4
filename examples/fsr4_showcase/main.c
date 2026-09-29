/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * PS5 FSR4 Showcase (PPSA99010): the FSR4 demo scene upscaled by ps5_fsr4 in
 * every configuration the SDK offers, with a guided tour and a HUD:
 *
 *   - the FidelityFX quality modes, Native AA (1x) to Ultra Performance (3x,
 *     AMD's dedicated model);
 *   - output at 1920x1080, 2560x1440 or 3840x2160: larger outputs are
 *     box-filtered to the 1080p display, and the lens shows their own pixels;
 *   - FSR4 alone or split against a bilinear upscale of the same frame or a
 *     native render without anti-aliasing, with a lens that shows the same
 *     pixels from both sides;
 *   - dynamic resolution and RCAS sharpening.
 *
 * The tour runs until a button is pressed and resumes after 45 s without input.
 *   Left stick   orbit                     L2 / R2      camera distance
 *   Right stick  move the divider, or the lens while it is shown
 *   Cross        next view                 Circle       next output size
 *   L1 / R1      quality mode              Square       lens (D-pad left/right: zoom)
 *   Triangle     sharpening (D-pad up/down: strength)
 *   L3           dynamic resolution        R3           pause the scene
 *   Options      guided tour on/off        Touch pad    hide/show the HUD
 */
#include <ps5vk/ps5vk.h>
#include <ps5vk/ps5vk_present.h>
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

extern int sceKernelDebugOutText(int level, const char *text);
extern int sceUserServiceInitialize(void *params);
extern int sceUserServiceGetInitialUser(int32_t *user);
extern int scePadInit(void);
extern int scePadOpen(int32_t user, int32_t type, int32_t index, const void *params);
extern int scePadReadState(int32_t handle, void *data);
extern int fsr4_native_heap_init(void);

#ifndef FSR4_SHOWCASE_SCREENSHOTS
#define FSR4_SHOWCASE_SCREENSHOTS 0  /* 1: save one frame of each tour chapter, for documentation */
#endif
#ifndef FSR4_SHOWCASE_SELFTEST
#define FSR4_SHOWCASE_SELFTEST 0     /* 1: replace the pad with a scripted walk over every setting */
#endif

enum { DISPLAY_W = HUD_W, DISPLAY_H = HUD_H, LENS_RADIUS = 200, LENS_X = DISPLAY_W / 2, LENS_Y = 640 };
static const double PI = 3.14159265358979323846;
enum { PAD_L3 = 0x2, PAD_R3 = 0x4, PAD_OPTIONS = 0x8, PAD_UP = 0x10, PAD_RIGHT = 0x20, PAD_DOWN = 0x40,
       PAD_LEFT = 0x80, PAD_L1 = 0x400, PAD_R1 = 0x800, PAD_TRIANGLE = 0x1000, PAD_CIRCLE = 0x2000,
       PAD_CROSS = 0x4000, PAD_SQUARE = 0x8000, PAD_TOUCH = 0x100000 };

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

/* 4K goes up to Quality (2560x1440): Native AA would render the scene itself at 4K. */
static int quality_allowed(int output, int quality) { return !(output == OUT_4K && quality == Q_NATIVE); }

static void render_size(int output, int quality, uint32_t *w, uint32_t *h)
{
    *w = (uint32_t)((float)OUTPUTS[output].w / QUALITIES[quality].scale);  /* FidelityFX truncates */
    *h = (uint32_t)((float)OUTPUTS[output].h / QUALITIES[quality].scale);
}

static const struct chapter {
    const char *title, *caption;
    int output, quality, view, lens, drs, sharpen;
    double seconds;
} TOUR[] = {
    {"FSR 4 on PlayStation 5", "AMD's machine-learning upscaler, running natively on the PS5 GPU",
     OUT_1080P, Q_QUALITY, V_FSR4, 0, 0, 0, 10},
    {"FSR 4 against bilinear", "Left: FSR 4. Right: the same 1280×720 frame, scaled bilinearly",
     OUT_1080P, Q_QUALITY, V_VS_BILINEAR, 1, 0, 0, 12},
    {"Every quality mode", "Quality 1.5×, Balanced 1.7×, Performance 2×, Ultra Performance 3×",
     OUT_1080P, Q_QUALITY, V_VS_BILINEAR, 0, 0, 0, 12},
    {"Ultra Performance", "640×360 → 1920×1080: a ninth of the pixels, AMD's dedicated 3× model",
     OUT_1080P, Q_ULTRA, V_VS_BILINEAR, 1, 0, 0, 10},
    {"Dynamic resolution", "The render size changes every frame; FSR 4 keeps its history",
     OUT_1080P, Q_QUALITY, V_FSR4, 0, 1, 0, 12},
    {"RCAS sharpening", "Contrast-adaptive sharpening after upscaling, strength 0 to 1",
     OUT_1080P, Q_QUALITY, V_FSR4, 1, 0, 1, 10},
    {"1440p output", "1280×720 → 2560×1440; the lens shows real 1440p pixels",
     OUT_1440P, Q_PERFORMANCE, V_FSR4, 1, 0, 0, 10},
    {"4K output", "1920×1080 → 3840×2160; the lens shows real 4K pixels",
     OUT_4K, Q_PERFORMANCE, V_VS_BILINEAR, 1, 0, 0, 12},
    {"4K from 720p", "1280×720 → 3840×2160 with the Ultra Performance model",
     OUT_4K, Q_ULTRA, V_VS_BILINEAR, 1, 0, 0, 10},
    {"FSR 4 against native rendering", "FSR 4 from 960×540 against native 1920×1080 without anti-aliasing",
     OUT_1080P, Q_PERFORMANCE, V_VS_NATIVE, 1, 0, 0, 12},
};
enum { CHAPTERS = sizeof(TOUR) / sizeof(TOUR[0]), CHAPTER_QUALITY_MODES = 2, CHAPTER_SHARPENING = 5 };

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

/* The pipelines and the per-output resources: render targets up to the largest render
 * size of the output, the FSR4 output, a native render at output size and the context. */
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
    bind_image(scene.set, 1, target.depth.view);
    bind_image(scene.set, 2, target.motion.view);
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

/* What the viewer sees and how it was measured. */
static struct state {
    int output, quality, view, lens, lens_zoom, drs, sharpen, hud, tour, chapter, paused, auto_orbit;
    float sharpness, split, lens_x, lens_y;
    double chapter_start;
} state = {OUT_1080P, Q_QUALITY, V_FSR4, 0, 4, 0, 0, 1, 1, 0, 0, 1, 0.5f, DISPLAY_W / 2, LENS_X, LENS_Y, 0};

static struct stats { double fsr4_ms, scene_ms, compose_ms, frame_ms; uint32_t render_w, render_h; } stats;

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

/* The self-test's presses, one every 24 frames: every view at every quality mode
 * of each output, then each toggle; 0 when the script is over. */
static uint32_t selftest_press(uint32_t step)
{
    static const uint32_t toggles[] = {PAD_SQUARE, PAD_RIGHT, PAD_RIGHT, PAD_LEFT, PAD_CROSS, PAD_TRIANGLE, PAD_UP,
        PAD_DOWN, PAD_TRIANGLE, PAD_SQUARE, PAD_L3, PAD_CIRCLE, PAD_CIRCLE, PAD_R1, PAD_L3, PAD_R3, PAD_R3,
        PAD_TOUCH, PAD_TOUCH, PAD_CIRCLE, PAD_OPTIONS};
    if (step < 3 * 30) return step % 30 == 29 ? PAD_CIRCLE : step % 6 == 5 ? PAD_R1 : PAD_CROSS;
    step -= 3 * 30;
    return step < sizeof(toggles) / sizeof(toggles[0]) ? toggles[step] : 0;
}

static void draw_hud(uint32_t *overlay)
{
    hud_begin(overlay);
    if (!state.hud) return;
    const uint32_t white = HUD_RGBA(255, 255, 255, 255), grey = HUD_RGBA(190, 198, 210, 255);
    const uint32_t accent = HUD_RGBA(255, 132, 44, 255), panel = HUD_RGBA(12, 14, 20, 208);
    const struct chapter *c = &TOUR[state.chapter];

    /* Status panel: a title, a caption and four lines of settings and timings. */
    enum { LINES = 6 };
    char text[LINES][160], badge[32] = "";
    const enum hud_face faces[LINES] = {HUD_TITLE, HUD_BODY, HUD_BODY, HUD_BODY, HUD_BODY, HUD_BODY};
    const uint32_t colors[LINES] = {white, grey, white, white, white, accent};
    const uint32_t ow = OUTPUTS[state.output].w, oh = OUTPUTS[state.output].h;
    snprintf(text[0], sizeof(text[0]), "%s", state.tour ? c->title : "PS5 FSR4 Showcase");
    snprintf(text[1], sizeof(text[1]), "%s", state.tour ? c->caption : "Manual mode · OPTIONS starts the guided tour");
    if (state.drs)
        snprintf(text[2], sizeof(text[2]), "Dynamic resolution · %u×%u → %u×%u", stats.render_w, stats.render_h,
                 ow, oh);
    else
        snprintf(text[2], sizeof(text[2]), "%s %s · %u×%u → %u×%u", QUALITIES[state.quality].name,
                 QUALITIES[state.quality].ratio, stats.render_w, stats.render_h, ow, oh);
    if (state.lens)
        snprintf(text[3], sizeof(text[3]), "View: %s · lens %d×", VIEWS[state.view].name, state.lens_zoom);
    else
        snprintf(text[3], sizeof(text[3]), "View: %s", VIEWS[state.view].name);
    if (state.sharpen) snprintf(text[4], sizeof(text[4]), "RCAS sharpening %.2f", (double)state.sharpness);
    else snprintf(text[4], sizeof(text[4]), "RCAS sharpening off");
    snprintf(text[5], sizeof(text[5]), "FSR 4 %.2f ms · scene %.2f ms · %.0f fps", stats.fsr4_ms, stats.scene_ms,
             stats.frame_ms > 0 ? 1000.0 / stats.frame_ms : 0.0);
    if (state.tour) snprintf(badge, sizeof(badge), "%d / %d", state.chapter + 1, (int)CHAPTERS);
    int width = hud_text_width(HUD_TITLE, text[0]) + (badge[0] ? 40 + hud_text_width(HUD_SMALL, badge) : 0);
    int height = 36;
    for (int i = 0; i < LINES; ++i) {
        const int w = hud_text_width(faces[i], text[i]);
        if (w > width) width = w;
        height += hud_line_height(faces[i]) + (i == 1 ? 12 : 0);
    }
    const int px = 36, py = 36, pw = width + 64;
    hud_fill(overlay, px, py, pw, height, 18, panel);
    hud_fill(overlay, px, py, 8, height, 4, accent);
    int y = py + 18;
    for (int i = 0; i < LINES; ++i) {
        hud_text(overlay, px + 32, y, faces[i], text[i], colors[i]);
        y += hud_line_height(faces[i]) + (i == 1 ? 12 : 0);
    }
    if (badge[0])
        hud_text(overlay, px + pw - 24 - hud_text_width(HUD_SMALL, badge), py + 26, HUD_SMALL, badge, grey);
    char line[160];

    /* The sources of a split, next to the divider. */
    const int left = VIEWS[state.view].left, right = VIEWS[state.view].right;
    if (left != right) {
        const int split = (int)state.split, ly = DISPLAY_H - 150;
        const int lw = hud_text_width(HUD_BODY, SOURCES[left]), rw = hud_text_width(HUD_BODY, SOURCES[right]);
        hud_fill(overlay, split - lw - 44, ly, lw + 28, 40, 12, panel);
        hud_text(overlay, split - lw - 30, ly + 5, HUD_BODY, SOURCES[left], white);
        hud_fill(overlay, split + 16, ly, rw + 28, 40, 12, panel);
        hud_text(overlay, split + 30, ly + 5, HUD_BODY, SOURCES[right], white);
    }
    if (state.lens) {  /* under each lens circle: its source, zoom and pixels */
        const int pair = left != right, y = (int)state.lens_y + LENS_RADIUS + 12;
        for (int side = 0; side <= pair; ++side) {
            const int cx = (int)state.lens_x + (pair ? (side ? 1 : -1) * (LENS_RADIUS + 4) : 0);
            snprintf(line, sizeof(line), "%s · %d× · %s pixels", SOURCES[side ? right : left], state.lens_zoom,
                     OUTPUTS[state.output].name);
            const int lw = hud_text_width(HUD_SMALL, line);
            hud_fill(overlay, cx - lw / 2 - 12, y, lw + 24, 32, 10, panel);
            hud_text(overlay, cx - lw / 2, y + 4, HUD_SMALL, line, white);
        }
    }

    /* Controls. */
    const char *controls = "✕ view    L1 R1 quality    ○ output    □ lens    △ sharpen    L3 dynamic res.    "
                           "R3 pause    OPTIONS tour    touch pad HUD";
    const int cw = hud_text_width(HUD_SMALL, controls);
    hud_fill(overlay, (DISPLAY_W - cw) / 2 - 24, DISPLAY_H - 70, cw + 48, 40, 14, panel);
    hud_text(overlay, (DISPLAY_W - cw) / 2, DISPLAY_H - 64, HUD_SMALL, controls, grey);
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
    /* Let FSR4 run each pass at its faster wave size where the driver offers it. */
    VkPhysicalDeviceVulkan13Features offered = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    VkPhysicalDeviceFeatures2 query = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, .pNext = &offered};
    vkGetPhysicalDeviceFeatures2(physical, &query);
    VkPhysicalDeviceVulkan13Features enable13 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES,
                                                 .subgroupSizeControl = offered.subgroupSizeControl};
    wave64 = offered.subgroupSizeControl;
    VkDeviceCreateInfo di = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .pNext = wave64 ? &enable13 : NULL, .queueCreateInfoCount = 1,
        .pQueueCreateInfos = &qi, .pEnabledFeatures = &features, .enabledExtensionCount = 1,
        .ppEnabledExtensionNames = &extension};
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

    /* The displayed frame, the HUD overlay and, for screenshots, a packed BGRA8 copy. */
    struct image composed;
    struct buffer overlay, screenshot;
    if (create_image(&composed, VK_FORMAT_R8G8B8A8_UNORM, DISPLAY_W, DISPLAY_H, 1) ||
        create_buffer(&overlay, (VkDeviceSize)DISPLAY_W * DISPLAY_H * 4) ||
        create_buffer(&screenshot, (VkDeviceSize)DISPLAY_W * DISPLAY_H * 4))
        return 1;
    /* The CPU draws the HUD here; upload_hud copies what changed into the overlay. */
    uint32_t *hud = calloc((size_t)HUD_W * HUD_H, 4);
    if (!hud) { report("FSR4_SHOWCASE_ERROR hud allocation\n"); return 1; }
    VkPhysicalDeviceProperties properties;
    vkGetPhysicalDeviceProperties(physical, &properties);
    if (properties.limits.nonCoherentAtomSize > overlay_atom) overlay_atom = properties.limits.nonCoherentAtomSize;
    memset(overlay.mapped, 0, (size_t)HUD_W * HUD_H * 4);
    VkMappedMemoryRange whole = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, .memory = overlay.memory,
                                 .size = VK_WHOLE_SIZE};
    CHECK(vkFlushMappedMemoryRanges(device, 1, &whole));

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

    size_t cache_bytes = 0;
    void *cache_blob = read_asset("pipeline-cache.bin", &cache_bytes);
    VkPipelineCacheCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO,
                                     .initialDataSize = cache_bytes, .pInitialData = cache_blob};
    CHECK(vkCreatePipelineCache(device, &pci, NULL, &pipeline_cache));
    free(cache_blob);

    VkDescriptorPoolSize sizes[3] = {{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 8}, {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 2},
                                     {VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1}};
    VkDescriptorPoolCreateInfo dpi = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 4,
                                      .poolSizeCount = 3, .pPoolSizes = sizes};
    VkDescriptorPool pool;
    CHECK(vkCreateDescriptorPool(device, &dpi, NULL, &pool));
    const VkDescriptorType I = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, B = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    const VkDescriptorType scene_types[3] = {I, I, I}, compose_types[6] = {I, I, I, B, I, B};
    struct blit blit;
    if (create_compute(&scene, fsr4_showcase_scene_spv, sizeof(fsr4_showcase_scene_spv), scene_types, 3, 48, pool) ||
        create_compute(&native_scene, fsr4_showcase_native_spv, sizeof(fsr4_showcase_native_spv), scene_types, 1,
                       48, pool) ||
        create_compute(&compose, fsr4_showcase_compose_spv, sizeof(fsr4_showcase_compose_spv), compose_types, 6,
                       48, pool) ||
        create_blit(&blit, display, composed.view, pool))
        return 1;
    bind_buffer(compose.set, 3, overlay.buffer);
    bind_image(compose.set, 4, composed.view);
    bind_buffer(compose.set, 5, screenshot.buffer);
    if (begin()) return 1;
    image_barrier(composed.image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0, VK_ACCESS_SHADER_WRITE_BIT,
                  VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    if (submit(NULL) || setup_target(state.output, state.drs)) return 1;

    int32_t pad = -1, user = -1;
    sceUserServiceInitialize(NULL);
    if (sceUserServiceGetInitialUser(&user) >= 0 && scePadInit() >= 0)
        pad = scePadOpen(user, 0, 0, NULL);
    report("FSR4_SHOWCASE_READY display=%dx%d pad=%d cache_in=%zu\n", DISPLAY_W, DISPLAY_H, pad, cache_bytes);

    float camera[4] = {0.6f, 0.35f, 9.0f, 0.0f}, previous[4];
    uint32_t last_buttons = 0, jitter_index = 0, sum_frames = 0, shots_taken = 0;
    int reset = 1, reported_chapter = -1;
    double start = now_ms(), last_input = start, last_hud = 0, last_frame_start = start, scene_time = 0;
    double sum_fsr = 0, sum_scene = 0, sum_compose = 0, sum_present = 0, sum_frame = 0, hud_ms = 0;
    double step_fsr = 0, step_scene = 0, step_compose = 0, step_frame = 0;
    uint32_t step_frames = 0, step = 0;
    state.chapter_start = start;
    for (uint32_t frame = 0;; ++frame) {
        const double frame_start = now_ms(), dt = frame ? frame_start - last_frame_start : 16.7;
        last_frame_start = frame_start;
        memcpy(previous, camera, sizeof(camera));

        /* Input. */
        uint8_t pad_state[128] __attribute__((aligned(16)));
        uint32_t buttons = 0;
        float lx = 0, ly = 0, rx = 0, ry = 0, l2 = 0, r2 = 0;
        if (pad >= 0 && scePadReadState(pad, pad_state) >= 0 && pad_state[76]) {
            memcpy(&buttons, pad_state, sizeof(buttons));
            if (buttons & 0x80000000u) buttons = 0;  /* intercepted by the system */
            lx = stick(pad_state[4]); ly = stick(pad_state[5]);
            rx = stick(pad_state[6]); ry = stick(pad_state[7]);
            l2 = pad_state[8] / 255.0f; r2 = pad_state[9] / 255.0f;
        }
        if (FSR4_SHOWCASE_SELFTEST && frame >= 120 && frame % 24 == 0 && step != UINT32_MAX) {
            if (step_frames)  /* the setting the previous press left, measured after it settled */
                report("FSR4_SHOWCASE_STEP index=%u output=%ux%u render=%ux%u quality=%d view=%d lens=%d dynamic=%d "
                       "sharpen=%d hud=%d tour=%d fsr4_ms=%.2f scene_ms=%.2f compose_ms=%.2f frame_ms=%.2f\n",
                       step, OUTPUTS[state.output].w, OUTPUTS[state.output].h, stats.render_w, stats.render_h,
                       state.quality, state.view, state.lens, state.drs, state.sharpen, state.hud, state.tour,
                       step_fsr / step_frames, step_scene / step_frames, step_compose / step_frames,
                       step_frame / step_frames);
            step_fsr = step_scene = step_compose = step_frame = 0;
            step_frames = 0;
            buttons = selftest_press(step++);
            if (!buttons) {
                report("FSR4_SHOWCASE_SELFTEST_DONE steps=%u\n", step - 1);
                step = UINT32_MAX;
            }
        }
        const uint32_t pressed = buttons & ~last_buttons;
        last_buttons = buttons;
        const int moved = lx || ly || rx || ry || l2 > 0.1f || r2 > 0.1f;
        if (pressed & PAD_OPTIONS) {
            state.tour = !state.tour;
            state.chapter = 0;
            state.chapter_start = frame_start;
        } else if (pressed || moved) {
            state.tour = 0;
        }
        if (pressed || moved) last_input = frame_start;
        if (!state.tour && frame_start - last_input > 45000.0) {
            state.tour = 1;
            state.chapter = 0;
            state.chapter_start = frame_start;
        }
        if (!state.tour) {
            if (pressed & PAD_CROSS) state.view = (state.view + 1) % VIEW_COUNT;
            if (pressed & PAD_CIRCLE) {
                state.output = (state.output + 1) % OUTPUT_COUNT;
                state.lens_zoom = OUTPUTS[state.output].lens_zoom;
                if (!quality_allowed(state.output, state.quality)) state.quality = Q_QUALITY;
            }
            if (pressed & PAD_R1)
                do state.quality = (state.quality + 1) % QUALITY_COUNT;
                while (!quality_allowed(state.output, state.quality));
            if (pressed & PAD_L1)
                do state.quality = (state.quality + QUALITY_COUNT - 1) % QUALITY_COUNT;
                while (!quality_allowed(state.output, state.quality));
            if (pressed & PAD_SQUARE) state.lens = !state.lens;
            if ((pressed & PAD_RIGHT) && state.lens_zoom < 8) ++state.lens_zoom;
            if ((pressed & PAD_LEFT) && state.lens_zoom > 2) --state.lens_zoom;
            if (pressed & PAD_TRIANGLE) state.sharpen = !state.sharpen;
            if (pressed & PAD_UP) state.sharpness = fminf(1.0f, state.sharpness + 0.1f);
            if (pressed & PAD_DOWN) state.sharpness = fmaxf(0.0f, state.sharpness - 0.1f);
            if (pressed & PAD_L3) state.drs = !state.drs;
            if (pressed & PAD_R3) state.paused = !state.paused;
            if (pressed & PAD_TOUCH) state.hud = !state.hud;
            if (lx || ly) state.auto_orbit = 0;
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
            state.output = c->output; state.lens_zoom = OUTPUTS[c->output].lens_zoom;
            state.quality = c->quality; state.view = c->view; state.lens = c->lens;
            state.drs = c->drs; state.sharpen = c->sharpen; state.split = DISPLAY_W / 2;
            state.lens_x = LENS_X; state.lens_y = LENS_Y; state.auto_orbit = 1; state.paused = 0;
            if (state.chapter == CHAPTER_QUALITY_MODES)  /* Quality, Balanced, Performance, Ultra Performance */
                state.quality = Q_QUALITY + (int)fmin(3.0, elapsed / (c->seconds * 250.0));
            if (state.chapter == CHAPTER_SHARPENING)
                state.sharpness = (float)(0.5 - 0.5 * cos(elapsed / (c->seconds * 1000.0) * 2.0 * PI));
            if (state.chapter != reported_chapter) {
                report("FSR4_SHOWCASE_CHAPTER index=%d title=%s\n", state.chapter, c->title);
                reported_chapter = state.chapter;
            }
        } else {
            reported_chapter = -1;
        }
        if (state.lens) {  /* the right stick moves the lens while it is shown, else the divider */
            const float reach = VIEWS[state.view].left != VIEWS[state.view].right ? 2 * LENS_RADIUS + 24.0f
                                                                                  : LENS_RADIUS + 20.0f;
            state.lens_x = fminf(DISPLAY_W - reach, fmaxf(reach, state.lens_x + rx * 12.0f));
            state.lens_y = fminf(DISPLAY_H - LENS_RADIUS - 150.0f,
                                 fmaxf(LENS_RADIUS + 20.0f, state.lens_y + ry * 12.0f));
        } else {
            state.split = fminf(DISPLAY_W - 200.0f, fmaxf(200.0f, state.split + rx * 12.0f));
        }

        if (state.output != target.output || state.drs != target.drs) {
            if (setup_target(state.output, state.drs)) return 1;
            reset = 1;
            last_hud = 0;
        }

        /* Camera and render size. */
        if (!state.paused) scene_time += dt / 1000.0;
        if (state.auto_orbit && !state.paused) camera[0] += 0.0035f;
        camera[0] += lx * 0.03f;
        camera[1] = fminf(1.3f, fmaxf(0.05f, camera[1] - ly * 0.02f));
        camera[2] = fminf(30.0f, fmaxf(3.0f, camera[2] * (1.0f + (l2 - r2) * 0.02f)));
        camera[3] = (float)scene_time;
        if (frame == 0) memcpy(previous, camera, sizeof(camera));
        uint32_t rw, rh;
        render_size(state.output, state.quality, &rw, &rh);
        if (state.drs) {  /* between the mode's size and half of it, once every 4 s */
            const double scale = 0.75 + 0.25 * cos(frame_start / 4000.0 * 2.0 * PI);
            rw = (uint32_t)(rw * scale); rh = (uint32_t)(rh * scale);
        }
        const uint32_t ow = OUTPUTS[state.output].w, oh = OUTPUTS[state.output].h;
        const int native = VIEWS[state.view].left == SRC_NATIVE || VIEWS[state.view].right == SRC_NATIVE;

        /* The scene at render size with jitter, and at output size without for a native view. */
        float jx, jy;
        ps5fsr4_jitter_offset(jitter_index++, ps5fsr4_jitter_phase_count(rw, ow), &jx, &jy);
        struct { float camera[4], previous[4], frame[4]; } scene_params = {
            {camera[0], camera[1], camera[2], camera[3]}, {previous[0], previous[1], previous[2], previous[3]},
            {jx, jy, (float)rw, (float)rh}};
        double scene_ms, fsr_ms, compose_ms;
        if (begin()) return 1;
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, scene.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, scene.layout, 0, 1, &scene.set, 0, NULL);
        vkCmdPushConstants(cmd, scene.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(scene_params), &scene_params);
        vkCmdDispatch(cmd, (rw + 7) / 8, (rh + 7) / 8, 1);
        if (native) {
            struct { float camera[4], previous[4], frame[4]; } native_params = {
                {camera[0], camera[1], camera[2], camera[3]}, {camera[0], camera[1], camera[2], camera[3]},
                {0.0f, 0.0f, (float)ow, (float)oh}};
            vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, native_scene.pipeline);
            vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, native_scene.layout, 0, 1,
                                    &native_scene.set, 0, NULL);
            vkCmdPushConstants(cmd, native_scene.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(native_params),
                               &native_params);
            vkCmdDispatch(cmd, (ow + 7) / 8, (oh + 7) / 8, 1);
        }
        if (submit(&scene_ms)) return 1;

        /* FSR4. */
        if (begin()) return 1;
        memory_barrier(VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                       VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        ps5fsr4_dispatch_desc dd = {.struct_size = sizeof(dd), .command_buffer = cmd,
            .color = target.color.view, .depth = target.depth.view, .motion_vectors = target.motion.view,
            .output = target.upscaled.view, .color_layout = VK_IMAGE_LAYOUT_GENERAL,
            .depth_layout = VK_IMAGE_LAYOUT_GENERAL, .motion_vectors_layout = VK_IMAGE_LAYOUT_GENERAL,
            .render_width = rw, .render_height = rh, .jitter_x = jx, .jitter_y = jy,
            .motion_vector_scale_x = (float)rw, .motion_vector_scale_y = (float)rh, .pre_exposure = 1.0f,
            .frame_time_delta_ms = (float)dt, .camera_near = 0.1f, .camera_far = 200.0f,
            .camera_fov_vertical = 1.0471976f, .reset = reset, .enable_sharpening = state.sharpen,
            .sharpness = state.sharpness};
        ps5fsr4_result fr = ps5fsr4_dispatch(target.context, &dd);
        if (fr) { report("FSR4_SHOWCASE_ERROR dispatch=%d\n", (int)fr); return 1; }
        if (submit(&fsr_ms)) return 1;
        reset = 0;

        /* The HUD, redrawn four times a second and whenever the setup changes. */
        stats.render_w = rw; stats.render_h = rh;
        if (frame_start - last_hud > 250.0 || pressed || (state.tour && frame_start - state.chapter_start < 20.0)) {
            const double hud_start = now_ms();
            draw_hud(hud);
            CHECK(upload_hud(hud, &overlay));
            hud_ms = fmax(hud_ms, now_ms() - hud_start);
            last_hud = frame_start;
        }

        /* Compose and present. A screenshot build saves each chapter halfway through the first tour. */
        const int shot = FSR4_SHOWCASE_SCREENSHOTS && state.tour && shots_taken == (uint32_t)state.chapter &&
                         frame_start - state.chapter_start > TOUR[state.chapter].seconds * 500.0;
        const uint32_t slot = frame & 1;
        const int zoom = state.lens ? state.lens_zoom : 0;
        struct { int32_t view[4], lens[4], sizes[4]; } compose_params = {
            {VIEWS[state.view].left, VIEWS[state.view].right, (int32_t)state.split, zoom},
            {(int32_t)state.lens_x, (int32_t)state.lens_y, LENS_RADIUS, shot},
            {(int32_t)rw, (int32_t)rh, (int32_t)ow, (int32_t)oh}};
        if (begin()) return 1;
        if (frame) {  /* the previous frame's draw left the composed frame read-only */
            image_barrier(composed.image, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
                          VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_SHADER_WRITE_BIT,
                          VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        }
        memory_barrier(VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                       VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, compose.pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, compose.layout, 0, 1, &compose.set, 0, NULL);
        vkCmdPushConstants(cmd, compose.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(compose_params),
                           &compose_params);
        vkCmdDispatch(cmd, DISPLAY_W / 8, (DISPLAY_H + 7) / 8, 1);
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
        const double present_start = now_ms();
        VkResult pr = ps5vkPresentFrame(surface, slot, (uint64_t)frame + 1);
        if (pr != VK_SUCCESS) { report("FSR4_SHOWCASE_ERROR present=%d\n", (int)pr); return 1; }
        sum_present += now_ms() - present_start;
        if (shot) {
            char path[256];
            snprintf(path, sizeof(path), "%s/fsr4-showcase-%02u.bgra", output_root(), shots_taken);
            VkMappedMemoryRange range = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
                                         .memory = screenshot.memory, .size = VK_WHOLE_SIZE};
            CHECK(vkInvalidateMappedMemoryRanges(device, 1, &range));
            FILE *f = fopen(path, "wb");
            if (f) { fwrite(screenshot.mapped, 1, (size_t)DISPLAY_W * DISPLAY_H * 4, f); fclose(f); }
            report("FSR4_SHOWCASE_SCREENSHOT chapter=%u\n", shots_taken);
            if (++shots_taken == CHAPTERS) report("FSR4_SHOWCASE_TOUR_DONE\n");
        }

        /* Statistics, smoothed for the HUD and logged every 120 frames. */
        const double frame_ms = now_ms() - frame_start;
        stats.fsr4_ms = frame ? stats.fsr4_ms * 0.9 + fsr_ms * 0.1 : fsr_ms;
        stats.scene_ms = frame ? stats.scene_ms * 0.9 + scene_ms * 0.1 : scene_ms;
        stats.compose_ms = frame ? stats.compose_ms * 0.9 + compose_ms * 0.1 : compose_ms;
        stats.frame_ms = frame ? stats.frame_ms * 0.9 + dt * 0.1 : dt;
        sum_fsr += fsr_ms; sum_scene += scene_ms; sum_compose += compose_ms; sum_frame += frame_ms;
        if (frame % 24 >= 8) {
            step_fsr += fsr_ms; step_scene += scene_ms; step_compose += compose_ms; step_frame += frame_ms;
            ++step_frames;
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
    for (;;) usleep(100000);
}
