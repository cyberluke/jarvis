# 11 — GPU scene renderer

## Inspection
No in-repo D3D/Vulkan/OpenGL SceneGraph renderer. `d3d11.dll` and
`vulkan-1.dll` exist on the host. `moderngl` is not installed.

## Decision
Did not invent a new GPU engine this run (forbidden: overwrite unrelated
project code / giant new dependency for one night).

CPU incremental `LiveScene` is now **16.75 ms/frame** (30-frame bench,
~59.7 fps estimate) at 3840×2160 PQ. Production encode still 15 fps.

## Status
PARTIAL — GPU path not shipped. CPU path already faster than the 20–45 ms
from the previous night. 60 fps HDR remains unclaimed on the wire.
