# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute with RenderDoc 1.46's --python option, using the documented environment."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import traceback

import renderdoc as rd

WARP_SHA = "e79c10550449365adf0a9393d97a0df69941e671ab6e952a78d92da066517ca3"
OUTPUT_SHA = "1a06dd8e1be3aac817a8a17abcb3dc9358e41b0821fb3c210993878336e04799"


def serialise(value):
    """Retain SWIG structure/sequence values; never stringify opaque pointers."""
    if isinstance(value, rd.ResourceId):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, getattr(rd, "SamplerDescriptor", ())):
        # D3D12 does not populate Vulkan YCbCr fields; their enum getters may be invalid.
        return fields(value, "addressU addressV addressW borderColorValue borderColorType "
                             "compareFunction filter maxAnisotropy minLOD maxLOD mipBias")
    if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        return [serialise(item) for item in value]
    if hasattr(value, "this"):
        names = [name for name in dir(value)
                 if not name.startswith("_") and name not in ("this", "thisown", "gpuAddress")
                 and not callable(getattr(value, name))]
        if names:
            return {name: serialise(getattr(value, name)) for name in names}
    raise TypeError("Unsupported RenderDoc value: " + type(value).__name__)


def fields(value, names):
    return {name: serialise(getattr(value, name)) for name in names.split()}


def blob(out, data):
    data = bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    path = out / (digest + ".bin")
    if not path.exists():
        path.write_bytes(data)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("Corrupt existing export blob: " + path.name)
    return dict(sha256=digest, bytes=len(data))


def export(capture, warp, out, variant="upstream-rc11"):
    outputs = {"upstream-rc11": OUTPUT_SHA,
               "scalar-unpack": "ed7f85a7edf0c90cb86cbddbfa361ac1ad8d2f41d7714752344059f1d0965860"}
    if variant not in outputs:
        raise ValueError("Unknown reference variant")
    expected_output = outputs[variant]
    if hashlib.sha256(warp.read_bytes()).hexdigest() != WARP_SHA:
        raise ValueError("WARP runtime identity mismatch")
    out.mkdir(parents=True, exist_ok=False)
    # Hold the app-local WARP module alive throughout replay.
    warp_module = ctypes.WinDLL(str(warp))
    cap = rd.OpenCaptureFile()
    controller = None
    try:
        status = cap.OpenFile(str(capture), "", None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError(str(status))
        status, controller = cap.OpenCapture(rd.ReplayOptions(), None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError(str(status))
        graph = dict(schema=1, ps5_execution=False, reference_variant=variant,
                     capture_sha256=hashlib.sha256(capture.read_bytes()).hexdigest(),
                     warp_sha256=WARP_SHA,
                     resources=[fields(x, "resourceId name type") for x in controller.GetResources()],
                     buffers=[serialise(x) for x in controller.GetBuffers()],
                     textures=[serialise(x) for x in controller.GetTextures()],
                     dispatches=[])
        textures = {str(x.resourceId) for x in controller.GetTextures()}
        for action in controller.GetRootActions():
            if action.children:
                raise ValueError("Unexpected nested action tree in the pinned workload")
            if not action.flags & rd.ActionFlags.Dispatch:
                continue
            controller.SetFrameEvent(action.eventId, True)
            pipe = controller.GetPipelineState()
            shader = pipe.GetShaderReflection(rd.ShaderStage.Compute)
            entry = fields(action, "eventId dispatchDimension")
            entry.update(shader=blob(out, shader.rawBytes), entryPoint=shader.entryPoint,
                         threads=list(shader.dispatchThreadsDimension),
                         rootSignature=serialise(controller.GetD3D12PipelineState().rootSignature))
            groups = [
                ("srv", pipe.GetReadOnlyResources, shader.readOnlyResources),
                ("uav", pipe.GetReadWriteResources, shader.readWriteResources),
                ("cbv", pipe.GetConstantBlocks, shader.constantBlocks),
                ("samplers", pipe.GetSamplers, shader.samplers),
            ]
            written_resources = []
            for label, getter, reflection in groups:
                entry[label] = []
                entry[label + "_reflection"] = [
                    fields(x, "name fixedBindNumber fixedBindSetOrSpace bindArraySize")
                    for x in reflection]
                for binding in getter(rd.ShaderStage.Compute):
                    record = dict(access=serialise(binding.access))
                    entry[label].append(record)
                    if label == "samplers":
                        record["sampler"] = serialise(binding.sampler)
                        continue
                    descriptor = binding.descriptor
                    record["descriptor"] = serialise(descriptor)
                    if descriptor.resource == rd.ResourceId.Null():
                        continue
                    if str(descriptor.resource) in textures:
                        # The pinned workload uses one slice per view; don't silently truncate others.
                        if descriptor.numSlices != 1:
                            raise ValueError("Multi-slice view requires explicit export support")
                        sub = rd.Subresource()
                        sub.mip, sub.slice = descriptor.firstMip, descriptor.firstSlice
                        data = controller.GetTextureData(descriptor.resource, sub)
                    else:
                        data = controller.GetBufferData(descriptor.resource,
                                                        descriptor.byteOffset, descriptor.byteSize)
                    record["at_event"] = blob(out, data)
                    if label == "uav":
                        written_resources.append((record, descriptor))
            # Capture the actual input to in-place passes, including any copies
            # between dispatches; the preceding dispatch snapshot is insufficient.
            controller.SetFrameEvent(action.eventId - 1, True)
            for record, descriptor in written_resources:
                if str(descriptor.resource) in textures:
                    sub = rd.Subresource()
                    sub.mip, sub.slice = descriptor.firstMip, descriptor.firstSlice
                    data = controller.GetTextureData(descriptor.resource, sub)
                else:
                    data = controller.GetBufferData(descriptor.resource,
                                                    descriptor.byteOffset, descriptor.byteSize)
                record["before_event"] = blob(out, data)
            graph["dispatches"].append(entry)
            (out / "graph.json").write_text(json.dumps(graph, indent=2) + "\n")
        if len(graph["dispatches"]) != 112:
            raise ValueError("Expected 28 dispatches for each of four frames")
        matches = [x for x in graph["dispatches"][-1]["uav"]
                   if x.get("at_event", {}).get("sha256") == expected_output]
        if len(matches) != 1:
            raise ValueError("Replay output differs from the uncaptured reference")
        receipt = dict(dispatches=112, resources=len(graph["resources"]),
                       unique_shaders=len({x["shader"]["sha256"] for x in graph["dispatches"]}),
                       final_output=matches[0]["descriptor"]["resource"],
                       final_output_sha256=expected_output, reference_variant=variant,
                       graph_sha256=hashlib.sha256((out / "graph.json").read_bytes()).hexdigest(),
                       copies_and_barriers_exported=False, uav_before_state_exported=True, ps5_execution=False)
        (out / "complete.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt
    finally:
        if controller:
            controller.Shutdown()
        cap.Shutdown()
        del warp_module


if __name__ == "__main__":
    # SystemExit prevents qrenderdoc opening its main UI after the script.
    try:
        export(Path(os.environ["FSR4_CAPTURE_FILE"]),
               Path(os.environ["FSR4_WARP_DLL"]), Path(os.environ["FSR4_CAPTURE_EXPORT"]),
               os.environ.get("FSR4_REFERENCE_VARIANT", "upstream-rc11"))
    except Exception:
        path = Path(os.environ["FSR4_CAPTURE_EXPORT"])
        if path.is_dir() and not (path / "complete.json").exists():
            (path / "error.txt").write_text(traceback.format_exc())
        traceback.print_exc()
    raise SystemExit()
