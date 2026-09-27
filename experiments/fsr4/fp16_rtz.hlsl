// Copyright (C) 2026 BlackBearReloaded
// SPDX-License-Identifier: GPL-3.0-or-later
#if ROUND_TO_EVEN
Texture2D<half> Input : register(t0);
#else
ByteAddressBuffer Input : register(t0);
#endif
RWByteAddressBuffer Output : register(u0);
[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    if (tid.x >= COUNT) return;
#if ROUND_TO_EVEN
    uint bits = (uint)asuint16(Input.Load(int3(tid.x % 192, tid.x / 192, 0)));
#else
    half value = (half)asfloat(Input.Load(tid.x * 4));
    uint bits = (uint)asuint16(value);
#endif
    // NaN payloads are not an oracle; preserve the sign and classify them.
    if ((bits & 0x7c00) == 0x7c00 && (bits & 0x3ff))
        bits = (bits & 0x8000) | 0x7e00;
    Output.Store(tid.x * 4, bits);
}
