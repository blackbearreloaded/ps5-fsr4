/* Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Application-owned CPU heap using the platform allocator. Link with --wrap.
 * The SDK itself does not replace its caller's allocation policy.
 */
#include <errno.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

extern int sceKernelAllocateMainDirectMemory(size_t, size_t, int, int64_t *);
extern int sceKernelMapDirectMemory(void **, size_t, int, int, int64_t, size_t);
extern int sceKernelReleaseDirectMemory(int64_t, size_t);
extern int sceKernelMunmap(void *, size_t);
extern void *sceLibcMspaceCreate(const char *, void *, size_t, unsigned);
extern void *sceLibcMspaceMalloc(void *, size_t);
extern void *sceLibcMspaceCalloc(void *, size_t, size_t);
extern void *sceLibcMspaceRealloc(void *, void *, size_t);
extern void *sceLibcMspaceMemalign(void *, size_t, size_t);
extern void sceLibcMspaceFree(void *, void *);
extern size_t sceLibcMspaceMallocUsableSize(void *, const void *);
extern void *__real_malloc(size_t);
extern void *__real_calloc(size_t, size_t);
extern void *__real_realloc(void *, size_t);
extern void __real_free(void *);
extern int __real_posix_memalign(void **, size_t, size_t);
extern size_t __real_malloc_usable_size(const void *);

enum { HEAP_BYTES = 1536u * 1024u * 1024u };
/* The larger neural shader measured 732000 KiB host RSS during compilation.
 * Reserve headroom for the native allocator and compiler working set. */
size_t fsr4_native_heap_capacity(void) { return HEAP_BYTES; }
static void *heap_base, *mspace;
static pthread_mutex_t heap_lock = PTHREAD_MUTEX_INITIALIZER;
static int owns(const void *p)
{
    uintptr_t address = (uintptr_t)p, base = (uintptr_t)heap_base;
    return mspace && address >= base && address - base < HEAP_BYTES;
}

/* Call once on the startup thread before creating any workers.
 * Keep the arena through process teardown: C++/compiler finalizers may free
 * allocations after main returns. The kernel releases it with the process. */
int fsr4_native_heap_init(void)
{
    if (mspace) return 0;
    int64_t physical = -1;
    int rc = sceKernelAllocateMainDirectMemory(HEAP_BYTES, 65536, 0x0c, &physical);
    if (rc) return rc;
    rc = sceKernelMapDirectMemory(&heap_base, HEAP_BYTES, 3, 0, physical, 65536);
    if (rc) {
        sceKernelReleaseDirectMemory(physical, HEAP_BYTES);
        return rc;
    }
    mspace = sceLibcMspaceCreate("fsr4-compiler", heap_base, HEAP_BYTES, 0);
    if (!mspace) {
        sceKernelMunmap(heap_base, HEAP_BYTES);
        sceKernelReleaseDirectMemory(physical, HEAP_BYTES);
        heap_base = NULL;
        return ENOMEM;
    }
    return 0;
}
void *__wrap_malloc(size_t n)
{
    if (!mspace) return __real_malloc(n);
    pthread_mutex_lock(&heap_lock);
    void *p = sceLibcMspaceMalloc(mspace, n);
    pthread_mutex_unlock(&heap_lock);
    return p;
}
void *__wrap_calloc(size_t n, size_t size)
{
    if (size && n > SIZE_MAX / size) { errno = ENOMEM; return NULL; }
    if (!mspace) return __real_calloc(n, size);
    pthread_mutex_lock(&heap_lock);
    void *p = sceLibcMspaceCalloc(mspace, n, size);
    pthread_mutex_unlock(&heap_lock);
    return p;
}
void __wrap_free(void *p)
{
    if (!p) return;
    if (!owns(p)) { __real_free(p); return; }
    pthread_mutex_lock(&heap_lock);
    sceLibcMspaceFree(mspace, p);
    pthread_mutex_unlock(&heap_lock);
}
void *__wrap_realloc(void *p, size_t n)
{
    if (!p) return __wrap_malloc(n);
    if (!owns(p)) return __real_realloc(p, n);
    pthread_mutex_lock(&heap_lock);
    void *result = sceLibcMspaceRealloc(mspace, p, n);
    pthread_mutex_unlock(&heap_lock);
    return result;
}
int __wrap_posix_memalign(void **out, size_t alignment, size_t n)
{
    if (!out || alignment < sizeof(void *) || (alignment & (alignment - 1))) return EINVAL;
    if (!mspace) return __real_posix_memalign(out, alignment, n);
    pthread_mutex_lock(&heap_lock);
    void *p = sceLibcMspaceMemalign(mspace, alignment, n);
    pthread_mutex_unlock(&heap_lock);
    if (!p) return ENOMEM;
    *out = p;
    return 0;
}
void *__wrap_memalign(size_t alignment, size_t n)
{
    void *p = NULL;
    int rc = __wrap_posix_memalign(&p, alignment, n);
    if (rc) errno = rc;
    return p;
}
void *__wrap_aligned_alloc(size_t alignment, size_t n)
{
    if (!alignment || n % alignment) { errno = EINVAL; return NULL; }
    return __wrap_memalign(alignment, n);
}
size_t __wrap_malloc_usable_size(const void *p)
{
    if (!p) return 0;
    if (!owns(p)) return __real_malloc_usable_size(p);
    pthread_mutex_lock(&heap_lock);
    size_t size = sceLibcMspaceMallocUsableSize(mspace, p);
    pthread_mutex_unlock(&heap_lock);
    return size;
}
