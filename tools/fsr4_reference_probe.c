/*
 * Copyright (C) 2026 BlackBearReloaded
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Run the pinned MIT-licensed BC250 workload on an app-local WARP/Agility runtime.
 */
#define COBJMACROS
#define INITGUID
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#ifdef FSR4_CAPTURE
#include <stdbool.h>
#include <renderdoc_app.h>
static RENDERDOC_API_1_6_0 *capture_api;
static void *captured_device;
#endif

__declspec(dllexport) const UINT D3D12SDKVersion = 619;
__declspec(dllexport) const char *D3D12SDKPath = ".\\D3D12\\";

static FARPROC WINAPI reference_get_proc_address(HMODULE module, LPCSTR name);
static DWORD WINAPI reference_wait(HANDLE handle, DWORD milliseconds);
#define GetProcAddress reference_get_proc_address
#define WaitForSingleObject reference_wait
#define mainCRTStartup bc250_mainCRTStartup
#include BC250_PROBE_SOURCE
#undef mainCRTStartup
#undef WaitForSingleObject
#undef GetProcAddress

/* The probe waits 30 s for each frame's fence; WARP can take longer at 3840x2160. */
static DWORD WINAPI reference_wait(HANDLE handle, DWORD milliseconds)
{
    return WaitForSingleObject(handle, milliseconds == INFINITE ? INFINITE : 600000);
}

#ifdef FSR4_CAPTURE
static PfnFfxDestroyContext real_destroy;
static ffxReturnCode_t reference_destroy(ffxContext *context,
                                        const ffxAllocationCallbacks *allocation)
{
    int ok = capture_api->EndFrameCapture(captured_device, NULL);
    log_line(ok ? "capture: complete=1\n" : "capture: complete=0\n");
    ffxReturnCode_t rc = real_destroy(context, allocation);
    if (!ok) ExitProcess(32);
    return rc;
}
#endif

static HRESULT (WINAPI *create_device)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);

static HRESULT WINAPI create_warp_device(IUnknown *unused, D3D_FEATURE_LEVEL level,
                                         REFIID iid, void **device)
{
    (void)unused;
    IDXGIFactory4 *factory = NULL;
    IUnknown *adapter = NULL;
    HMODULE dxgi = LoadLibraryW(L"dxgi.dll");
    HRESULT (WINAPI *create_factory)(REFIID, void **) =
        (void *)GetProcAddress(dxgi, "CreateDXGIFactory1");
    HRESULT hr = create_factory ? create_factory(&IID_IDXGIFactory4, (void **)&factory) : E_NOINTERFACE;
#ifdef FSR4_HARDWARE_ADAPTER
    /* Variance measurement only: the largest non-software adapter replaces WARP. */
    SIZE_T best = 0;
    for (UINT i = 0; SUCCEEDED(hr); i++) {
        IDXGIAdapter1 *candidate = NULL;
        DXGI_ADAPTER_DESC1 desc;
        if (FAILED(IDXGIFactory4_EnumAdapters1(factory, i, &candidate)))
            break;
        if (SUCCEEDED(IDXGIAdapter1_GetDesc1(candidate, &desc)) &&
            !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) && desc.DedicatedVideoMemory > best) {
            best = desc.DedicatedVideoMemory;
            if (adapter) IUnknown_Release(adapter);
            adapter = (IUnknown *)candidate;
            candidate = NULL;
        }
        if (candidate) IDXGIAdapter1_Release(candidate);
    }
    if (SUCCEEDED(hr) && !adapter)
        hr = DXGI_ERROR_NOT_FOUND;
#else
    if (SUCCEEDED(hr))
        hr = IDXGIFactory4_EnumWarpAdapter(factory, &IID_IUnknown, (void **)&adapter);
#endif
    if (SUCCEEDED(hr))
        hr = create_device(adapter, level, iid, device);
#ifdef FSR4_CAPTURE
    if (SUCCEEDED(hr)) {
        captured_device = *device;
        capture_api->StartFrameCapture(*device, NULL);
    }
#endif
    if (adapter) IUnknown_Release(adapter);
    if (factory) IDXGIFactory4_Release(factory);
    return hr;
}

static FARPROC WINAPI reference_get_proc_address(HMODULE module, LPCSTR name)
{
    FARPROC result = GetProcAddress(module, name);
    if ((uintptr_t)name > 65535 && !strcmp(name, "D3D12CreateDevice")) {
        create_device = (void *)result;
        return result ? (FARPROC)create_warp_device : NULL;
    }
#ifdef FSR4_CAPTURE
    if ((uintptr_t)name > 65535 && !strcmp(name, "ffxDestroyContext")) {
        real_destroy = (void *)result;
        return result ? (FARPROC)reference_destroy : NULL;
    }
#endif
    return result;
}

void mainCRTStartup(void)
{
#ifdef FSR4_CAPTURE
    static wchar_t library[4096];
    static char path[4096];
    DWORD n = GetEnvironmentVariableW(L"FSR4_RENDERDOC_DLL", library, 4096);
    if (!n || n >= 4096) ExitProcess(33);
    HMODULE module = LoadLibraryW(library);
    pRENDERDOC_GetAPI get_api = module ? (void *)GetProcAddress(module, "RENDERDOC_GetAPI") : NULL;
    if (!get_api || get_api(eRENDERDOC_API_Version_1_6_0, (void **)&capture_api) != 1)
        ExitProcess(34);
    n = GetEnvironmentVariableA("BC250_FFX_OUTPUT", path, sizeof(path));
    if (!n || n >= sizeof(path)) ExitProcess(35);
    capture_api->SetCaptureFilePathTemplate(path);
    capture_api->MaskOverlayBits(0, 0);
#endif
#ifndef FSR4_HARDWARE_ADAPTER
    if (!LoadLibraryW(L"d3d10warp.dll")) ExitProcess(31);
#endif
    bc250_mainCRTStartup();
}
