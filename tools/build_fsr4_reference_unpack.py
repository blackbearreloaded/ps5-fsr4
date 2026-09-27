#!/usr/bin/env python3
# Copyright (C) 2026 BlackBearReloaded
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a separate WARP oracle from the verified local RC11 build inputs.

Requires the same pinned SDK, DXC and upstream sources as fsr4_reference_probe.
Refuses an existing output directory; never replaces the upstream provider.
"""
from pathlib import Path
import sys,json,hashlib,subprocess,shutil
from concurrent.futures import ThreadPoolExecutor
r=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(r/'tools'))
from fsr4_scalar_unpack import lower
src=r.parent/'references/bc250-fsr4-fork/dll'
sys.path.insert(0,str(src))
from repack_dll import build
from build import verify_sources
verify_sources()
manifest=json.loads((src/'manifest.json').read_text())
old=r/'build/reference-runtime/bc250-rc11'
out=r/'build/reference-runtime/bc250-scalar-unpack'
out.mkdir();(out/'shaders').mkdir()
dxc=r/'build/reference-runtime/dxc/linux_dxc_2026_07_29.x86_x64/lib/libdxcompiler.so'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(dxc)==manifest['dxcompiler_sha256']
def compile_one(original):
 row=dict(original);name=f"{row['original_offset']:08x}"
 source=src/row['source'];assert sha(source)==row['source_sha256']
 lowered,count=lower(source.read_text())
 target=out/'shaders'/f'{name}.dxil'
 if count:
  ll=out/'shaders'/f'{name}.ll';ll.write_text(lowered)
  raw=target.with_suffix('.raw.dxil')
  for mode,first,last in [('assemble',ll,raw),('validate',raw,target)]:
   q=subprocess.run([str(old/'assemble'),str(dxc),mode,str(first),str(last)],capture_output=True,text=True,timeout=90)
   if q.returncode:raise RuntimeError(q.stderr)
  raw.unlink()
 else:
  original_shader=old/'shaders'/f'{name}.dxil';assert sha(original_shader)==row['replacement_sha256']
  shutil.copyfile(original_shader,target)
 row.update(replacement=str(target),replacement_sha256=sha(target),scalar_unpack_count=count,upstream_shader_sha256=original['replacement_sha256'])
 return row
with ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(compile_one,manifest['replacements']))
record=build(r/'build/fsr4-reference/amd_fidelityfx_upscaler_dx12.dll',dict(sdk_sha256=manifest['sdk_sha256'],replacements=rows),out/'amd_fidelityfx_upscaler_dx12.dll')
receipt=dict(reference_only=True,transformation='i32 -> two i16: explicit truncate/logical-shift/unpack',transform_sha256=sha(r/'tools/fsr4_scalar_unpack.py'),upstream_provider_sha256=manifest['expected_dll_sha256'],provider_sha256=record['sha256'],shaders=rows,changed_shaders=sum(bool(x['scalar_unpack_count']) for x in rows),expanded_casts=sum(x['scalar_unpack_count'] for x in rows))
(out/'derivation.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({k:v for k,v in receipt.items() if k!='shaders'},indent=2))
