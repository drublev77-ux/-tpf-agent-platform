#!/usr/bin/env python3
"""Build the live 3D brain of the site's first screen.

Writes src/tpf/site/brain3d.js.gz (three.js r128 with the bloom post-processing it needs, then scene.js) and
src/tpf/site/brain-mesh.bin.gz (the cortex surface: header nv, nf; float32 bbox[6]; uint16 positions; uint8 sulcal
depth; padding to 4 bytes; uint32 faces). Both are stored gzipped and served as such (src/tpf/api/main.py).
Run it after changing scene.js:   python scripts/brain3d/build.py
"""
from __future__ import annotations

import gzip
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE.parent.parent / "src" / "tpf" / "site"
VENDOR = ["three.min.js", "CopyShader.js", "LuminosityHighPassShader.js", "EffectComposer.js", "RenderPass.js",
          "ShaderPass.js", "UnrealBloomPass.js"]


def main() -> None:
    js = "\n;\n".join((HERE / "vendor" / name).read_text() for name in VENDOR) + "\n;\n" + (HERE / "scene.js").read_text()
    (SITE / "brain3d.js.gz").write_bytes(gzip.compress(js.encode(), 9, mtime=0))
    (SITE / "brain-mesh.bin.gz").write_bytes(gzip.compress((HERE / "brain-mesh.bin").read_bytes(), 9, mtime=0))


if __name__ == "__main__":
    main()
