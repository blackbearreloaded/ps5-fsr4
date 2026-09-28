/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Native replay of one captured FSR4 dispatch, then shell-mediated closure.
 * This is not a complete FSR4 frame.
 */
#include <ps5vk/ps5vk.h>
#include <stdint.h>
#include <stdarg.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "fsr4_clear_fixture.h"

static FILE *log_file;
extern int sceKernelDebugOutText(int level, const char *text);
static void report(const char *format, ...)
{
    char message[2048];
    va_list args;
    va_start(args, format);
    vsnprintf(message, sizeof(message), format, args);
    va_end(args);
    sceKernelDebugOutText(0, message);
    if (log_file) { fputs(message, log_file); fflush(log_file); }
}

#define CHECK(call) do { \
    VkResult rc = (call); \
    report("%s = %d\n", #call, (int)rc); \
    if (rc != VK_SUCCESS) return 1; \
} while (0)

static void *asset(const char *name, void *data, size_t bytes)
{
    char path[256];
    if (snprintf(path, sizeof(path), "/app0/assets/%s", name) >= (int)sizeof(path))
        return NULL;
    FILE *f = fopen(path, "rb");
    if (!f) { report("ASSET open %s errno=%d\n", path, errno); return NULL; }
    size_t got = data ? fread(data, 1, bytes, f) : 0;
    int tail = data ? fgetc(f) : 0;
    int ok = data && got == bytes && tail == EOF;
    report("ASSET %s allocated=%d got=%zu expected=%zu tail=%d error=%d errno=%d\n",
           name, data != NULL, got, bytes, tail, ferror(f), errno);
    fclose(f);
    if (!ok) return NULL;
    return data;
}

/* Diagnostic checksum only; the final oracle remains a full byte comparison. */
static uint32_t tensor_crc32(const uint8_t *data, size_t bytes)
{
    uint32_t table[256], crc = UINT32_MAX;
    for (unsigned i = 0; i < 256; ++i) {
        uint32_t value = i;
        for (unsigned bit = 0; bit < 8; ++bit)
            value = (value >> 1) ^ (UINT32_C(0xedb88320) & (0u - (value & 1u)));
        table[i] = value;
    }
    for (size_t i = 0; i < bytes; ++i)
        crc = table[(crc ^ data[i]) & 255u] ^ (crc >> 8);
    return ~crc;
}

extern int fsr4_native_heap_init(void);
extern size_t fsr4_native_heap_capacity(void);
int main(void)
{
    log_file = fopen("/download0/fsr4-clear-result.txt", "w");
    if (tensor_crc32((const uint8_t *)"123456789", 9) != UINT32_C(0xcbf43926)) {
        report("FAIL checksum self-check\n"); return 1;
    }
    int heap_rc = fsr4_native_heap_init();
    report("FSR4_NATIVE_HEAP rc=%d bytes=%zu\n", heap_rc, fsr4_native_heap_capacity());
    if (heap_rc) return 1;
    report("FSR4_CLEAR_BEGIN fixture=%s complete_fsr4=0\n", FSR4_FIXTURE_ID);

    uint8_t *heap_check = calloc(1, 32u * 1024u * 1024u);
    if (!heap_check) { report("FSR4_NATIVE_HEAP_CHECK failed=calloc\n"); return 1; }
    for (size_t i = 0; i < 32u * 1024u * 1024u; ++i)
        if (heap_check[i]) { report("FSR4_NATIVE_HEAP_CHECK failed=zero\n"); return 1; }
    heap_check[0] = 0x5a;
    uint8_t *resized = realloc(heap_check, 40u * 1024u * 1024u);
    if (!resized || resized[0] != 0x5a) { report("FSR4_NATIVE_HEAP_CHECK failed=realloc\n"); return 1; }
    free(resized);
    void *aligned = NULL;
    if (posix_memalign(&aligned, 65536, 65536) || ((uintptr_t)aligned & 65535u)) {
        report("FSR4_NATIVE_HEAP_CHECK failed=alignment\n"); return 1;
    }
    free(aligned);
    report("FSR4_NATIVE_HEAP_CHECK PASS\n");
    static uint8_t before_data[FSR4_SCRATCH_BYTES], expected_data[FSR4_SCRATCH_BYTES];
    static uint8_t constants_data[FSR4_CONSTANT_BYTES * FSR4_DISPATCH_COUNT];
    static uint8_t model_data[FSR4_MODEL_BYTES ? FSR4_MODEL_BYTES : 1];
    uint8_t *model = FSR4_MODEL_BYTES ? asset("model.bin", model_data, FSR4_MODEL_BYTES) : model_data;
    uint8_t *before = asset("before.bin", before_data, sizeof(before_data));
    uint8_t *expected = asset("expected.bin", expected_data, sizeof(expected_data));
    uint8_t *constants = asset("constants.bin", constants_data, sizeof(constants_data));
    if (!before || !expected || !constants || !model ||
        !memcmp(before, expected, FSR4_SCRATCH_BYTES)) {
        report("FAIL invalid or ineffective fixture\n"); if (log_file) fclose(log_file); return 1;
    }
    VkInstance instance;
    VkInstanceCreateInfo ii = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    CHECK(vkCreateInstance(&ii, NULL, &instance));
    uint32_t count = 1;
    VkPhysicalDevice physical;
    CHECK(vkEnumeratePhysicalDevices(instance, &count, &physical));
    if (count != 1) return 1;
    float priority = 1.0f;
    VkDeviceQueueCreateInfo qi = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = 0, .queueCount = 1, .pQueuePriorities = &priority};
    const char *extension = VK_KHR_STORAGE_BUFFER_STORAGE_CLASS_EXTENSION_NAME;
    VkPhysicalDeviceFeatures enabled = {.shaderInt16 = FSR4_MODEL_BYTES ? VK_TRUE : VK_FALSE};
    VkDeviceCreateInfo di = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1, .pQueueCreateInfos = &qi, .pEnabledFeatures = &enabled,
        .enabledExtensionCount = 1, .ppEnabledExtensionNames = &extension};
    VkDevice device;
    CHECK(vkCreateDevice(physical, &di, NULL, &device));
    VkQueue queue;
    vkGetDeviceQueue(device, 0, 0, &queue);
    VkPhysicalDeviceMemoryProperties properties;
    vkGetPhysicalDeviceMemoryProperties(physical, &properties);

    VkDescriptorSetLayout layouts[3];
    for (unsigned i = 0; i < 3; ++i) {
        VkDescriptorSetLayoutBinding binding = {
            .binding = i == 0 ? 18 : i == 1 ? 11 : 0,
            .descriptorType = i != 2 ? VK_DESCRIPTOR_TYPE_STORAGE_BUFFER :
                                      VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER,
            .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT};
        VkDescriptorSetLayoutCreateInfo li = {
            .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
            .bindingCount = FSR4_MODEL_BYTES ? 1 : !!i, .pBindings = FSR4_MODEL_BYTES ? &binding : i ? &binding : NULL};
        CHECK(vkCreateDescriptorSetLayout(device, &li, NULL, &layouts[i]));
    }
    VkPipelineLayout layout;
    VkPipelineLayoutCreateInfo pli = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 3, .pSetLayouts = layouts};
    CHECK(vkCreatePipelineLayout(device, &pli, NULL, &layout));

    VkBuffer buffers[3];
    VkDeviceMemory memory[3];
    void *mapped[3];
    const unsigned buffer_count = 2 + !!FSR4_MODEL_BYTES;
    VkDeviceSize sizes[3] = {FSR4_SCRATCH_BYTES + 512, FSR4_CONSTANT_BYTES, FSR4_MODEL_BYTES};
    for (unsigned i = 0; i < buffer_count; ++i) {
        VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
            .size = sizes[i], .usage = i == 1 ? VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT :
                                          VK_BUFFER_USAGE_STORAGE_BUFFER_BIT};
        CHECK(vkCreateBuffer(device, &bi, NULL, &buffers[i]));
        VkMemoryRequirements req;
        vkGetBufferMemoryRequirements(device, buffers[i], &req);
        unsigned type = 0;
        for (; type < properties.memoryTypeCount; ++type)
            if ((req.memoryTypeBits & (1u << type)) &&
                (properties.memoryTypes[type].propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT))
                break;
        if (type == properties.memoryTypeCount) return 1;
        VkMemoryAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
            .allocationSize = req.size, .memoryTypeIndex = type};
        CHECK(vkAllocateMemory(device, &ai, NULL, &memory[i]));
        CHECK(vkBindBufferMemory(device, buffers[i], memory[i], 0));
        CHECK(vkMapMemory(device, memory[i], 0, VK_WHOLE_SIZE, 0, &mapped[i]));
        memset(mapped[i], 0xa5, (size_t)sizes[i]);
        memcpy((uint8_t *)mapped[i] + (i ? 0 : 256), i == 2 ? model : i == 1 ? constants : before,
               i == 2 ? FSR4_MODEL_BYTES : i == 1 ? FSR4_CONSTANT_BYTES : FSR4_SCRATCH_BYTES);
        VkMappedMemoryRange flush = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
            .memory = memory[i], .offset = 0, .size = VK_WHOLE_SIZE};
        CHECK(vkFlushMappedMemoryRanges(device, 1, &flush));
    }
    VkDescriptorPoolSize pool_sizes[2] = {
        {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 2}, {VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1}};
    VkDescriptorPoolCreateInfo dpi = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets = 3, .poolSizeCount = 2, .pPoolSizes = pool_sizes};
    VkDescriptorPool pool;
    CHECK(vkCreateDescriptorPool(device, &dpi, NULL, &pool));
    VkDescriptorSet sets[3];
    VkDescriptorSetAllocateInfo da = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool = pool, .descriptorSetCount = 3, .pSetLayouts = layouts};
    CHECK(vkAllocateDescriptorSets(device, &da, sets));
    VkDescriptorBufferInfo db[3] = {
        {buffers[0], 256, FSR4_SCRATCH_BYTES}, {buffers[1], 0, FSR4_CONSTANT_BYTES},
        {FSR4_MODEL_BYTES ? buffers[2] : VK_NULL_HANDLE, 0, FSR4_MODEL_BYTES}};
    for (unsigned i = 0; i < buffer_count; ++i) {
        VkWriteDescriptorSet write = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
            .dstSet = sets[i == 2 ? 0 : i + 1], .dstBinding = i == 2 ? 18 : i == 1 ? 0 : 11, .descriptorCount = 1,
            .descriptorType = i == 1 ? VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER :
                                 VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
            .pBufferInfo = &db[i]};
        vkUpdateDescriptorSets(device, 1, &write, 0, NULL);
    }
    size_t stage_mismatches = 0;
    /* The tensor is uploaded once. Every pass consumes the preceding GPU result. */
    for (unsigned pass = 0; pass < FSR4_DISPATCH_COUNT; ++pass) {
        const struct fsr4_dispatch *dispatch = &fsr4_dispatches[pass];
        report("FSR4_REPLAY_DISPATCH index=%u groups=%u,%u,%u model_bytes=%u\n",
               dispatch->index, dispatch->groups[0], dispatch->groups[1],
               dispatch->groups[2], FSR4_MODEL_BYTES);
        memcpy(mapped[1], constants + pass * FSR4_CONSTANT_BYTES, FSR4_CONSTANT_BYTES);
        VkMappedMemoryRange constants_flush = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
            .memory = memory[1], .offset = 0, .size = VK_WHOLE_SIZE};
        CHECK(vkFlushMappedMemoryRanges(device, 1, &constants_flush));
        VkShaderModule shader;
        VkShaderModuleCreateInfo si = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
            .codeSize = dispatch->code_bytes, .pCode = dispatch->code};
        CHECK(vkCreateShaderModule(device, &si, NULL, &shader));
        VkPipeline pipeline;
        VkComputePipelineCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
            .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                      .stage = VK_SHADER_STAGE_COMPUTE_BIT, .module = shader, .pName = "main"},
            .layout = layout};
        CHECK(vkCreateComputePipelines(device, VK_NULL_HANDLE, 1, &ci, NULL, &pipeline));

        VkCommandPool command_pool;
        VkCommandPoolCreateInfo pi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
            .queueFamilyIndex = 0};
        CHECK(vkCreateCommandPool(device, &pi, NULL, &command_pool));
        VkCommandBuffer command;
        VkCommandBufferAllocateInfo ca = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
            .commandPool = command_pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
            .commandBufferCount = 1};
        CHECK(vkAllocateCommandBuffers(device, &ca, &command));
        VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        CHECK(vkBeginCommandBuffer(command, &begin));
        vkCmdBindPipeline(command, VK_PIPELINE_BIND_POINT_COMPUTE, pipeline);
        vkCmdBindDescriptorSets(command, VK_PIPELINE_BIND_POINT_COMPUTE, layout, 0, 3, sets, 0, NULL);
        VkMemoryBarrier barrier = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
            .srcAccessMask = VK_ACCESS_HOST_WRITE_BIT | VK_ACCESS_SHADER_WRITE_BIT,
            .dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT};
        vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_HOST_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
            VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, 0, 1, &barrier, 0, NULL, 0, NULL);
        vkCmdDispatch(command, dispatch->groups[0], dispatch->groups[1], dispatch->groups[2]);
        barrier.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
        barrier.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
        vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
            VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &barrier, 0, NULL, 0, NULL);
        CHECK(vkEndCommandBuffer(command));
        VkFence fence;
        VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
        CHECK(vkCreateFence(device, &fi, NULL, &fence));
        VkSubmitInfo submit = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
            .commandBufferCount = 1, .pCommandBuffers = &command};
        CHECK(vkQueueSubmit(queue, 1, &submit, fence));
        VkResult waited = vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_C(5000000000));
        report("FSR4_CLEAR_FENCE result=%d\n", (int)waited);

        if (waited != VK_SUCCESS) {
            /* Keep possibly in-flight storage alive until the host closes this title. */
            for (;;) sleep(1);
        }
        vkDestroyFence(device, fence, NULL);
        vkDestroyCommandPool(device, command_pool, NULL);
        vkDestroyPipeline(device, pipeline, NULL);
        vkDestroyShaderModule(device, shader, NULL);
        report("FSR4_REPLAY_RETIRED index=%u\n", dispatch->index);
        VkMappedMemoryRange stage_read = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
            .memory = memory[0], .offset = 0, .size = VK_WHOLE_SIZE};
        CHECK(vkInvalidateMappedMemoryRanges(device, 1, &stage_read));
        uint32_t crc = tensor_crc32((const uint8_t *)mapped[0] + 256, FSR4_SCRATCH_BYTES);
        stage_mismatches += crc != dispatch->expected_crc32;
        report("FSR4_REPLAY_CRC index=%u actual=%08x expected=%08x match=%u\n",
               dispatch->index, crc, dispatch->expected_crc32, crc == dispatch->expected_crc32);
    }
    VkMappedMemoryRange invalidate = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
        .memory = memory[0], .offset = 0, .size = VK_WHOLE_SIZE};
    CHECK(vkInvalidateMappedMemoryRanges(device, 1, &invalidate));
    uint8_t *actual = (uint8_t *)mapped[0];
    size_t mismatches = 0, guards = 0;
    for (size_t i = 0; i < FSR4_SCRATCH_BYTES; ++i) {
        if (actual[256 + i] != expected[i]) {
            if (mismatches < 16)
                report("FSR4_REPLAY_DIFF offset=%zu actual=%02x expected=%02x\n",
                       i, actual[256 + i], expected[i]);
            ++mismatches;
        }
    }
    for (unsigned i = 0; i < 256; ++i)
        guards += (actual[i] != 0xa5) + (actual[256 + FSR4_SCRATCH_BYTES + i] != 0xa5);
    FILE *output = fopen("/download0/fsr4-clear-output.bin", "wb");
    int output_ok = output && fwrite(actual + 256, 1, FSR4_SCRATCH_BYTES, output) == FSR4_SCRATCH_BYTES;
    if (output && fclose(output)) output_ok = 0;
    report("FSR4_CLEAR_RESULT bytes=%u mismatches=%zu guards=%zu output=%d stage_mismatches=%zu\n",
            (unsigned)FSR4_SCRATCH_BYTES, mismatches, guards, output_ok, stage_mismatches);

    vkDestroyDescriptorPool(device, pool, NULL);
    for (unsigned i = 0; i < buffer_count; ++i) {
        vkUnmapMemory(device, memory[i]);
        vkDestroyBuffer(device, buffers[i], NULL);
        vkFreeMemory(device, memory[i], NULL);
    }
    vkDestroyPipelineLayout(device, layout, NULL);
    for (unsigned i = 0; i < 3; ++i) vkDestroyDescriptorSetLayout(device, layouts[i], NULL);
    vkDestroyDevice(device, NULL);
    vkDestroyInstance(instance, NULL);
    report("FSR4_CLEAR_END result=%s retired=1\n",
            !mismatches && !guards && output_ok && !stage_mismatches ? "PASS" : "FAIL");
    if (log_file) fclose(log_file);
    /* Native titles stay alive for shell-mediated closure, as in the template.
     * Returning through the payload CRT calls exit and raises SIGSYS. */
    for (;;) usleep(100000);
}
