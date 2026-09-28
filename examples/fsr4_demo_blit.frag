#version 450
// Copyright (C) 2026 BlackBearReloaded
// SPDX-License-Identifier: GPL-3.0-or-later
//
// FSR4 demo presentation: copies the composed frame into the display image,
// whose tiling the color target handles.
layout(set = 0, binding = 0) uniform sampler2D frame;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;

void main()
{
    color = textureLod(frame, uv, 0.0);
}
