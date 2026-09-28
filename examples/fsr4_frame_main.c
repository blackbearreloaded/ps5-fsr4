/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Native captured FSR4 image stages and connected first-frame witness.
 */
#ifdef FSR4_HOST
/* Host replay of the identical fixture on any Vulkan 1.3 driver (e.g. lavapipe). */
#include <vulkan/vulkan.h>
#else
#include <ps5vk/ps5vk.h>
#endif
#include <stdint.h>
#include <stdarg.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "fsr4_frame_fixture.h"
static FILE *log_file;
#ifdef FSR4_HOST
#define ASSET_ROOT (getenv("FSR4_ASSET_DIR") ? getenv("FSR4_ASSET_DIR") : "assets")
#define OUTPUT_ROOT (getenv("FSR4_OUTPUT_DIR") ? getenv("FSR4_OUTPUT_DIR") : ".")
static int sceKernelDebugOutText(int level, const char *text) { (void)level; return fputs(text, stdout); }
#else
#include <sys/stat.h>
#define ASSET_ROOT "/app0/assets"
#define OUTPUT_ROOT output_root()
extern int sceKernelDebugOutText(int level, const char *text);
/* Prefer a directory the development host can read back; the sandbox is the fallback. */
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
    char path[1024];
    if (snprintf(path, sizeof(path), "%s/%s", ASSET_ROOT, name) >= (int)sizeof(path))
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


#define RESOURCE_COUNT (sizeof(fsr4_resources)/sizeof(fsr4_resources[0]))
#define PASS_COUNT (sizeof(fsr4_passes)/sizeof(fsr4_passes[0]))
struct resource {
    VkBuffer buffer;
    VkDeviceMemory memory;
    unsigned char *mapped;
    VkImage image;
    VkImageLayout layout;
    VkImageView view;
    VkDeviceMemory image_memory;
    unsigned char *image_mapped;
    VkDeviceSize image_guard, image_bytes;
};
static struct resource resources[RESOURCE_COUNT];
static VkDevice device;
static VkQueue queue;
static VkPhysicalDeviceMemoryProperties memory_properties;
static VkCommandPool commands;
static size_t stage_mismatches, retired;
struct cached_pipeline {
    const struct fsr4_pass *source;
    VkDescriptorSetLayout sets[4];
    VkPipelineLayout layout;
    VkPipeline pipeline;
};
static struct cached_pipeline pipelines[PASS_COUNT];
static size_t pipeline_count;

static int same_pipeline(const struct fsr4_pass *a, const struct fsr4_pass *b)
{
    if (a->code_bytes != b->code_bytes || a->binding_count != b->binding_count ||
        memcmp(a->code, b->code, a->code_bytes)) return 0;
    for (unsigned i = 0; i < a->binding_count; ++i) {
        const struct fsr4_binding *x = &a->bindings[i], *y = &b->bindings[i];
        if (x->set != y->set || x->binding != y->binding || x->type != y->type)
            return 0;
    }
    return 1;
}

static struct cached_pipeline *find_pipeline(const struct fsr4_pass *pass)
{
    for (size_t i = 0; i < pipeline_count; ++i)
        if (same_pipeline(pipelines[i].source, pass)) return &pipelines[i];
    return NULL;
}

static int allocate(const VkMemoryRequirements *req, VkDeviceSize extra,
                    VkDeviceMemory *memory, unsigned char **mapped)
{
    unsigned type=0;
    for(;type<memory_properties.memoryTypeCount;++type)
        if((req->memoryTypeBits&(1u<<type)) &&
           (memory_properties.memoryTypes[type].propertyFlags&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT))
            break;
    if(type==memory_properties.memoryTypeCount || req->size>UINT64_MAX-extra) return 1;
    VkMemoryAllocateInfo info={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize=req->size+extra,.memoryTypeIndex=type};
    CHECK(vkAllocateMemory(device,&info,NULL,memory));
    CHECK(vkMapMemory(device,*memory,0,VK_WHOLE_SIZE,0,(void**)mapped));
    memset(*mapped,0xa5,(size_t)(req->size+extra));
    return 0;
}
static int cache(VkDeviceMemory memory, int invalidate)
{
    VkMappedMemoryRange range={.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
        .memory=memory,.offset=0,.size=VK_WHOLE_SIZE};
    if(invalidate) {CHECK(vkInvalidateMappedMemoryRanges(device,1,&range));}
    else {CHECK(vkFlushMappedMemoryRanges(device,1,&range));}
    return 0;
}
static int begin(VkCommandBuffer *command)
{
    VkCommandBufferAllocateInfo ai={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool=commands,.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};
    CHECK(vkAllocateCommandBuffers(device,&ai,command));
    VkCommandBufferBeginInfo bi={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    CHECK(vkBeginCommandBuffer(*command,&bi));
    return 0;
}
static int execute(VkCommandBuffer command)
{
    CHECK(vkEndCommandBuffer(command));
    VkFence fence;
    VkFenceCreateInfo fi={.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    CHECK(vkCreateFence(device,&fi,NULL,&fence));
    VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount=1,.pCommandBuffers=&command};
    CHECK(vkQueueSubmit(queue,1,&submit,fence));
#ifdef FSR4_HOST
    const uint64_t timeout=UINT64_MAX;  /* CPU drivers can need minutes per network pass. */
#else
    const uint64_t timeout=UINT64_C(10000000000);
#endif
    VkResult result=vkWaitForFences(device,1,&fence,VK_TRUE,timeout);
    report("FSR4_FRAME_FENCE result=%d\n",result);
    /* Never release storage while completion is unknown. */
    if(result!=VK_SUCCESS) for(;;)usleep(100000);
    vkDestroyFence(device,fence,NULL);
    vkFreeCommandBuffers(device,commands,1,&command);
    return 0;
}
static void image_barrier(VkCommandBuffer command, VkImage image,
    VkImageLayout old_layout, VkImageLayout new_layout, VkAccessFlags source, VkAccessFlags destination,
    VkPipelineStageFlags source_stage, VkPipelineStageFlags destination_stage)
{
    VkImageMemoryBarrier b={.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .srcAccessMask=source,.dstAccessMask=destination,
        .oldLayout=old_layout,.newLayout=new_layout,
        .srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,
        .image=image,.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
    vkCmdPipelineBarrier(command,source_stage,destination_stage,0,0,NULL,0,NULL,1,&b);
}
static int create_resource(unsigned index)
{
    struct resource *r=&resources[index];
    const struct fsr4_resource_desc *d=&fsr4_resources[index];
    report("FSR4_FRAME_RESOURCE index=%u bytes=%zu format=%d extent=%u,%u\n",
        index,d->bytes,d->format,d->width,d->height);
    VkBufferCreateInfo bi={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size=d->bytes+512,.sharingMode=VK_SHARING_MODE_EXCLUSIVE,
        .usage=d->format!=VK_FORMAT_UNDEFINED?
            VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT:
            d->uniform?VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT:VK_BUFFER_USAGE_STORAGE_BUFFER_BIT};
    CHECK(vkCreateBuffer(device,&bi,NULL,&r->buffer));
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(device,r->buffer,&req);
    if(allocate(&req,0,&r->memory,&r->mapped))return 1;
    CHECK(vkBindBufferMemory(device,r->buffer,r->memory,0));
    if(!asset(d->asset,r->mapped+256,d->bytes) || cache(r->memory,0)) return 1;
    if(d->format==VK_FORMAT_UNDEFINED)return 0;
    VkImageCreateInfo ii={.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        .imageType=VK_IMAGE_TYPE_2D,.format=d->format,.extent={d->width,d->height,1},
        .mipLevels=1,.arrayLayers=1,.samples=VK_SAMPLE_COUNT_1_BIT,
        .tiling=VK_IMAGE_TILING_OPTIMAL,.usage=d->usage,.sharingMode=VK_SHARING_MODE_EXCLUSIVE,
        .initialLayout=VK_IMAGE_LAYOUT_UNDEFINED};
    CHECK(vkCreateImage(device,&ii,NULL,&r->image));
    vkGetImageMemoryRequirements(device,r->image,&req);
    r->image_guard=req.alignment;r->image_bytes=req.size;
    if(req.alignment>UINT64_MAX/2 || allocate(&req,2*req.alignment,&r->image_memory,&r->image_mapped))return 1;
    CHECK(vkBindImageMemory(device,r->image,r->image_memory,req.alignment));
    if(cache(r->image_memory,0))return 1;
    VkImageViewCreateInfo vi={.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
        .image=r->image,.viewType=VK_IMAGE_VIEW_TYPE_2D,.format=d->format,
        .subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
    CHECK(vkCreateImageView(device,&vi,NULL,&r->view));
    VkCommandBuffer command;
    if(begin(&command))return 1;
    const int storage=!!(d->usage&VK_IMAGE_USAGE_STORAGE_BIT);
    const VkImageLayout upload_layout=storage?VK_IMAGE_LAYOUT_GENERAL:VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    r->layout=storage?VK_IMAGE_LAYOUT_GENERAL:VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    image_barrier(command,r->image,VK_IMAGE_LAYOUT_UNDEFINED,upload_layout,0,VK_ACCESS_TRANSFER_WRITE_BIT,
        VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT);
    VkBufferImageCopy region={.bufferOffset=256,.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},
        .imageExtent={d->width,d->height,1}};
    vkCmdCopyBufferToImage(command,r->buffer,r->image,upload_layout,1,&region);
    image_barrier(command,r->image,upload_layout,r->layout,VK_ACCESS_TRANSFER_WRITE_BIT,
        VK_ACCESS_SHADER_READ_BIT|((d->usage&VK_IMAGE_USAGE_STORAGE_BIT)?VK_ACCESS_SHADER_WRITE_BIT:0),
        VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    return execute(command);
}
static void destroy_pipeline(struct cached_pipeline *entry)
{
    if(entry->pipeline)vkDestroyPipeline(device,entry->pipeline,NULL);
    if(entry->layout)vkDestroyPipelineLayout(device,entry->layout,NULL);
    for(unsigned set=0;set<4;++set)
        if(entry->sets[set])vkDestroyDescriptorSetLayout(device,entry->sets[set],NULL);
    *entry=(struct cached_pipeline){0};
}
static struct cached_pipeline *create_pipeline(const struct fsr4_pass *p)
{
    struct cached_pipeline *entry = &pipelines[pipeline_count];
    entry->source = p;
    VkShaderModule shader=VK_NULL_HANDLE;
    VkResult result=VK_SUCCESS;
    for(unsigned set=0;set<4;++set){
        VkDescriptorSetLayoutBinding bindings[32];unsigned count=0;
        for(unsigned i=0;i<p->binding_count;++i)if(p->bindings[i].set==set){
            const struct fsr4_binding *b=&p->bindings[i];
            if(count==32){result=VK_ERROR_INITIALIZATION_FAILED;goto fail;}
            bindings[count++]=(VkDescriptorSetLayoutBinding){.binding=b->binding,
                .descriptorType=b->type,.descriptorCount=1,.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT};
        }
        VkDescriptorSetLayoutCreateInfo info={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
            .bindingCount=count,.pBindings=count?bindings:NULL};
        result=vkCreateDescriptorSetLayout(device,&info,NULL,&entry->sets[set]);
        if(result!=VK_SUCCESS)goto fail;
    }
    VkPipelineLayoutCreateInfo li={.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount=4,.pSetLayouts=entry->sets};
    result=vkCreatePipelineLayout(device,&li,NULL,&entry->layout);
    if(result!=VK_SUCCESS)goto fail;
    VkShaderModuleCreateInfo si={.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize=p->code_bytes,.pCode=p->code};
    result=vkCreateShaderModule(device,&si,NULL,&shader);
    if(result!=VK_SUCCESS)goto fail;
    VkComputePipelineCreateInfo ci={.sType=VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage={.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
            .stage=VK_SHADER_STAGE_COMPUTE_BIT,.module=shader,.pName="main"},
        .layout=entry->layout};
    result=vkCreateComputePipelines(device,VK_NULL_HANDLE,1,&ci,NULL,&entry->pipeline);
    vkDestroyShaderModule(device,shader,NULL);
    shader=VK_NULL_HANDLE;
    if(result!=VK_SUCCESS)goto fail;
    ++pipeline_count;
    return entry;
fail:
    if(shader)vkDestroyShaderModule(device,shader,NULL);
    destroy_pipeline(entry);
    report("FSR4_FRAME_PIPELINE pass=%u result=%d\n",p->index,(int)result);
    return NULL;
}
static int dispatch_pass(const struct fsr4_pass *p, VkSampler sampler)
{
    report("FSR4_FRAME_DISPATCH index=%u groups=%u,%u,%u\n",
        p->index,p->groups[0],p->groups[1],p->groups[2]);
    struct cached_pipeline *entry=find_pipeline(p);
    if(!entry) entry=create_pipeline(p);
    if(!entry)return 1;
    const VkDescriptorSetLayout *layouts=entry->sets;
    VkPipelineLayout layout=entry->layout;
    VkPipeline pipeline=entry->pipeline;
    VkDescriptorPoolSize sizes[5]={{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,0},
        {VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER,0},{VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE,0},
        {VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,0},{VK_DESCRIPTOR_TYPE_SAMPLER,0}};
    for(unsigned i=0;i<p->binding_count;++i)
        for(unsigned j=0;j<5;++j)
            if(p->bindings[i].type==sizes[j].type)++sizes[j].descriptorCount;
    unsigned size_count=0;
    for(unsigned i=0;i<5;++i)if(sizes[i].descriptorCount)sizes[size_count++]=sizes[i];
    VkDescriptorPoolCreateInfo pi={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets=4,.poolSizeCount=size_count,.pPoolSizes=sizes};
    VkDescriptorPool pool;
    CHECK(vkCreateDescriptorPool(device,&pi,NULL,&pool));
    VkDescriptorSet sets[4];
    VkDescriptorSetAllocateInfo ai={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool=pool,.descriptorSetCount=4,.pSetLayouts=layouts};
    CHECK(vkAllocateDescriptorSets(device,&ai,sets));
    for(unsigned i=0;i<p->binding_count;++i){
        const struct fsr4_binding *b=&p->bindings[i];
        const struct resource *r=&resources[b->resource];
        VkDescriptorBufferInfo buffer={r->buffer,256,fsr4_resources[b->resource].bytes};
        VkDescriptorImageInfo image={sampler,r->view,r->layout};
        VkWriteDescriptorSet w={.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
            .dstSet=sets[b->set],.dstBinding=b->binding,.descriptorCount=1,.descriptorType=b->type};
        if(b->type==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER || b->type==VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER)
            w.pBufferInfo=&buffer;
        else w.pImageInfo=&image;
        vkUpdateDescriptorSets(device,1,&w,0,NULL);
    }
    VkCommandBuffer command;
    if(begin(&command))return 1;
    VkMemoryBarrier barrier={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER,
        .srcAccessMask=VK_ACCESS_HOST_WRITE_BIT|VK_ACCESS_SHADER_WRITE_BIT|VK_ACCESS_TRANSFER_WRITE_BIT,
        .dstAccessMask=VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT|VK_ACCESS_UNIFORM_READ_BIT};
    vkCmdPipelineBarrier(command,VK_PIPELINE_STAGE_HOST_BIT|VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT|
        VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,1,&barrier,0,NULL,0,NULL);
    vkCmdBindPipeline(command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);
    vkCmdBindDescriptorSets(command,VK_PIPELINE_BIND_POINT_COMPUTE,layout,0,4,sets,0,NULL);
    vkCmdDispatch(command,p->groups[0],p->groups[1],p->groups[2]);
    barrier.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT;
    barrier.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_HOST_READ_BIT;
    vkCmdPipelineBarrier(command,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
        VK_PIPELINE_STAGE_TRANSFER_BIT|VK_PIPELINE_STAGE_HOST_BIT,0,1,&barrier,0,NULL,0,NULL);
    for(unsigned i=0;i<p->output_count;++i){
        unsigned n=p->outputs[i].resource;const struct fsr4_resource_desc*d=&fsr4_resources[n];
        if(resources[n].image){
            VkBufferImageCopy region={.bufferOffset=256,.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},
                .imageExtent={d->width,d->height,1}};
            vkCmdCopyImageToBuffer(command,resources[n].image,VK_IMAGE_LAYOUT_GENERAL,resources[n].buffer,1,&region);
        }
    }
    if(execute(command))return 1;
    ++retired;
    for(unsigned i=0;i<p->output_count;++i){
        unsigned n=p->outputs[i].resource;
        if(cache(resources[n].memory,1))return 1;
        uint32_t crc=tensor_crc32(resources[n].mapped+256,fsr4_resources[n].bytes);
        stage_mismatches+=crc!=p->outputs[i].crc;
        report("FSR4_FRAME_CRC pass=%u resource=%u actual=%08x expected=%08x match=%d\n",
            p->index,n,crc,p->outputs[i].crc,crc==p->outputs[i].crc);
    }
    vkDestroyDescriptorPool(device,pool,NULL);
    return 0;
}
static int run(void)
{
    VkInstance instance;VkInstanceCreateInfo ii={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    CHECK(vkCreateInstance(&ii,NULL,&instance));
    uint32_t count=1;VkPhysicalDevice physical;
    CHECK(vkEnumeratePhysicalDevices(instance,&count,&physical));
    if(count!=1)return 1;
    float priority=1;
    VkDeviceQueueCreateInfo qi={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueCount=1,.pQueuePriorities=&priority};
    VkPhysicalDeviceFeatures features={.shaderInt16=VK_TRUE};
    const char *extension=VK_KHR_STORAGE_BUFFER_STORAGE_CLASS_EXTENSION_NAME;
    VkDeviceCreateInfo di={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount=1,.pQueueCreateInfos=&qi,.pEnabledFeatures=&features,
        .enabledExtensionCount=1,.ppEnabledExtensionNames=&extension};
#ifdef FSR4_HOST
    /* A conformant driver requires the features the converted shaders declare. */
    VkPhysicalDeviceVulkan13Features f13={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    VkPhysicalDeviceVulkan12Features f12={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES,.pNext=&f13};
    VkPhysicalDeviceVulkan11Features f11={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_1_FEATURES,.pNext=&f12};
    VkPhysicalDeviceFeatures2 all={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2,.pNext=&f11};
    vkGetPhysicalDeviceFeatures2(physical,&all);
    f13.robustImageAccess=VK_FALSE;all.features.robustBufferAccess=VK_FALSE;
    di.pNext=&all;di.pEnabledFeatures=NULL;di.enabledExtensionCount=0;
#endif
    CHECK(vkCreateDevice(physical,&di,NULL,&device));
    vkGetDeviceQueue(device,0,0,&queue);
    vkGetPhysicalDeviceMemoryProperties(physical,&memory_properties);
    VkCommandPoolCreateInfo pi={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    CHECK(vkCreateCommandPool(device,&pi,NULL,&commands));
    VkSampler sampler;
    VkSamplerCreateInfo sci={.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,
        .magFilter=VK_FILTER_LINEAR,.minFilter=VK_FILTER_LINEAR,.mipmapMode=VK_SAMPLER_MIPMAP_MODE_LINEAR,
        .addressModeU=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,.addressModeV=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeW=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,.maxAnisotropy=1,.maxLod=1000};
    CHECK(vkCreateSampler(device,&sci,NULL,&sampler));
    for(unsigned i=0;i<RESOURCE_COUNT;++i)if(create_resource(i))return 1;
    for(unsigned i=0;i<PASS_COUNT;++i)if(dispatch_pass(&fsr4_passes[i],sampler))return 1;
    size_t mismatches=0,guards=0;
    for(unsigned i=0;i<RESOURCE_COUNT;++i){
        struct resource*r=&resources[i];const struct fsr4_resource_desc*d=&fsr4_resources[i];
        if(cache(r->memory,1))return 1;
        for(unsigned j=0;j<256;++j)guards+=(r->mapped[j]!=0xa5)+(r->mapped[256+d->bytes+j]!=0xa5);
        if(r->image){
            if(cache(r->image_memory,1))return 1;
            for(VkDeviceSize j=0;j<r->image_guard;++j)
                guards+=(r->image_mapped[j]!=0xa5)+(r->image_mapped[r->image_guard+r->image_bytes+j]!=0xa5);
        }
        if(*d->expected){
            unsigned char*expected=malloc(d->bytes);
            if(!expected || !asset(d->expected,expected,d->bytes))return 1;
            size_t differences=0;
            for(size_t j=0;j<d->bytes;++j)if(expected[j]!=r->mapped[256+j]){
                if(differences<8)report("FSR4_FRAME_DIFF resource=%u offset=%zu actual=%02x expected=%02x\n",
                    i,j,r->mapped[256+j],expected[j]);
                ++differences;
            }
            free(expected);mismatches+=differences;
            char path[1024];snprintf(path,sizeof(path),"%s/fsr4-frame-r%u.bin",OUTPUT_ROOT,i);
            FILE*f=fopen(path,"wb");int ok=f && fwrite(r->mapped+256,1,d->bytes,f)==d->bytes;
            if(f && fclose(f))ok=0;
            report("FSR4_FRAME_RESULT resource=%u bytes=%zu mismatches=%zu output=%d\n",i,d->bytes,differences,ok);
            if(!ok)return 1;
        }
    }
    report("FSR4_FRAME_TOTAL mismatches=%zu guards=%zu stage_mismatches=%zu\n",mismatches,guards,stage_mismatches);
    report("FSR4_FRAME_PIPELINES created=%zu passes=%zu\n",pipeline_count,PASS_COUNT);
    for(size_t i=0;i<pipeline_count;++i)destroy_pipeline(&pipelines[i]);
    vkDestroySampler(device,sampler,NULL);
    for(unsigned i=0;i<RESOURCE_COUNT;++i){
        struct resource*r=&resources[i];
        if(r->image){vkDestroyImageView(device,r->view,NULL);vkDestroyImage(device,r->image,NULL);
            vkUnmapMemory(device,r->image_memory);vkFreeMemory(device,r->image_memory,NULL);}
        vkDestroyBuffer(device,r->buffer,NULL);vkUnmapMemory(device,r->memory);vkFreeMemory(device,r->memory,NULL);
    }
    vkDestroyCommandPool(device,commands,NULL);vkDestroyDevice(device,NULL);vkDestroyInstance(instance,NULL);
    return !!(mismatches||guards||stage_mismatches);
}
extern int fsr4_native_heap_init(void);
extern size_t fsr4_native_heap_capacity(void);
#ifdef FSR4_HOST
int fsr4_native_heap_init(void) { return 0; }
size_t fsr4_native_heap_capacity(void) { return 0; }
#endif
int main(void)
{
    char log_path[1024];
    snprintf(log_path,sizeof(log_path),"%s/fsr4-frame-result.txt",OUTPUT_ROOT);
    log_file=fopen(log_path,"w");
    int result=fsr4_native_heap_init();
    report("FSR4_FRAME_BEGIN fixture=%s passes=%zu heap_result=%d heap_bytes=%zu output=%s\n",
        FSR4_FIXTURE_ID,PASS_COUNT,result,fsr4_native_heap_capacity(),OUTPUT_ROOT);
    if(!result && tensor_crc32((const uint8_t*)"123456789",9)==UINT32_C(0xcbf43926)) result=run();
    else result=1;
    report("FSR4_FRAME_END result=%s retired_dispatches=%zu\n",result?"FAIL":"PASS",retired);
    if(log_file)fclose(log_file);
#ifdef FSR4_HOST
    return result;
#endif
    for(;;)usleep(100000);
}
