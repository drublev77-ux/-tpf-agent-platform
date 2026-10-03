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

# --- web applications: the same project with a server, a Postgres database and an owner-only admin area ---

APP_PACKAGE = json.dumps({
    "name": "tpf-app",
    "private": True,
    "version": "1.0.0",
    "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
    "dependencies": {"next": "^16.3.6", "react": "^19.0.0", "react-dom": "^19.0.0", "postgres": "^3.4.5"},
    "devDependencies": {"typescript": "^5.4.0", "@types/node": "^22.0.0", "@types/react": "^19.0.0"},
}, indent=2) + "\n"

APP_NEXT_CONFIG = "/** @type {import('next').NextConfig} */\nconst nextConfig = { images: { unoptimized: true }, poweredByHeader: false };\nexport default nextConfig;\n"

APP_DB = '''import "server-only";
import postgres from "postgres";

// One small pool per server instance. The database is reached through a transaction pooler, hence prepare: false.
// The connection's role sees only this application's own schema.
declare global {
  // eslint-disable-next-line no-var
  var __tpfSql: ReturnType<typeof postgres> | undefined;
}

export const sql =
  globalThis.__tpfSql ??
  (globalThis.__tpfSql = postgres(process.env.DATABASE_URL ?? "postgres://missing:missing@127.0.0.1:1/missing", {
    prepare: false, max: 3, idle_timeout: 20, connect_timeout: 10,
  }));
'''

APP_AUTH = '''import "server-only";
import { createHmac, timingSafeEqual } from "crypto";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

// Owner-only admin area: one password (ADMIN_PASSWORD), a signed session cookie.
const COOKIE = "tpf_admin";
const TTL_SEC = 60 * 60 * 24 * 14;

function secret(): string {
  const s = process.env.SESSION_SECRET;
  if (!s) throw new Error("SESSION_SECRET is not set");
  return s;
}

function sign(exp: number): string {
  return `${exp}.${createHmac("sha256", secret()).update(`admin:${exp}`).digest("hex")}`;
}

function same(a: string, b: string): boolean {
  const x = Buffer.from(a), y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

/** True when the visitor holds a valid admin session. */
export async function isAdmin(): Promise<boolean> {
  const value = (await cookies()).get(COOKIE)?.value ?? "";
  const exp = Number(value.split(".")[0]);
  return Number.isFinite(exp) && exp > Date.now() / 1000 && same(value, sign(exp));
}

/** Call at the top of every admin page, admin route handler and admin server action. */
export async function requireAdmin(): Promise<void> {
  if (!(await isAdmin())) redirect("/admin/login");
}

/** Check the password and start a session. Use only in a server action or route handler. */
export async function signIn(password: string): Promise<boolean> {
  const expected = process.env.ADMIN_PASSWORD ?? "";
  if (!expected || !same(password, expected)) return false;
  const exp = Math.floor(Date.now() / 1000) + TTL_SEC;
  (await cookies()).set(COOKIE, sign(exp), { httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: TTL_SEC });
  return true;
}

/** End the session. Use only in a server action or route handler. */
export async function signOut(): Promise<void> {
  (await cookies()).delete(COOKIE);
}
'''

APP_HEALTH = '''import { sql } from "@/tpf/db";

export const dynamic = "force-dynamic";

// The platform calls this after every deployment to see that the application reaches its database.
export async function GET() {
  try {
    await sql`select 1`;
    return Response.json({ db: "ok" });
  } catch {
    return Response.json({ db: "error" }, { status: 500 });
  }
}
'''

HEALTH_PATH = "app/api/tpf-health/route.ts"
SCHEMA_PATH = "db/schema.sql"

APP_RULES = (
    "Project template (fixed by the platform, do not write these files): Next.js 16 App Router, React 19, "
    "TypeScript, deployed on Vercel with a Node.js server; dependencies are next, react and `postgres` only, no "
    "other packages. Style with plain CSS in app/globals.css or CSS modules. You may write files only under app/, "
    "components/, lib/, public/ and db/. app/layout.tsx, app/page.tsx and db/schema.sql are required; each route "
    "'/x' needs app/x/page.tsx. Use next/link for internal links. "
    "DATABASE: PostgreSQL, reached only through `import { sql } from '@/tpf/db'` (the `postgres` tagged template: "
    "sql`select * from bookings where id = ${id}`; values are always interpolated as ${parameters}, never built "
    "into the query text by string concatenation, and never sql.unsafe). Use it only in server code: server "
    "components, server actions ('use server') and route handlers — never in a 'use client' file. db/schema.sql "
    "creates every table and index the code uses. The platform runs it before each deployment, on a database that "
    "may already hold live data, so it must be idempotent and never destroy data: CREATE TABLE IF NOT EXISTS, "
    "CREATE INDEX IF NOT EXISTS, ALTER TABLE ... ADD COLUMN IF NOT EXISTS; no DROP, TRUNCATE or DELETE, no "
    "schema names, no extensions, roles or grants. Table names are plain lowercase identifiers. "
    "Pages that read the database must not be prerendered at build time: put `export const dynamic = "
    "'force-dynamic'` in every page, layout or route handler that queries it. "
    "ADMIN AREA: the owner manages the data under /admin, protected by one password. Use the platform's "
    "`import { requireAdmin, isAdmin, signIn, signOut } from '@/tpf/auth'`: `await requireAdmin()` is the first "
    "line of every page under /admin (except /admin/login), of every admin route handler and of every server "
    "action that changes or reads admin data; /admin/login has a password form whose server action calls "
    "`signIn(password)` and redirects to /admin on success; a sign-out button calls `signOut()`. Do not write your "
    "own authentication, password storage or user accounts. "
    "Validate and trim every input on the server (length limits, allowed values) before it reaches the database; "
    "public forms get a hidden honeypot field. Mutations go through server actions followed by revalidatePath or "
    "redirect. Never expose environment variables, the admin password or database errors to the browser. "
    "Do not write files under tpf/ or app/api/tpf-health/. Not available: e-mail sending, file uploads, payments, "
    "visitor accounts, external APIs, scheduled jobs. "
)

ALLOWED_PREFIXES = ("app/", "components/", "lib/", "public/")
APP_PREFIXES = ALLOWED_PREFIXES + ("db/",)
APP_SUFFIXES = (".tsx", ".ts", ".css", ".svg", ".txt", ".json", ".webmanifest", ".ico", ".sql")
ALLOWED_SUFFIXES = (".tsx", ".ts", ".css", ".svg", ".txt", ".json", ".webmanifest", ".ico")
MAX_FILE_BYTES = 200_000
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_\-./\[\]()]+$")

CONTACT_FORM_RULES = (
    "A contact or request form whose messages go to the owner's e-mail is available, only through the platform's "
    "ready component: `import ContactForm from '@/tpf/ContactForm'` and render "
    "`<ContactForm labels={{ name, contact, message, submit, sending, success, error, consent }} />` with every "
    "label written in the site's language (consent is optional). It has exactly three fields (name, contact, "
    "message), sends the message to the site owner's e-mail and shows the success or error text itself. Do not "
    "write files under tpf/, do not build your own form, fetch call or mailto form, and do not add other fields. "
    "Style it in app/globals.css through the class names tpf-form, tpf-form__field (a label holding a span and "
    "an input or textarea), tpf-form__submit, tpf-form__status (has data-state idle|sending|sent|error) and "
    "tpf-form__consent."
)

TEMPLATE_RULES = (
    "Project template (fixed by the platform, do not write these files): Next.js 16 App Router, "
    "React 19, TypeScript, static export (`output: 'export'`), no API routes, no server actions, no "
    "external packages beyond next/react. Style with plain CSS in app/globals.css or CSS modules. "
    "You may write files only under app/, components/, lib/, public/ with extensions "
    + ", ".join(ALLOWED_SUFFIXES) + ". app/layout.tsx and app/page.tsx are required; each route '/x' "
    "needs app/x/page.tsx. Use next/link for internal links. The site has no server of its own. "
    + CONTACT_FORM_RULES
)


def validate_path(path: str, kind: str = "site") -> str | None:
    """Return an error message, or None if the generated file path is acceptable."""
    app = kind == "app"
    if not _SAFE_PATH.match(path) or ".." in path.split("/") or path.startswith("/"):
        return f"unsafe path: {path}"
    if not path.startswith(APP_PREFIXES if app else ALLOWED_PREFIXES):
        return f"path outside allowed folders: {path}"
    if not path.endswith(APP_SUFFIXES if app else ALLOWED_SUFFIXES):
        return f"file type not allowed: {path}"
    if path.startswith("app/api/tpf-health/"):
        return f"path reserved by the platform: {path}"
    if path.startswith("db/") and path != SCHEMA_PATH:
        return f"the only file allowed under db/ is {SCHEMA_PATH}: {path}"
    return None


def rules_for(kind: str) -> str:
    """The template rules an agent works under: a static site, or an application with a database."""
    return (APP_RULES + CONTACT_FORM_RULES) if kind == "app" else TEMPLATE_RULES


def route_to_page(route: str) -> str:
    route = route.strip("/")
    return "app/page.tsx" if not route else f"app/{route}/page.tsx"


def platform_files(form_endpoint: str = "", kind: str = "site") -> dict[str, str]:
    """Everything the platform owns in a project: build configuration, the contact form and its endpoint,
    and for an application the database client, the admin session helper and the health check."""
    files = TEMPLATE_FILES | {
        "tpf/ContactForm.tsx": CONTACT_FORM,
        "tpf/config.ts": f"export const FORM_ENDPOINT = {json.dumps(form_endpoint)};\n",
    }
    if kind == "app":
        files |= {
            "package.json": APP_PACKAGE, "next.config.mjs": APP_NEXT_CONFIG,
            "tpf/db.ts": APP_DB, "tpf/auth.ts": APP_AUTH, HEALTH_PATH: APP_HEALTH,
            ".env.example": "DATABASE_URL=postgres://user:password@host:6543/postgres\nSESSION_SECRET=\nADMIN_PASSWORD=\n",
        }
    return files


def form_endpoint(base_url: str, order_id: int) -> str:
    return f"{base_url.rstrip('/')}/forms/{order_id}"


def materialize(root: Path, files: list[dict[str, str]], form_endpoint: str = "", kind: str = "site") -> Path:
    """Write template + generated files into `root` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    for path, content in platform_files(form_endpoint, kind).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    for f in files:
        err = validate_path(f["path"], kind)
        if err:
            raise ValueError(err)
        target = (root / f["path"]).resolve()
        if root.resolve() not in target.parents:
            raise ValueError(f"path escapes workspace: {f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
    return root


def all_files(files: list[dict[str, str]], form_endpoint: str = "", kind: str = "site") -> list[dict[str, str]]:
    """Template + generated files, as the deployer and git publisher need them."""
    return [{"path": p, "content": c} for p, c in platform_files(form_endpoint, kind).items()] + list(files)


def kind_of(spec: dict | None) -> str:
    """'app' for an order whose spec asks for an application with a database, else 'site'."""
    return "app" if (spec or {}).get("product_type") == "app" else "site"
