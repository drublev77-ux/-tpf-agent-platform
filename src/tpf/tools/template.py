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

type Sql = ReturnType<typeof postgres>;

// The database is reached through a transaction pooler, hence prepare: false. The connection's role sees
// only this application's own schema.
//
// Serverless instances are frozen between requests, and a connection left open across a freeze can be dead
// without the driver knowing: the next query on it would hang forever. So connections are reused only while
// the instance is busy; after a few idle seconds the next query starts from fresh connections.
const IDLE_MS = 5000;

declare global {
  // eslint-disable-next-line no-var
  var __tpfDb: { sql: Sql; used: number } | undefined;
}

function connect(): Sql {
  return postgres(process.env.DATABASE_URL ?? "postgres://missing:missing@127.0.0.1:1/missing", {
    prepare: false, max: 5, idle_timeout: 10, connect_timeout: 10, fetch_types: false,
  });
}

function current(): Sql {
  const now = Date.now();
  const held = globalThis.__tpfDb;
  if (held && now - held.used < IDLE_MS) {
    held.used = now;
    return held.sql;
  }
  if (held) void held.sql.end({ timeout: 5 }).catch(() => {});
  const fresh = connect();
  globalThis.__tpfDb = { sql: fresh, used: now };
  return fresh;
}

/** The `postgres` tagged template: sql`select ... where id = ${id}`, sql.begin(...), sql(values). */
export const sql: Sql = new Proxy(function () {} as unknown as Sql, {
  apply: (_target, _self, args) => Reflect.apply(current() as unknown as (...a: unknown[]) => unknown, undefined, args),
  get: (_target, prop) => {
    const live = current() as unknown as Record<string | symbol, unknown>;
    const value = live[prop];
    return typeof value === "function" ? (value as (...a: unknown[]) => unknown).bind(live) : value;
  },
});
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

# --- Telegram bots: an application whose main interface is a chat; Telegram calls the webhook below ---

BOT_TELEGRAM = '''import "server-only";
import { createHash } from "crypto";
import { sql } from "@/tpf/db";

// The bot's token is kept by the platform in a table this application can only read.
let cached: { token: string; username: string; at: number } | null = null;

async function secrets(): Promise<{ token: string; username: string }> {
  if (cached && Date.now() - cached.at < 60_000) return cached;
  const rows = await sql`select key, value from tpf_secrets where key in ('bot_token', 'bot_username')`;
  const get = (k: string) => String(rows.find((r) => r.key === k)?.value ?? "");
  cached = { token: get("bot_token"), username: get("bot_username"), at: Date.now() };
  return cached;
}

/** The bot's @username without the @, or "" while the owner has not connected a bot yet. */
export async function botUsername(): Promise<string> {
  try {
    return (await secrets()).username;
  } catch {
    return "";
  }
}

/** The value Telegram sends in X-Telegram-Bot-Api-Secret-Token; the platform registers the same one. */
export function webhookSecret(): string {
  return createHash("sha256").update(`tg:${process.env.SESSION_SECRET ?? ""}`).digest("hex");
}

export type TgUser = { id: number; first_name?: string; last_name?: string; username?: string; language_code?: string };
export type TgChat = { id: number; type: string };
export type TgMessage = { message_id: number; chat: TgChat; from?: TgUser; text?: string; contact?: { phone_number: string } };
export type TgCallbackQuery = { id: string; from: TgUser; message?: TgMessage; data?: string };
export type TgUpdate = { update_id: number; message?: TgMessage; callback_query?: TgCallbackQuery };
export type TgButton = { text: string; callback_data?: string; url?: string };
export type TgExtra = { reply_markup?: unknown; parse_mode?: "HTML"; disable_web_page_preview?: boolean };

/** Call any Bot API method. Returns the result, or null when Telegram refused (the reason is logged). */
export async function tg<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T | null> {
  const { token } = await secrets();
  if (!token) return null;
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(params),
      signal: AbortSignal.timeout(8000),
    });
    const body = (await r.json()) as { ok: boolean; result?: T; description?: string };
    if (!body.ok) console.error(`telegram ${method}: ${body.description}`);
    return body.ok ? (body.result as T) : null;
  } catch (e) {
    console.error(`telegram ${method}: ${e instanceof Error ? e.name : "error"}`);
    return null;
  }
}

/** Send a text message. `buttons` is rows of inline buttons. */
export function sendMessage(chatId: number, text: string, buttons?: TgButton[][], extra: TgExtra = {}) {
  return tg<TgMessage>("sendMessage", {
    chat_id: chatId, text: text.slice(0, 4096), ...(buttons ? { reply_markup: { inline_keyboard: buttons } } : {}), ...extra,
  });
}

/** Replace the text and buttons of a message the bot sent earlier. */
export function editMessage(chatId: number, messageId: number, text: string, buttons?: TgButton[][]) {
  return tg("editMessageText", {
    chat_id: chatId, message_id: messageId, text: text.slice(0, 4096),
    ...(buttons ? { reply_markup: { inline_keyboard: buttons } } : {}),
  });
}

/** Stop the spinner on a pressed inline button; `text` shows as a short toast. */
export function answerCallback(id: string, text?: string) {
  return tg("answerCallbackQuery", { callback_query_id: id, ...(text ? { text: text.slice(0, 200) } : {}) });
}
'''

BOT_WEBHOOK = '''import { timingSafeEqual } from "crypto";
import { handleUpdate } from "@/lib/bot";
import { webhookSecret, type TgUpdate } from "@/tpf/telegram";

export const dynamic = "force-dynamic";
export const maxDuration = 30;

// Telegram delivers every message and button press here. Only requests carrying the secret the platform
// registered with Telegram are accepted.
export async function POST(request: Request) {
  const given = Buffer.from(request.headers.get("x-telegram-bot-api-secret-token") ?? "");
  const expected = Buffer.from(webhookSecret());
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
    return new Response("forbidden", { status: 403 });
  }
  try {
    await handleUpdate((await request.json()) as TgUpdate);
  } catch (e) {
    // Answer 200 anyway: otherwise Telegram redelivers the same update again and again.
    console.error("bot update failed:", e instanceof Error ? e.message : "error");
  }
  return Response.json({ ok: true });
}
'''

BOT_WEBHOOK_PATH = "app/api/tpf-telegram/route.ts"
BOT_HANDLER_PATH = "lib/bot.ts"

BOT_RULES = (
    "TELEGRAM BOT: this application's main interface is a Telegram bot. The platform owns the webhook "
    "(app/api/tpf-telegram/route.ts) and `@/tpf/telegram`; you write lib/bot.ts, which must export "
    "`async function handleUpdate(update: TgUpdate): Promise<void>` and handle `update.message` (text, commands "
    "such as /start and /help, a shared contact) and `update.callback_query` (inline button presses; always call "
    "`answerCallback(query.id)`). Talk to Telegram only through `import { sendMessage, editMessage, "
    "answerCallback, tg, botUsername, type TgUpdate, type TgButton } from '@/tpf/telegram'`: "
    "`sendMessage(chatId, text, buttons?)` where buttons is rows of `{ text, callback_data }` or `{ text, url }` "
    "(callback_data at most 64 bytes); `tg(method, params)` for any other Bot API method. Never read or print the "
    "bot token and never call api.telegram.org yourself. "
    "The code runs on serverless functions: nothing is kept in memory between updates, so every step of a dialog "
    "(which question the user is on, what they have answered so far) is stored in the database, keyed by the "
    "Telegram chat id (BIGINT). Handle each update in a few seconds; no polling, timers or background work. "
    "Unknown text gets a short hint with the main menu; /start always resets the dialog and shows the menu. Every "
    "text the bot sends is plain text in the bot's language (no Markdown) unless you pass parse_mode HTML and "
    "escape user input. Treat everything a user sends as untrusted: trim it, limit its length and store it through "
    "${} parameters. Keep a users table (chat id, name, username, first seen) so the owner can see who uses the bot. "
    "To notify the owner in Telegram about new records, let the owner link their own chat: the admin area shows a "
    "one-time code, the owner sends `/link CODE` to the bot, and the bot stores that chat id as an owner chat; "
    "send notifications only to linked owner chats. Messages to many users at once are sent from an admin server "
    "action to at most 25 recipients per call, with a 'send next batch' button for the rest. "
    "Table names starting with tpf_ belong to the platform: do not create, read or change them. "
    "The web part is small: the home page '/' names the bot and links to it with "
    "`https://t.me/${await botUsername()}` (show a neutral text while botUsername() is empty), and /admin lets the "
    "owner see and manage the bot's data and edit the texts and lists the spec says the owner controls. "
)

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
    app = has_database(kind)
    if not _SAFE_PATH.match(path) or ".." in path.split("/") or path.startswith("/"):
        return f"unsafe path: {path}"
    if not path.startswith(APP_PREFIXES if app else ALLOWED_PREFIXES):
        return f"path outside allowed folders: {path}"
    if not path.endswith(APP_SUFFIXES if app else ALLOWED_SUFFIXES):
        return f"file type not allowed: {path}"
    if path.startswith(("app/api/tpf-health/", "app/api/tpf-telegram/")):
        return f"path reserved by the platform: {path}"
    if path.startswith("db/") and path != SCHEMA_PATH:
        return f"the only file allowed under db/ is {SCHEMA_PATH}: {path}"
    return None


def rules_for(kind: str) -> str:
    """The template rules an agent works under: a static site, or an application with a database."""
    if kind == "bot":
        return APP_RULES + BOT_RULES
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
    if has_database(kind):
        files |= {
            "package.json": APP_PACKAGE, "next.config.mjs": APP_NEXT_CONFIG,
            "tpf/db.ts": APP_DB, "tpf/auth.ts": APP_AUTH, HEALTH_PATH: APP_HEALTH,
            "vercel.json": '{ "regions": ["fra1"] }\n',   # next to the database, not across an ocean from it
            ".env.example": "DATABASE_URL=postgres://user:password@host:6543/postgres\nSESSION_SECRET=\nADMIN_PASSWORD=\n",
        }
    if kind == "bot":
        files |= {"tpf/telegram.ts": BOT_TELEGRAM, BOT_WEBHOOK_PATH: BOT_WEBHOOK}
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
    """What the order is: 'site' (static pages), 'app' (database and admin area) or 'bot' (a Telegram bot
    with a database and admin area)."""
    kind = (spec or {}).get("product_type")
    return kind if kind in ("app", "bot") else "site"


def has_database(kind: str) -> bool:
    return kind in ("app", "bot")
