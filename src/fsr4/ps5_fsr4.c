/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Native FSR 4.1.1 INT8 runtime: persistent pipelines, model data, history and
 * per-dispatch constants, recorded into the application's command buffer.
 */
#include <ps5fsr4/ps5_fsr4.h>

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "fsr4_tables.h"
#include "fsr4_passes.h"
#include "fsr4_layout.h"

struct fsr4_image {
    VkImage image;
    VkDeviceMemory memory;
    VkImageView view;
    uint32_t width, height;
};

struct fsr4_buffer {
    VkBuffer buffer;
    VkDeviceMemory memory;
    VkDeviceSize bytes;
    void *mapped;
};

struct fsr4_pipeline {
    VkDescriptorSetLayout set_layouts[FSR4_DESCRIPTOR_SETS];
    VkPipelineLayout layout;
    VkPipeline pipeline;
    VkDescriptorSet sets[FSR4_DESCRIPTOR_SETS];
};

struct ps5fsr4_context {
    ps5fsr4_context_desc desc;
    VkPhysicalDeviceMemoryProperties memory;
    struct fsr4_layout layout;
    struct fsr4_image images[FSR4_ROLE_COUNT];
    struct fsr4_buffer weights, scratch, constants;
    VkDeviceSize constant_stride;
    VkSampler sampler;
    VkDescriptorPool pool;
    struct fsr4_pipeline pipelines[FSR4_PASS_COUNT];
    int initialized;
    float previous_pre_exposure;
};

static const VkAllocationCallbacks *allocator(const ps5fsr4_context *c)
{
    return c->desc.allocator;
}

static int memory_type(const ps5fsr4_context *c, uint32_t bits, VkMemoryPropertyFlags wanted)
{
    for (uint32_t i = 0; i < c->memory.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (c->memory.memoryTypes[i].propertyFlags & wanted) == wanted)
            return (int)i;
    return -1;
}

static ps5fsr4_result allocate(ps5fsr4_context *c, const VkMemoryRequirements *req,
                               VkMemoryPropertyFlags preferred, VkMemoryPropertyFlags required,
                               VkDeviceMemory *memory)
{
    int type = memory_type(c, req->memoryTypeBits, preferred | required);
    if (type < 0) type = memory_type(c, req->memoryTypeBits, required);
    if (type < 0) return PS5FSR4_ERROR_UNSUPPORTED;
    VkMemoryAllocateInfo info = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req->size, (uint32_t)type};
    VkResult r = vkAllocateMemory(c->desc.device, &info, allocator(c), memory);
    return r == VK_SUCCESS ? PS5FSR4_OK :
        r == VK_ERROR_OUT_OF_HOST_MEMORY || r == VK_ERROR_OUT_OF_DEVICE_MEMORY ?
        PS5FSR4_ERROR_OUT_OF_MEMORY : PS5FSR4_ERROR_VULKAN;
}

static ps5fsr4_result create_buffer(ps5fsr4_context *c, struct fsr4_buffer *b, VkDeviceSize bytes,
                                    VkBufferUsageFlags usage, int host)
{
    VkBufferCreateInfo info = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    info.size = bytes;
    info.usage = usage;
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (vkCreateBuffer(c->desc.device, &info, allocator(c), &b->buffer) != VK_SUCCESS)
        return PS5FSR4_ERROR_VULKAN;
    b->bytes = bytes;
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(c->desc.device, b->buffer, &req);
    ps5fsr4_result r = host ?
        allocate(c, &req, 0, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, &b->memory) :
        allocate(c, &req, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 0, &b->memory);
    if (r) return r;
    if (vkBindBufferMemory(c->desc.device, b->buffer, b->memory, 0) != VK_SUCCESS)
        return PS5FSR4_ERROR_VULKAN;
    if (host && vkMapMemory(c->desc.device, b->memory, 0, VK_WHOLE_SIZE, 0, &b->mapped) != VK_SUCCESS)
        return PS5FSR4_ERROR_VULKAN;
    return PS5FSR4_OK;
}

static ps5fsr4_result create_image(ps5fsr4_context *c, struct fsr4_image *img, VkFormat format,
                                   uint32_t width, uint32_t height, int sampled)
{
    VkImageCreateInfo info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    info.imageType = VK_IMAGE_TYPE_2D;
    info.format = format;
    info.extent = (VkExtent3D){width, height, 1};
    info.mipLevels = 1;
    info.arrayLayers = 1;
    info.samples = VK_SAMPLE_COUNT_1_BIT;
    info.tiling = VK_IMAGE_TILING_OPTIMAL;
    /* ps5vk storage images: storage and both transfer roles, plus sampling when read as a texture. */
    info.usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT |
                 (sampled ? VK_IMAGE_USAGE_SAMPLED_BIT : 0u);
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    img->width = width;
    img->height = height;
    if (vkCreateImage(c->desc.device, &info, allocator(c), &img->image) != VK_SUCCESS)
        return PS5FSR4_ERROR_VULKAN;
    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(c->desc.device, img->image, &req);
    ps5fsr4_result r = allocate(c, &req, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 0, &img->memory);
    if (r) return r;
    if (vkBindImageMemory(c->desc.device, img->image, img->memory, 0) != VK_SUCCESS)
        return PS5FSR4_ERROR_VULKAN;
    VkImageViewCreateInfo view = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    view.image = img->image;
    view.viewType = VK_IMAGE_VIEW_TYPE_2D;
    view.format = format;
    view.subresourceRange = (VkImageSubresourceRange){VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    return vkCreateImageView(c->desc.device, &view, allocator(c), &img->view) == VK_SUCCESS ?
        PS5FSR4_OK : PS5FSR4_ERROR_VULKAN;
}

static VkDescriptorType descriptor_type(uint8_t kind)
{
    switch (kind) {
    case FSR4_DESCRIPTOR_SAMPLED_IMAGE: return VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;
    case FSR4_DESCRIPTOR_STORAGE_IMAGE: return VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
    case FSR4_DESCRIPTOR_STORAGE_BUFFER: return VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    case FSR4_DESCRIPTOR_UNIFORM_BUFFER: return VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    default: return VK_DESCRIPTOR_TYPE_SAMPLER;
    }
}

static ps5fsr4_result create_pipeline(ps5fsr4_context *c, uint32_t index)
{
    const struct fsr4_pass_info *p = &fsr4_passes[index];
    struct fsr4_pipeline *pl = &c->pipelines[index];
    for (uint32_t set = 0; set < FSR4_DESCRIPTOR_SETS; ++set) {
        VkDescriptorSetLayoutBinding bindings[32];
        uint32_t count = 0;
        for (uint32_t i = 0; i < p->binding_count; ++i) {
            const struct fsr4_binding *b = &p->bindings[i];
            if (b->set != set) continue;
            if (count == 32) return PS5FSR4_ERROR_UNSUPPORTED;
            bindings[count++] = (VkDescriptorSetLayoutBinding){b->binding, descriptor_type(b->descriptor),
                                                               1, VK_SHADER_STAGE_COMPUTE_BIT, NULL};
        }
        VkDescriptorSetLayoutCreateInfo info = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
        info.bindingCount = count;
        info.pBindings = count ? bindings : NULL;
        if (vkCreateDescriptorSetLayout(c->desc.device, &info, allocator(c), &pl->set_layouts[set]) != VK_SUCCESS)
            return PS5FSR4_ERROR_VULKAN;
    }
    VkPipelineLayoutCreateInfo layout = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    layout.setLayoutCount = FSR4_DESCRIPTOR_SETS;
    layout.pSetLayouts = pl->set_layouts;
    if (vkCreatePipelineLayout(c->desc.device, &layout, allocator(c), &pl->layout) != VK_SUCCESS)
        return PS5FSR4_ERROR_VULKAN;
    VkShaderModuleCreateInfo module_info = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    module_info.codeSize = p->code_bytes;
    module_info.pCode = p->code;
    VkShaderModule module;
    if (vkCreateShaderModule(c->desc.device, &module_info, allocator(c), &module) != VK_SUCCESS)
        return PS5FSR4_ERROR_VULKAN;
    VkComputePipelineCreateInfo info = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
    info.stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    info.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    info.stage.module = module;
    info.stage.pName = "main";
    info.layout = pl->layout;
    VkResult r = vkCreateComputePipelines(c->desc.device, c->desc.pipeline_cache, 1, &info, allocator(c), &pl->pipeline);
    vkDestroyShaderModule(c->desc.device, module, allocator(c));
    if (r != VK_SUCCESS) return PS5FSR4_ERROR_VULKAN;
    VkDescriptorSetAllocateInfo sets = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    sets.descriptorPool = c->pool;
    sets.descriptorSetCount = FSR4_DESCRIPTOR_SETS;
    sets.pSetLayouts = pl->set_layouts;
    return vkAllocateDescriptorSets(c->desc.device, &sets, pl->sets) == VK_SUCCESS ?
        PS5FSR4_OK : PS5FSR4_ERROR_VULKAN;
}

static int application_role(uint8_t role)
{
    return role == FSR4_ROLE_COLOR || role == FSR4_ROLE_DEPTH ||
           role == FSR4_ROLE_MOTION_VECTORS || role == FSR4_ROLE_OUTPUT;
}

/* Bind every descriptor, taking application views from desc when given. */
static void write_descriptors(ps5fsr4_context *c, uint32_t index, const ps5fsr4_dispatch_desc *desc)
{
    const struct fsr4_pass_info *p = &fsr4_passes[index];
    struct fsr4_pipeline *pl = &c->pipelines[index];
    for (uint32_t i = 0; i < p->binding_count; ++i) {
        const struct fsr4_binding *b = &p->bindings[i];
        if (application_role(b->role) != (desc != NULL)) continue;
        VkWriteDescriptorSet w = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
        VkDescriptorImageInfo image = {c->sampler, VK_NULL_HANDLE, VK_IMAGE_LAYOUT_GENERAL};
        VkDescriptorBufferInfo buffer = {VK_NULL_HANDLE, 0, VK_WHOLE_SIZE};
        w.dstSet = pl->sets[b->set];
        w.dstBinding = b->binding;
        w.descriptorCount = 1;
        w.descriptorType = descriptor_type(b->descriptor);
        switch (b->role) {
        case FSR4_ROLE_COLOR: image.imageView = desc->color; image.imageLayout = desc->color_layout; break;
        case FSR4_ROLE_DEPTH: image.imageView = desc->depth; image.imageLayout = desc->depth_layout; break;
        case FSR4_ROLE_MOTION_VECTORS:
            image.imageView = desc->motion_vectors; image.imageLayout = desc->motion_vectors_layout; break;
        case FSR4_ROLE_OUTPUT: image.imageView = desc->output; break;
        case FSR4_ROLE_WEIGHTS: buffer.buffer = c->weights.buffer; break;
        case FSR4_ROLE_SCRATCH: buffer.buffer = c->scratch.buffer; break;
        case FSR4_ROLE_CONSTANTS:
            buffer.buffer = c->constants.buffer;
            buffer.offset = index * c->constant_stride;
            buffer.range = FSR4_CONSTANT_BLOCK_BYTES;
            break;
        case FSR4_ROLE_SAMPLER: break;
        default: image.imageView = c->images[b->role].view; break;
        }
        if (w.descriptorType == VK_DESCRIPTOR_TYPE_STORAGE_BUFFER ||
            w.descriptorType == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER)
            w.pBufferInfo = &buffer;
        else
            w.pImageInfo = &image;
        vkUpdateDescriptorSets(c->desc.device, 1, &w, 0, NULL);
    }
}

static int valid_context_desc(const ps5fsr4_context_desc *d)
{
    return d && d->struct_size == sizeof(*d) && d->physical_device && d->device &&
           d->max_render_width && d->max_render_height && d->output_width && d->output_height &&
           d->max_render_width <= d->output_width && d->max_render_height <= d->output_height &&
           !(d->flags & ~(uint32_t)(PS5FSR4_FLAG_HIGH_DYNAMIC_RANGE | PS5FSR4_FLAG_AUTO_EXPOSURE));
}

ps5fsr4_result ps5fsr4_get_memory_requirements(const ps5fsr4_context_desc *desc,
                                               ps5fsr4_memory_requirements *requirements)
{
    struct fsr4_layout layout;
    if (!valid_context_desc(desc) || !requirements) return PS5FSR4_ERROR_INVALID_ARGUMENT;
    if (fsr4_layout_init(&layout, desc->max_render_width, desc->max_render_height,
                         desc->output_width, desc->output_height))
        return PS5FSR4_ERROR_UNSUPPORTED;
    VkDeviceSize output = (VkDeviceSize)desc->output_width * desc->output_height;
    requirements->device_bytes = layout.scratch_bytes + FSR4_WEIGHTS_BYTES + output * (8 + 8 + 4) +
        (VkDeviceSize)layout.luma_width * layout.luma_height * 4 + 16;
    requirements->host_visible_bytes = (VkDeviceSize)FSR4_PASS_COUNT * 512;
    return PS5FSR4_OK;
}

void ps5fsr4_context_destroy(ps5fsr4_context *c)
{
    if (!c) return;
    VkDevice d = c->desc.device;
    for (uint32_t i = 0; i < FSR4_PASS_COUNT; ++i) {
        struct fsr4_pipeline *pl = &c->pipelines[i];
        if (pl->pipeline) vkDestroyPipeline(d, pl->pipeline, allocator(c));
        if (pl->layout) vkDestroyPipelineLayout(d, pl->layout, allocator(c));
        for (uint32_t s = 0; s < FSR4_DESCRIPTOR_SETS; ++s)
            if (pl->set_layouts[s]) vkDestroyDescriptorSetLayout(d, pl->set_layouts[s], allocator(c));
    }
    if (c->pool) vkDestroyDescriptorPool(d, c->pool, allocator(c));
    if (c->sampler) vkDestroySampler(d, c->sampler, allocator(c));
    for (uint32_t i = 0; i < FSR4_ROLE_COUNT; ++i) {
        if (c->images[i].view) vkDestroyImageView(d, c->images[i].view, allocator(c));
        if (c->images[i].image) vkDestroyImage(d, c->images[i].image, allocator(c));
        if (c->images[i].memory) vkFreeMemory(d, c->images[i].memory, allocator(c));
    }
    struct fsr4_buffer *buffers[] = {&c->weights, &c->scratch, &c->constants};
    for (uint32_t i = 0; i < 3; ++i) {
        if (buffers[i]->buffer) vkDestroyBuffer(d, buffers[i]->buffer, allocator(c));
        if (buffers[i]->memory) vkFreeMemory(d, buffers[i]->memory, allocator(c));
    }
    free(c);
}

ps5fsr4_result ps5fsr4_context_create(const ps5fsr4_context_desc *desc, ps5fsr4_context **context)
{
    if (!valid_context_desc(desc) || !context) return PS5FSR4_ERROR_INVALID_ARGUMENT;
    *context = NULL;
    ps5fsr4_context *c = calloc(1, sizeof(*c));
    if (!c) return PS5FSR4_ERROR_OUT_OF_MEMORY;
    c->desc = *desc;
    ps5fsr4_result r = PS5FSR4_OK;
    if (fsr4_layout_init(&c->layout, desc->max_render_width, desc->max_render_height,
                         desc->output_width, desc->output_height)) {
        r = PS5FSR4_ERROR_UNSUPPORTED;
        goto fail;
    }
    vkGetPhysicalDeviceMemoryProperties(desc->physical_device, &c->memory);
    VkPhysicalDeviceProperties properties;
    vkGetPhysicalDeviceProperties(desc->physical_device, &properties);
    VkDeviceSize align = properties.limits.minUniformBufferOffsetAlignment;
    if (!align) align = 1;
    c->constant_stride = (FSR4_CONSTANT_BLOCK_BYTES + align - 1) / align * align;

    const uint32_t ow = desc->output_width, oh = desc->output_height;
    if ((r = create_image(c, &c->images[FSR4_ROLE_HISTORY], VK_FORMAT_R16G16B16A16_SFLOAT, ow, oh, 1)) ||
        (r = create_image(c, &c->images[FSR4_ROLE_HISTORY_REPROJECTED], VK_FORMAT_R16G16B16A16_SFLOAT, ow, oh, 1)) ||
        (r = create_image(c, &c->images[FSR4_ROLE_RECURRENT], VK_FORMAT_R8G8B8A8_UNORM, ow, oh, 1)) ||
        (r = create_image(c, &c->images[FSR4_ROLE_LUMA_MIP5], VK_FORMAT_R32_SFLOAT,
                          c->layout.luma_width, c->layout.luma_height, 0)) ||
        (r = create_image(c, &c->images[FSR4_ROLE_AUTO_EXPOSURE], VK_FORMAT_R32_SFLOAT, 2, 1, 1)) ||
        (r = create_image(c, &c->images[FSR4_ROLE_SPD_COUNTER], VK_FORMAT_R32_UINT, 1, 1, 0)) ||
        (r = create_buffer(c, &c->weights, FSR4_WEIGHTS_BYTES, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT, 1)) ||
        (r = create_buffer(c, &c->scratch, c->layout.scratch_bytes,
                           VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT, 0)) ||
        (r = create_buffer(c, &c->constants, c->constant_stride * FSR4_PASS_COUNT,
                           VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT, 1)))
        goto fail;
    memcpy(c->weights.mapped, fsr4_weights, FSR4_WEIGHTS_BYTES);

    VkSamplerCreateInfo sampler = {.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
    sampler.magFilter = sampler.minFilter = VK_FILTER_LINEAR;
    sampler.mipmapMode = VK_SAMPLER_MIPMAP_MODE_LINEAR;
    sampler.addressModeU = sampler.addressModeV = sampler.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sampler.maxAnisotropy = 1;
    sampler.maxLod = 1000;
    if (vkCreateSampler(desc->device, &sampler, allocator(c), &c->sampler) != VK_SUCCESS) {
        r = PS5FSR4_ERROR_VULKAN;
        goto fail;
    }
    uint32_t counts[5] = {0};
    for (uint32_t i = 0; i < FSR4_PASS_COUNT; ++i)
        for (uint32_t j = 0; j < fsr4_passes[i].binding_count; ++j)
            ++counts[fsr4_passes[i].bindings[j].descriptor];
    VkDescriptorPoolSize sizes[5];
    uint32_t size_count = 0;
    for (uint8_t k = 0; k < 5; ++k)
        if (counts[k]) sizes[size_count++] = (VkDescriptorPoolSize){descriptor_type(k), counts[k]};
    VkDescriptorPoolCreateInfo pool = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    pool.maxSets = FSR4_PASS_COUNT * FSR4_DESCRIPTOR_SETS;
    pool.poolSizeCount = size_count;
    pool.pPoolSizes = sizes;
    if (vkCreateDescriptorPool(desc->device, &pool, allocator(c), &c->pool) != VK_SUCCESS) {
        r = PS5FSR4_ERROR_VULKAN;
        goto fail;
    }
    for (uint32_t i = 0; i < FSR4_PASS_COUNT; ++i) {
        if ((r = create_pipeline(c, i))) goto fail;
        write_descriptors(c, i, NULL);
    }
    *context = c;
    return PS5FSR4_OK;
fail:
    ps5fsr4_context_destroy(c);
    return r;
}

static void write_constants(ps5fsr4_context *c, const ps5fsr4_dispatch_desc *d)
{
    uint32_t spd[FSR4_CONSTANT_BLOCK_BYTES / 4], mlsr[FSR4_CONSTANT_BLOCK_BYTES / 4];
    uint32_t tensor[FSR4_CONSTANT_BLOCK_BYTES / 4];
    fsr4_spd_constants(&c->layout, d->render_width, d->render_height, spd);
    int reset = d->reset || !c->initialized;
    fsr4_mlsr_constants(&c->layout, d, reset, reset ? 0.0f : c->previous_pre_exposure, mlsr);
    fsr4_tensor_constants(&c->layout, tensor);
    for (uint32_t i = 0; i < FSR4_PASS_COUNT; ++i) {
        const uint32_t *source = NULL;
        switch (fsr4_passes[i].constants) {
        case FSR4_CONSTANTS_SPD: source = spd; break;
        case FSR4_CONSTANTS_MLSR: source = mlsr; break;
        case FSR4_CONSTANTS_TENSOR: source = tensor; break;
        default: continue;
        }
        memcpy((char *)c->constants.mapped + i * c->constant_stride, source, FSR4_CONSTANT_BLOCK_BYTES);
    }
}

static void clear_state(ps5fsr4_context *c, VkCommandBuffer cmd)
{
    static const uint8_t internal[] = {FSR4_ROLE_HISTORY, FSR4_ROLE_HISTORY_REPROJECTED, FSR4_ROLE_RECURRENT,
                                       FSR4_ROLE_LUMA_MIP5, FSR4_ROLE_AUTO_EXPOSURE, FSR4_ROLE_SPD_COUNTER};
    VkImageMemoryBarrier barriers[sizeof(internal)];
    const VkImageSubresourceRange range = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    for (uint32_t i = 0; i < sizeof(internal); ++i) {
        barriers[i] = (VkImageMemoryBarrier){.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
        barriers[i].dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        barriers[i].oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        barriers[i].newLayout = VK_IMAGE_LAYOUT_GENERAL;
        barriers[i].srcQueueFamilyIndex = barriers[i].dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barriers[i].image = c->images[internal[i]].image;
        barriers[i].subresourceRange = range;
    }
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0,
                         0, NULL, 0, NULL, sizeof(internal), barriers);
    /* Zero the scratch buffer, then use it as the zero source for every image:
     * portable, and within the transfer shapes ps5vk executes (it clears only
     * 32-bit storage formats directly). */
    vkCmdFillBuffer(cmd, c->scratch.buffer, 0, VK_WHOLE_SIZE, 0);
    VkMemoryBarrier filled = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL, VK_ACCESS_TRANSFER_WRITE_BIT,
                              VK_ACCESS_TRANSFER_READ_BIT};
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0,
                         1, &filled, 0, NULL, 0, NULL);
    for (uint32_t i = 0; i < sizeof(internal); ++i) {
        const struct fsr4_image *img = &c->images[internal[i]];
        VkBufferImageCopy region = {0, 0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, {0, 0, 0},
                                    {img->width, img->height, 1}};
        vkCmdCopyBufferToImage(cmd, c->scratch.buffer, img->image, VK_IMAGE_LAYOUT_GENERAL, 1, &region);
    }
    VkMemoryBarrier done = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL, VK_ACCESS_TRANSFER_WRITE_BIT,
                            VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT};
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, 0,
                         1, &done, 0, NULL, 0, NULL);
}

/* The PS5 libc isfinite() macro is not warning-clean under clang. */
static int finite_float(float x)
{
    return x - x == 0.0f;
}

ps5fsr4_result ps5fsr4_dispatch(ps5fsr4_context *c, const ps5fsr4_dispatch_desc *d)
{
    return ps5fsr4_dispatch_passes(c, d, 0, FSR4_PASS_COUNT);
}

ps5fsr4_result ps5fsr4_dispatch_passes(ps5fsr4_context *c, const ps5fsr4_dispatch_desc *d,
                                       uint32_t first_pass, uint32_t pass_count)
{
    if (!c || !d || first_pass >= FSR4_PASS_COUNT || !pass_count || pass_count > FSR4_PASS_COUNT - first_pass)
        return PS5FSR4_ERROR_INVALID_ARGUMENT;
    const uint32_t end = first_pass + pass_count;
    if (!c || !d || d->struct_size != sizeof(*d) || !d->command_buffer || !d->color || !d->depth ||
        !d->motion_vectors || !d->output || !d->render_width || !d->render_height ||
        d->render_width > c->desc.max_render_width || d->render_height > c->desc.max_render_height ||
        !(d->pre_exposure > 0.0f) || !finite_float(d->jitter_x) || !finite_float(d->jitter_y))
        return PS5FSR4_ERROR_INVALID_ARGUMENT;
    if (fsr4_layout_supports_render(&c->layout, d->render_width, d->render_height))
        return PS5FSR4_ERROR_UNSUPPORTED;
    VkCommandBuffer cmd = d->command_buffer;
    if (first_pass == 0) {  /* a frame starts: constants and bindings for all of its passes */
        write_constants(c, d);
        for (uint32_t i = 0; i < FSR4_PASS_COUNT; ++i)
            write_descriptors(c, i, d);
        if (!c->initialized)
            clear_state(c, cmd);
    }
    const VkMemoryBarrier between = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL, VK_ACCESS_SHADER_WRITE_BIT,
                                     VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT};
    for (uint32_t i = first_pass; i < end; ++i) {
        uint32_t groups[3];
        const struct fsr4_pass_info *p = &fsr4_passes[i];
        fsr4_pass_groups(&c->layout, p->groups, p->tensor, p->limit_width, p->limit_height,
                         d->render_width, d->render_height, groups);
        if (i > first_pass)
            vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                                 0, 1, &between, 0, NULL, 0, NULL);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, c->pipelines[i].pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, c->pipelines[i].layout, 0,
                                FSR4_DESCRIPTOR_SETS, c->pipelines[i].sets, 0, NULL);
        vkCmdDispatch(cmd, groups[0], groups[1], groups[2]);
    }
    if (end == FSR4_PASS_COUNT) {
        c->initialized = 1;
        c->previous_pre_exposure = d->pre_exposure;
    }
    return PS5FSR4_OK;
}

uint32_t ps5fsr4_pass_count(void)
{
    return FSR4_PASS_COUNT;
}

uint32_t ps5fsr4_jitter_phase_count(uint32_t render_width, uint32_t output_width)
{
    if (!render_width) return 0;
    double ratio = (double)output_width / render_width;
    return (uint32_t)(8.0 * ratio * ratio + 0.5);
}

static float halton(uint32_t index, uint32_t base)
{
    float f = 1.0f, result = 0.0f;
    for (uint32_t i = index; i > 0; i /= base) {
        f /= (float)base;
        result += f * (float)(i % base);
    }
    return result;
}

void ps5fsr4_jitter_offset(uint32_t index, uint32_t phase_count, float *x, float *y)
{
    uint32_t i = phase_count ? index % phase_count + 1 : 1;
    if (x) *x = halton(i, 2) - 0.5f;
    if (y) *y = halton(i, 3) - 0.5f;
}
