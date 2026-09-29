"""Fixed project template for pilot sites. Agents write only app/, components/, lib/ and public/;
the build configuration below is owned by the platform so a model cannot break it."""
from __future__ import annotations

import json
import re
from pathlib import Path

TEMPLATE_FILES: dict[str, str] = {
    "package.json": json.dumps({
        "name": "tpf-site",
        "private": True,
        "version": "1.0.0",
        "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
        "dependencies": {"next": "^16.3.6", "react": "^19.0.0", "react-dom": "^19.0.0"},
        "devDependencies": {"typescript": "^5.4.0", "@types/node": "^22.0.0", "@types/react": "^19.0.0"},
    }, indent=2) + "\n",
    "next.config.mjs": "/** @type {import('next').NextConfig} */\nconst nextConfig = { output: 'export', images: { unoptimized: true } };\nexport default nextConfig;\n",
    "tsconfig.json": json.dumps({
        "compilerOptions": {
            "target": "ES2020", "lib": ["dom", "dom.iterable", "esnext"], "allowJs": False,
            "skipLibCheck": True, "strict": True, "noEmit": True, "esModuleInterop": True,
            "module": "esnext", "moduleResolution": "bundler", "resolveJsonModule": True,
            "isolatedModules": True, "jsx": "react-jsx", "incremental": True,
            "plugins": [{"name": "next"}], "paths": {"@/*": ["./*"]},
        },
        "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
        "exclude": ["node_modules"],
    }, indent=2) + "\n",
    "next-env.d.ts": '/// <reference types="next" />\n/// <reference types="next/image-types/global" />\n',
    ".gitignore": "node_modules\n.next\nout\n",
}

ALLOWED_PREFIXES = ("app/", "components/", "lib/", "public/")
ALLOWED_SUFFIXES = (".tsx", ".ts", ".css", ".svg", ".txt", ".json", ".webmanifest", ".ico")
MAX_FILE_BYTES = 200_000
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_\-./\[\]()]+$")

TEMPLATE_RULES = (
    "Project template (fixed by the platform, do not write these files): Next.js 16 App Router, "
    "React 19, TypeScript, static export (`output: 'export'`), no API routes, no server actions, no "
    "external packages beyond next/react. Style with plain CSS in app/globals.css or CSS modules. "
    "You may write files only under app/, components/, lib/, public/ with extensions "
    + ", ".join(ALLOWED_SUFFIXES) + ". app/layout.tsx and app/page.tsx are required; each route '/x' "
    "needs app/x/page.tsx. Use next/link for internal links."
)


def validate_path(path: str) -> str | None:
    """Return an error message, or None if the generated file path is acceptable."""
    if not _SAFE_PATH.match(path) or ".." in path.split("/") or path.startswith("/"):
        return f"unsafe path: {path}"
    if not path.startswith(ALLOWED_PREFIXES):
        return f"path outside allowed folders: {path}"
    if not path.endswith(ALLOWED_SUFFIXES):
        return f"file type not allowed: {path}"
    return None


def route_to_page(route: str) -> str:
    route = route.strip("/")
    return "app/page.tsx" if not route else f"app/{route}/page.tsx"


def materialize(root: Path, files: list[dict[str, str]]) -> Path:
    """Write template + generated files into `root` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    for path, content in TEMPLATE_FILES.items():
        (root / path).write_text(content, encoding="utf-8")
    for f in files:
        err = validate_path(f["path"])
        if err:
            raise ValueError(err)
        target = (root / f["path"]).resolve()
        if root.resolve() not in target.parents:
            raise ValueError(f"path escapes workspace: {f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
    return root


def all_files(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """Template + generated files, as the deployer and git publisher need them."""
    return [{"path": p, "content": c} for p, c in TEMPLATE_FILES.items()] + list(files)
