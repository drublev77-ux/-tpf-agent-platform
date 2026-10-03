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

# Platform-owned contact form. The site is a static export, so the form posts to the platform,
# which stores the message and e-mails it to the site's owner. Agents import it, never rewrite it.
CONTACT_FORM = '''"use client";

import { useState, type FormEvent } from "react";
import { FORM_ENDPOINT } from "./config";

export type ContactFormLabels = {
  name: string; contact: string; message: string; submit: string;
  sending: string; success: string; error: string; consent?: string;
};

/** Contact form wired to the TPF platform. Style it through the tpf-form* class names. */
export default function ContactForm({ labels, className = "" }: { labels: ContactFormLabels; className?: string }) {
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const data = Object.fromEntries(new FormData(form).entries());
    setState("sending");
    try {
      const r = await fetch(FORM_ENDPOINT, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...data, page: window.location.pathname }),
      });
      if (!r.ok) throw new Error(String(r.status));
      form.reset();
      setState("sent");
    } catch {
      setState("error");
    }
  }

  return (
    <form className={("tpf-form " + className).trim()} onSubmit={onSubmit}>
      <label className="tpf-form__field">
        <span>{labels.name}</span>
        <input name="name" type="text" autoComplete="name" required maxLength={200} />
      </label>
      <label className="tpf-form__field">
        <span>{labels.contact}</span>
        <input name="contact" type="text" autoComplete="email" required maxLength={200} />
      </label>
      <label className="tpf-form__field">
        <span>{labels.message}</span>
        <textarea name="message" rows={5} required maxLength={5000} />
      </label>
      <label aria-hidden="true" style={{ position: "absolute", left: "-9999px", width: 1, height: 1, overflow: "hidden" }}>
        Website<input name="website" type="text" tabIndex={-1} autoComplete="off" />
      </label>
      <button className="tpf-form__submit" type="submit" disabled={state === "sending"}>
        {state === "sending" ? labels.sending : labels.submit}
      </button>
      <p className="tpf-form__status" role="status" aria-live="polite" data-state={state}>
        {state === "sent" ? labels.success : state === "error" ? labels.error : ""}
      </p>
      {labels.consent ? <p className="tpf-form__consent">{labels.consent}</p> : null}
    </form>
  );
}
'''

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
    "needs app/x/page.tsx. Use next/link for internal links. "
    "A contact or request form is the one dynamic feature available, and only through the platform's "
    "ready component: `import ContactForm from '@/tpf/ContactForm'` and render "
    "`<ContactForm labels={{ name, contact, message, submit, sending, success, error, consent }} />` with every "
    "label written in the site's language (consent is optional). It has exactly three fields (name, contact, "
    "message), sends the message to the site owner's e-mail and shows the success or error text itself. Do not "
    "write files under tpf/, do not build your own form, fetch call or mailto form, and do not add other fields. "
    "Style it in app/globals.css through the class names tpf-form, tpf-form__field (a label holding a span and "
    "an input or textarea), tpf-form__submit, tpf-form__status (has data-state idle|sending|sent|error) and "
    "tpf-form__consent."
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


def platform_files(form_endpoint: str = "") -> dict[str, str]:
    """Everything the platform owns in a site: build configuration plus the contact form and its endpoint."""
    return TEMPLATE_FILES | {
        "tpf/ContactForm.tsx": CONTACT_FORM,
        "tpf/config.ts": f"export const FORM_ENDPOINT = {json.dumps(form_endpoint)};\n",
    }


def form_endpoint(base_url: str, order_id: int) -> str:
    return f"{base_url.rstrip('/')}/forms/{order_id}"


def materialize(root: Path, files: list[dict[str, str]], form_endpoint: str = "") -> Path:
    """Write template + generated files into `root` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    for path, content in platform_files(form_endpoint).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
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


def all_files(files: list[dict[str, str]], form_endpoint: str = "") -> list[dict[str, str]]:
    """Template + generated files, as the deployer and git publisher need them."""
    return [{"path": p, "content": c} for p, c in platform_files(form_endpoint).items()] + list(files)
