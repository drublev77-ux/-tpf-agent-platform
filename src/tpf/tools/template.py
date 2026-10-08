"""Fixed project template for pilot sites. Agents write only app/, components/, lib/ and public/;
the build configuration below is owned by the platform so a model cannot break it."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from tpf.tools.imagegen import IMAGES_RULES, PUBLIC_DIR as GEN_DIR
from tpf.tools.template_extras import (APP_AUTO_REFRESH, APP_FILE_INPUT, APP_FILES, APP_GEO, APP_GEO_FORM, CHAT_RULES, FILES_PATH,
                                       GEO_RULES, UPLOADS_RULES)
from tpf.tools.template_caps import (APP_AI, APP_MAIL, APP_PDF, APP_PDF_FONT, APP_PLATFORM, APP_SERVICES, APP_SHEETS, APP_TICK,
                                     CAPS_PACKAGES, CAPS_RULES, SCHEDULE_PATH, SEO_CONFIG_PATH, SEO_FILES, SEO_RULES, TICK_PATH)
from tpf.tools.shop_payments_ts import (SHOP_PAY_CHECK, SHOP_PAY_CHECK_PATH, SHOP_PAY_NOTIFY, SHOP_PAY_NOTIFY_PATH,
                                         SHOP_PAY_RETURN, SHOP_PAY_RETURN_PATH, SHOP_PAYMENTS)

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

APP_PACKAGE_CAPS = json.dumps(json.loads(APP_PACKAGE) | {
    "dependencies": json.loads(APP_PACKAGE)["dependencies"] | CAPS_PACKAGES}, indent=2) + "\n"

APP_NEXT_CONFIG = ("/** @type {import('next').NextConfig} */\nconst nextConfig = { images: { unoptimized: true }, poweredByHeader: false,\n"
                   "  experimental: { serverActions: { bodySizeLimit: '5mb' } } };   // a form may carry a photo or a PDF\nexport default nextConfig;\n")

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
    prepare: false, max: 5, idle_timeout: 10, connect_timeout: 10,   // fetch_types stays on: arrays need it
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

# --- applications with user accounts: people the owner adds sign in with e-mail and password, each with a role.
# Everything security-sensitive (password hashing, sessions, lockout, the owner check) lives in this file; the
# generated code only renders forms and calls it. ---

APP_USERS = '''import "server-only";
import { createHmac, randomBytes, scrypt, timingSafeEqual } from "crypto";
import { cache } from "react";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ROLES } from "@/lib/roles";
import { isAdmin } from "@/tpf/auth";
import { sql } from "@/tpf/db";

export type User = { id: number; email: string; name: string; role: string };
export type ManagedUser = User & { active: boolean; createdAt: Date; lastLoginAt: Date | null };

const COOKIE = "tpf_user";
const TTL_SEC = 60 * 60 * 24 * 30;
const MAX_FAILED = 8;                      // wrong passwords in a row before the account rests for 15 minutes
const NO_SALT = Buffer.alloc(16);

function secret(): string {
  const s = process.env.SESSION_SECRET;
  if (!s) throw new Error("SESSION_SECRET is not set");
  return s;
}

function sign(id: number, exp: number, version: number): string {
  return createHmac("sha256", secret()).update(`user:${id}:${exp}:${version}`).digest("hex");
}

function same(a: string, b: string): boolean {
  const x = Buffer.from(a), y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

function derive(password: string, salt: Buffer): Promise<Buffer> {
  return new Promise((resolve, reject) =>
    scrypt(password.normalize("NFKC"), salt, 64, { N: 16384, r: 8, p: 1 }, (e, key) => (e ? reject(e) : resolve(key))));
}

async function makeHash(password: string): Promise<string> {
  const salt = randomBytes(16);
  return `scrypt$${salt.toString("base64")}$${(await derive(password, salt)).toString("base64")}`;
}

async function matches(password: string, stored: string): Promise<boolean> {
  const [kind, salt, hash] = stored.split("$");
  if (kind !== "scrypt" || !salt || !hash) {
    await derive(password, NO_SALT);       // spend the same time whether or not the account exists
    return false;
  }
  const want = Buffer.from(hash, "base64"), got = await derive(password, Buffer.from(salt, "base64"));
  return got.length === want.length && timingSafeEqual(got, want);
}

function newPassword(): string {
  const letters = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  return Array.from(randomBytes(12), (b) => letters[b % letters.length]).join("");
}

function knownRole(role: string): boolean {
  return ROLES.some((r) => r.id === role);
}

async function startSession(id: number, version: number): Promise<void> {
  const exp = Math.floor(Date.now() / 1000) + TTL_SEC;
  (await cookies()).set(COOKIE, `${id}.${exp}.${version}.${sign(id, exp, version)}`,
    { httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: TTL_SEC });
}

/** The signed-in user, or null. Safe to call many times in one request. */
export const currentUser = cache(async (): Promise<User | null> => {
  const [idText, expText, versionText, signature] = ((await cookies()).get(COOKIE)?.value ?? "").split(".");
  const id = Number(idText), exp = Number(expText), version = Number(versionText);
  if (!Number.isInteger(id) || !Number.isInteger(version) || !Number.isFinite(exp) || exp < Date.now() / 1000) return null;
  if (!signature || !same(signature, sign(id, exp, version))) return null;
  const rows = await sql<User[]>`select id, email, name, role from tpf_users
    where id = ${id} and active and session_version = ${version}`;
  return rows[0] && knownRole(rows[0].role) ? rows[0] : null;
});

/** First line of every page, route handler and server action for signed-in people. With roles given, only
 *  those roles pass. Sends everyone else to /login (not signed in) or / (signed in, wrong role). */
export async function requireUser(...roles: string[]): Promise<User> {
  const user = await currentUser();
  if (!user) redirect("/login");
  if (roles.length > 0 && !roles.includes(user.role)) redirect("/");
  return user;
}

/** Check e-mail and password and start a session. Use only in a server action or route handler. */
export async function signInUser(email: string, password: string): Promise<"ok" | "wrong" | "locked"> {
  const address = email.trim().toLowerCase().slice(0, 200);
  const rows = await sql<{ id: number; password_hash: string; active: boolean; session_version: number; locked: boolean }[]>`
    select id, password_hash, active, session_version, coalesce(locked_until > now(), false) as locked
    from tpf_users where email = ${address}`;
  const found = rows[0];
  if (!found) {
    await matches(password, "");
    return "wrong";
  }
  if (found.locked) return "locked";
  if (!found.active || !(await matches(password.slice(0, 200), found.password_hash))) {
    await sql`update tpf_users set
      locked_until = case when failed + 1 >= ${MAX_FAILED} then now() + interval '15 minutes' else null end,
      failed = case when failed + 1 >= ${MAX_FAILED} then 0 else failed + 1 end where id = ${found.id}`;
    return "wrong";
  }
  await sql`update tpf_users set failed = 0, locked_until = null, last_login_at = now() where id = ${found.id}`;
  await startSession(found.id, found.session_version);
  return "ok";
}

/** End the user's session. Use only in a server action or route handler. */
export async function signOutUser(): Promise<void> {
  (await cookies()).delete(COOKIE);
}

/** The signed-in user changes their own password (8 to 200 characters). Other devices are signed out. */
export async function changeOwnPassword(current: string, next: string): Promise<"ok" | "wrong" | "weak" | "signed-out"> {
  const user = await currentUser();
  if (!user) return "signed-out";
  if (next.length < 8 || next.length > 200) return "weak";
  const rows = await sql<{ password_hash: string }[]>`select password_hash from tpf_users where id = ${user.id}`;
  if (!rows[0] || !(await matches(current.slice(0, 200), rows[0].password_hash))) return "wrong";
  const updated = await sql<{ session_version: number }[]>`update tpf_users
    set password_hash = ${await makeHash(next)}, session_version = session_version + 1 where id = ${user.id}
    returning session_version`;
  await startSession(user.id, updated[0].session_version);
  return "ok";
}

/** Names and roles of the active users, for showing who a record belongs to. Any signed-in user or the owner. */
export async function directory(): Promise<{ id: number; name: string; role: string }[]> {
  if (!(await currentUser()) && !(await isAdmin())) return [];
  return sql<{ id: number; name: string; role: string }[]>`select id, name, role from tpf_users where active order by name`;
}

/** The display name of a role id from lib/roles.ts. */
export function roleName(role: string): string {
  return ROLES.find((r) => r.id === role)?.name ?? role;
}

// --- the owner manages the accounts from the admin area; each of these checks the owner's session itself ---

async function ownerOnly(): Promise<void> {
  if (!(await isAdmin())) throw new Error("owner session required");
}

export async function listUsers(): Promise<ManagedUser[]> {
  await ownerOnly();
  return sql<ManagedUser[]>`select id, email, name, role, active, created_at as "createdAt", last_login_at as "lastLoginAt"
    from tpf_users order by active desc, name`;
}

/** Add an account. The password is made here and returned once: show it to the owner to pass on. */
export async function createUser(input: { email: string; name: string; role: string }):
    Promise<{ ok: true; id: number; password: string } | { ok: false; error: "email" | "name" | "role" | "exists" }> {
  await ownerOnly();
  const email = input.email.trim().toLowerCase(), name = input.name.trim();
  if (email.length > 200 || !/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email)) return { ok: false, error: "email" };
  if (name.length < 1 || name.length > 100) return { ok: false, error: "name" };
  if (!knownRole(input.role)) return { ok: false, error: "role" };
  const password = newPassword();
  const rows = await sql<{ id: number }[]>`insert into tpf_users (email, name, role, password_hash)
    values (${email}, ${name}, ${input.role}, ${await makeHash(password)}) on conflict (email) do nothing returning id`;
  return rows[0] ? { ok: true, id: rows[0].id, password } : { ok: false, error: "exists" };
}

/** Change a user's name, role or whether they may sign in. A change of role or access signs them out. */
export async function updateUser(id: number, change: { name?: string; role?: string; active?: boolean }):
    Promise<{ ok: true } | { ok: false; error: "name" | "role" | "missing" }> {
  await ownerOnly();
  const name = change.name?.trim();
  if (name !== undefined && (name.length < 1 || name.length > 100)) return { ok: false, error: "name" };
  if (change.role !== undefined && !knownRole(change.role)) return { ok: false, error: "role" };
  const rows = await sql<{ id: number }[]>`update tpf_users set
      name = coalesce(${name ?? null}, name),
      role = coalesce(${change.role ?? null}, role),
      active = coalesce(${change.active ?? null}, active),
      session_version = session_version + ${change.role !== undefined || change.active !== undefined ? 1 : 0}
    where id = ${id} returning id`;
  return rows[0] ? { ok: true } : { ok: false, error: "missing" };
}

/** Give a user a new password (returned once) and sign them out everywhere. */
export async function resetUserPassword(id: number): Promise<{ ok: true; password: string } | { ok: false }> {
  await ownerOnly();
  const password = newPassword();
  const rows = await sql<{ id: number }[]>`update tpf_users set password_hash = ${await makeHash(password)},
      session_version = session_version + 1, failed = 0, locked_until = null where id = ${id} returning id`;
  return rows[0] ? { ok: true, password } : { ok: false };
}
'''

USERS_PATH = "tpf/users.ts"
ROLES_PATH = "lib/roles.ts"

ACCOUNTS_RULES = (
    "USER ACCOUNTS: this application has people who sign in, each with one role from the architecture's roles. "
    "lib/roles.ts exports `export const ROLES: { id: string; name: string }[]` with exactly those roles (ids as in "
    "the architecture, names in the site's language). Accounts, passwords and sessions belong to the platform: "
    "`import { currentUser, requireUser, signInUser, signOutUser, changeOwnPassword, directory, roleName, listUsers, "
    "createUser, updateUser, resetUserPassword, type User, type ManagedUser } from '@/tpf/users'`. "
    "`const user = await requireUser()` (any signed-in user) or `await requireUser('manager', 'foreman')` (only "
    "those roles) is the first line of every page, route handler and server action that is not public — a server "
    "action checks it again itself, the page's check does not cover it. A user is `{ id, email, name, role }`. "
    "/login has an e-mail and password form whose server action calls `signInUser(email, password)`: 'ok' → "
    "redirect to the user's start page, 'wrong' → one message that does not say which of the two was wrong, "
    "'locked' → 'too many attempts, try again in 15 minutes'. A sign-out button calls `signOutUser()`. /account "
    "shows the user's name, e-mail and role and a change-password form calling `changeOwnPassword(current, next)` "
    "('ok', 'wrong', 'weak' = under 8 characters). There is no self-registration and no password reset by e-mail: "
    "the owner adds people at /admin/users (behind requireAdmin) with `listUsers()`, `createUser({ email, name, "
    "role })` — it returns `{ ok: true, password }`: show that password once on the page for the owner to pass on, "
    "never store or log it — `updateUser(id, { name, role, active })` to rename, change role or block, and "
    "`resetUserPassword(id)` which again returns a password to show once. The owner (the /admin password) is not a "
    "user: /admin pages keep using requireAdmin, user pages use requireUser, and neither opens the other. "
    "In your own tables refer to a person by `user_id INTEGER NOT NULL` (the id from currentUser; no foreign key, "
    "and never query tpf_users yourself); to show names use `directory()`, which returns `{ id, name, role }` of "
    "the active users. DATA BELONGS TO SOMEONE: every query run for a signed-in user is narrowed by what that role "
    "may see — a worker's own tasks by `user_id = ${user.id}`, a manager's by nothing — and every action that "
    "changes a record first checks that this user may change that record; an id from the browser is never trusted "
    "by itself. Do not write password fields into db/schema.sql, do not hash or compare passwords, do not read or "
    "set the tpf_user cookie. "
)

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

# --- every application: a way to tell the owner that something happened (an e-mail through the platform) ---

APP_NOTIFY = '''import "server-only";
import { FORM_ENDPOINT } from "@/tpf/config";

/** E-mail the owner about a new record (a booking, an order, a request). Never throws; at most 30 an hour. */
export async function notifyOwner(subject: string, text: string, contact = "—"): Promise<boolean> {
  if (!FORM_ENDPOINT) return false;
  if ((globalThis as { __tpfMuteNotify?: boolean }).__tpfMuteNotify) return true;   // the platform is checking the shop
  try {
    const r = await fetch(FORM_ENDPOINT, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: subject.slice(0, 200), contact: contact.slice(0, 200) || "—", message: text.slice(0, 5000) }),
      signal: AbortSignal.timeout(8000),
    });
    return r.ok;
  } catch {
    return false;
  }
}
'''

# --- online shops: an application that takes card payments through the owner's own account with a payment
# provider (Stripe, YooKassa or bePaid). The payment module itself is in shop_payments_ts.py. ---

SHOP_WEBHOOK = '''import { onPaid } from "@/lib/payments";
import { verifyStripeEvent, type Payment } from "@/tpf/payments";

export const dynamic = "force-dynamic";
export const maxDuration = 30;

// Stripe calls this when a customer has paid. Only events signed with the owner's webhook secret are accepted.
export async function POST(request: Request) {
  const payload = await request.text();
  const event = await verifyStripeEvent(payload, request.headers.get("stripe-signature") ?? "");
  if (!event) return new Response("forbidden", { status: 403 });
  const type = String(event.type ?? "");
  const session = ((event.data as { object?: Record<string, unknown> } | undefined)?.object ?? {}) as Record<string, unknown>;
  const paid = (type === "checkout.session.completed" && session.payment_status === "paid")
    || type === "checkout.session.async_payment_succeeded";
  if (paid && typeof session.client_reference_id === "string") {
    const payment: Payment = {
      sessionId: String(session.id ?? ""), amountTotal: Number(session.amount_total ?? 0),
      currency: String(session.currency ?? "").toLowerCase(),
      email: String((session.customer_details as { email?: string } | undefined)?.email ?? ""),
    };
    try {
      await onPaid(session.client_reference_id, payment);
    } catch (e) {
      console.error("onPaid failed:", e instanceof Error ? e.message : "error");
      return new Response("retry", { status: 500 });   // Stripe delivers the event again
    }
  }
  return Response.json({ received: true });
}
'''

SHOP_WEBHOOK_PATH = "app/api/tpf-stripe/route.ts"
SHOP_HANDLER_PATH = "lib/payments.ts"

NOTIFY_RULES = (
    "To tell the owner about a new record by e-mail, call `await notifyOwner(subject, text, contact?)` from "
    "`@/tpf/notify` in the server action that creates it (it never throws and is limited to 30 e-mails an hour, "
    "so use it for records people create, not for routine events). "
)

SHOP_RULES = (
    "ONLINE SHOP: this application sells products and takes card payments through the owner's own account with "
    "a payment provider — Stripe, YooKassa or bePaid, whichever the owner connects. Your code is the same for "
    "all of them and never names one. "
    "The platform owns the payment plumbing: `import { paymentsEnabled, createCheckout, type Payment } from "
    "'@/tpf/payments'` and the routes app/api/tpf-stripe, app/api/tpf-pay and app/api/tpf-pay-return; you write "
    "lib/payments.ts, which must export `async function onPaid(reference: string, payment: Payment): Promise<void>`. "
    "`Payment` is exactly `{ sessionId: string; amountTotal: number; currency: string; email: string }`: "
    "`payment.amountTotal` is what the customer paid, an integer in the currency's smallest unit, and "
    "`payment.currency` is the lowercase ISO code ('usd', 'rub', 'byn'). Use these fields by these names, with no "
    "type casts: a guessed field name reads as undefined and every real payment would be refused. "
    "Money is always an integer in the currency's smallest unit (cents) in the database and in code; format it "
    "only when displaying. The shop has one currency, taken from the spec. "
    "Catalogue: products (name, description, price, image URL, active flag, optional stock) come from the database "
    "and are managed by the owner under /admin; product images are URLs the owner pastes, shown with a plain "
    "<img> and a neutral placeholder when empty. "
    "Cart: kept in an httpOnly cookie holding only product ids and quantities; totals are always recomputed on "
    "the server from current database prices. "
    "Checkout: a form (name, e-mail or phone, delivery details or comment as the spec says) whose server action "
    "validates the cart against the database (active, in stock), creates an order with a random unguessable "
    "token (crypto.randomUUID()), status 'pending' and an order_items snapshot of names, unit prices and "
    "quantities, then: if `await paymentsEnabled()`, calls `createCheckout({ reference: token, lines, currency, "
    "successUrl, cancelUrl, customerEmail })` with absolute URLs built from the request's host and redirects to "
    "its url (if it returns null, show an error and keep the cart); otherwise marks the order 'new', clears the "
    "cart and shows a page saying the shop will contact the customer about payment. "
    "The success page (/order/[token]) shows the order's status from the database; it never marks an order paid. "
    "Only onPaid marks it paid: `update orders set status = 'paid', ... where token = ${reference} and status = "
    "'pending'`, and only when that update changed a row does it reduce stock and call notifyOwner — the platform "
    "may report the same payment twice, so onPaid must be safe to repeat. If `payment.amountTotal` differs from the order's "
    "total, or `payment.currency` from the order's currency compared in lowercase, do not mark it paid: record the mismatch on the order and notify the owner. "
    "PAYMENT CHECK: before the shop is shown to the client, the platform runs your onPaid against throwaway "
    "orders, so lib/payments.ts also exports three small functions used only by that check (the platform's route "
    "app/api/tpf-check, which you must not write): "
    "`async function createCheckOrder(): Promise<{ reference: string; amountTotal: number; currency: string }>` "
    "inserts an order exactly as the checkout's server action would for a customer about to pay by card — status "
    "'pending', a new crypto.randomUUID() token, placeholder customer details, and one order line that refers to no "
    "product (so no stock changes) with a positive price — and returns its token, total and currency; "
    "`async function isPaid(reference: string): Promise<boolean>` tells whether that order's status is 'paid'; "
    "`async function removeCheckOrder(reference: string): Promise<void>` deletes that order and its lines. The "
    "check calls onPaid with the exact total (the order must become paid, and stay so when called again), with a "
    "different amount and with a different currency (it must not), and for an unknown reference (no error). "
    "Admin: products (create, edit, deactivate), orders (list, details, status: new, pending, paid, shipped, "
    "completed, cancelled). Refunds are made by the owner in the payment provider's own dashboard, not here. "
    "Never read or print payment keys and never call a payment provider's API yourself. Texts for customers say "
    "'card payment', not a provider's name. Not available: customer accounts, "
    "discount codes, delivery-price calculation by outside services, subscriptions, saved cards. "
)

# --- applications and shops install on a phone's home screen like an app (PWA) ---

PWA_SW = '''// Installed-app support: when the device is offline, navigation shows a small "no connection" page.
// Nothing else is cached, so pages and data are always fresh.
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open("tpf-offline-v1").then((cache) => cache.add("/offline.html")));
  self.skipWaiting();
});
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => caches.match("/offline.html")));
});
'''

PWA_OFFLINE = '''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Offline</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px/1.5 system-ui,sans-serif;background:#f6f6f4;color:#1c1c1c;text-align:center;padding:24px}
button{margin-top:16px;min-height:44px;padding:10px 22px;border-radius:999px;border:1px solid #1c1c1c;background:transparent;font:inherit;cursor:pointer}</style></head>
<body><main><p>Нет соединения с интернетом.<br>No internet connection.<br>没有网络连接。</p><button onclick="location.reload()">↻</button></main></body></html>
'''

PWA_COMPONENT = '''"use client";
import { useEffect } from "react";

/** Registers the service worker that makes the application installable. Render once, in app/layout.tsx. */
export default function Pwa() {
  useEffect(() => {
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  }, []);
  return null;
}
'''

PWA_MANIFEST = '''import type { MetadataRoute } from "next";
import { PWA } from "@/lib/pwa";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: PWA.name, short_name: PWA.shortName, start_url: "/", display: "standalone",
    background_color: PWA.backgroundColor, theme_color: PWA.themeColor,
    icons: [192, 512].map((size) => ({ src: `/tpf-icon/${size}`, sizes: `${size}x${size}`, type: "image/png" as const })),
  };
}
'''

PWA_ICON = '''import { ImageResponse } from "next/og";
import { PWA } from "@/lib/pwa";

// The application's icon, drawn from lib/pwa.ts: /tpf-icon/192, /tpf-icon/512 and /tpf-icon/180 (iPhone).
export async function GET(_request: Request, { params }: { params: Promise<{ size: string }> }) {
  const size = [180, 192, 512].includes(Number((await params).size)) ? Number((await params).size) : 192;
  const letter = /^[A-Za-z0-9]$/.test(PWA.letter) ? PWA.letter : "";
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center",
                    background: PWA.themeColor, color: PWA.backgroundColor, fontSize: size * 0.56, fontWeight: 700 }}>
        {letter || <div style={{ width: size * 0.42, height: size * 0.42, borderRadius: size, background: PWA.backgroundColor }} />}
      </div>
    ),
    { width: size, height: size, headers: { "cache-control": "public, max-age=86400" } },
  );
}
'''

PWA_CONFIG_PATH = "lib/pwa.ts"
PWA_RULES = (
    "INSTALLABLE: the application installs on a phone's home screen like an app. The platform owns the manifest, "
    "the icon route, the service worker and `@/tpf/Pwa`; you write lib/pwa.ts exporting `export const PWA = { name, "
    "shortName, themeColor, backgroundColor, letter }` (shortName at most 12 characters; colours as #rrggbb from "
    "the design; letter is one Latin letter or digit for the icon, or '' for a plain mark), render `<Pwa />` from "
    "'@/tpf/Pwa' once inside <body> in app/layout.tsx, and set in the layout's metadata `appleWebApp: { capable: "
    "true, title: PWA.shortName }` and `icons: { icon: '/tpf-icon/192', apple: '/tpf-icon/180' }`, plus `export "
    "const viewport = { themeColor: PWA.themeColor }`. Do not write app/manifest.ts, public/sw.js, "
    "public/offline.html or anything under app/tpf-icon/. Every page must be usable at 360px wide with touch "
    "targets of at least 44px. "
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
    "Do not write files under tpf/ or app/api/tpf-health/. Not available unless a later section "
    "provides it: e-mail sending, file uploads, payments, visitor accounts, external APIs, scheduled jobs. "
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
    if path.startswith(("app/api/tpf-health/", "app/api/tpf-telegram/", "app/api/tpf-stripe/", "app/api/tpf-pay/",
                        "app/api/tpf-pay-return/", "app/api/tpf-check/", "app/api/tpf-tick/", "app/tpf-icon/")) \
            or path in ("app/manifest.ts", "public/sw.js", "public/offline.html", "app/robots.ts", "app/sitemap.ts"):
        return f"path reserved by the platform: {path}"
    if path.startswith("db/") and path != SCHEMA_PATH:
        return f"the only file allowed under db/ is {SCHEMA_PATH}: {path}"
    return None


def rules_for(kind: str) -> str:
    """The template rules an agent works under: a static site, or an application with a database."""
    if kind == "bot":
        return APP_RULES + NOTIFY_RULES + BOT_RULES + SEO_RULES
    if kind == "shop":
        return APP_RULES + NOTIFY_RULES + SHOP_RULES + PWA_RULES + CONTACT_FORM_RULES + SEO_RULES + " " + IMAGES_RULES
    return (((APP_RULES + NOTIFY_RULES + PWA_RULES + CONTACT_FORM_RULES) if kind == "app" else TEMPLATE_RULES) + " " + SEO_RULES
            + " " + IMAGES_RULES)


def route_to_page(route: str) -> str:
    route = route.strip("/")
    return "app/page.tsx" if not route else f"app/{route}/page.tsx"


PWA_FILES = ("public/sw.js", "public/offline.html", "tpf/Pwa.tsx", "app/manifest.ts", "app/tpf-icon/[size]/route.tsx")


def _for_code(platform: dict[str, str], files: list[dict[str, str]]) -> dict[str, str]:
    """Platform files that fit this code: an application written before applications became installable has
    no lib/pwa.ts, and the manifest that imports it would break its build."""
    paths = {f["path"] for f in files}
    # The accounts helper imports the application's lib/roles.ts: it ships only with code that has one.
    skip = ((() if PWA_CONFIG_PATH in paths else PWA_FILES) + (() if ROLES_PATH in paths else (USERS_PATH,))
            + (() if SCHEDULE_PATH in paths else (TICK_PATH,))
            + (() if SEO_CONFIG_PATH in paths else tuple(SEO_FILES)))       # robots.ts and sitemap.ts import the site's lib/seo.ts     # the tick route imports the application's lib/schedule.ts
    return {p: c for p, c in platform.items() if p not in skip}


def platform_files(form_endpoint: str = "", kind: str = "site") -> dict[str, str]:
    """Everything the platform owns in a project: build configuration, the contact form and its endpoint,
    and for an application the database client, the admin session helper and the health check."""
    files = TEMPLATE_FILES | SEO_FILES | {
        "tpf/ContactForm.tsx": CONTACT_FORM,
        "tpf/config.ts": f"export const FORM_ENDPOINT = {json.dumps(form_endpoint)};\n",
    }
    if has_database(kind):
        files |= {
            "package.json": APP_PACKAGE, "next.config.mjs": APP_NEXT_CONFIG,
            "tpf/db.ts": APP_DB, "tpf/auth.ts": APP_AUTH, "tpf/notify.ts": APP_NOTIFY, HEALTH_PATH: APP_HEALTH,
            USERS_PATH: APP_USERS, FILES_PATH: APP_FILES, "tpf/FileInput.tsx": APP_FILE_INPUT,
            "tpf/AutoRefresh.tsx": APP_AUTO_REFRESH, "tpf/GeoForm.tsx": APP_GEO_FORM, "tpf/geo.ts": APP_GEO,
            "vercel.json": '{ "regions": ["fra1"] }\n',   # next to the database, not across an ocean from it
            ".env.example": "DATABASE_URL=postgres://user:password@host:6543/postgres\nSESSION_SECRET=\nADMIN_PASSWORD=\n",
        }
    if kind == "app":       # what an application's spec may ask for in capabilities; unused helpers cost nothing
        files |= {
            "package.json": APP_PACKAGE_CAPS, "tpf/platform.ts": APP_PLATFORM, "tpf/mail.ts": APP_MAIL, "tpf/ai.ts": APP_AI,
            "tpf/services.ts": APP_SERVICES, "tpf/pdf.ts": APP_PDF, "tpf/pdf-font.ts": APP_PDF_FONT, "tpf/sheets.ts": APP_SHEETS,
            TICK_PATH: APP_TICK,
        }
    if kind == "bot":
        files |= {"tpf/telegram.ts": BOT_TELEGRAM, BOT_WEBHOOK_PATH: BOT_WEBHOOK}
    if kind == "shop":
        files |= {"tpf/payments.ts": SHOP_PAYMENTS, SHOP_WEBHOOK_PATH: SHOP_WEBHOOK,
                  SHOP_PAY_NOTIFY_PATH: SHOP_PAY_NOTIFY, SHOP_PAY_RETURN_PATH: SHOP_PAY_RETURN,
                  SHOP_PAY_CHECK_PATH: SHOP_PAY_CHECK}
    if installable(kind):
        files |= {"public/sw.js": PWA_SW, "public/offline.html": PWA_OFFLINE, "tpf/Pwa.tsx": PWA_COMPONENT,
                  "app/manifest.ts": PWA_MANIFEST, "app/tpf-icon/[size]/route.tsx": PWA_ICON}
    return files


def form_endpoint(base_url: str, order_id: int) -> str:
    return f"{base_url.rstrip('/')}/forms/{order_id}"


def materialize(root: Path, files: list[dict[str, str]], form_endpoint: str = "", kind: str = "site") -> Path:
    """Write template + generated files into `root` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    for path, content in _for_code(platform_files(form_endpoint, kind), files).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    for f in files:
        picture = f.get("encoding") == "base64" and f["path"].startswith(GEN_DIR)   # drawn by the platform
        err = None if picture else validate_path(f["path"], kind)
        if err:
            raise ValueError(err)
        target = (root / f["path"]).resolve()
        if root.resolve() not in target.parents:
            raise ValueError(f"path escapes workspace: {f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if picture:
            target.write_bytes(base64.b64decode(f["content"]))
        else:
            target.write_text(f["content"], encoding="utf-8")
    return root


def all_files(files: list[dict[str, str]], form_endpoint: str = "", kind: str = "site") -> list[dict[str, str]]:
    """Template + generated files, as the deployer and git publisher need them."""
    return [{"path": p, "content": c} for p, c in _for_code(platform_files(form_endpoint, kind), files).items()] + list(files)


def kind_of(spec: dict | None) -> str:
    """What the order is: 'site' (static pages), 'app' (database and admin area), 'bot' (a Telegram bot
    with a database and admin area) or 'shop' (an application that sells and takes card payments)."""
    kind = (spec or {}).get("product_type")
    return kind if kind in ("app", "bot", "shop") else "site"


CAPABILITIES = {"uploads": UPLOADS_RULES, "chat": CHAT_RULES, "geolocation": GEO_RULES} | CAPS_RULES
ACCOUNT_CAPABILITIES = ("uploads", "chat", "geolocation")    # these need people who sign in; the rest any application can have


def capabilities_of(spec: dict | None) -> list[str]:
    """Which of the optional abilities an application's spec asks for, in a fixed order."""
    asked = (spec or {}).get("capabilities") or []
    if kind_of(spec) != "app":
        return []
    accounts = bool((spec or {}).get("user_roles"))
    return [c for c in CAPABILITIES if c in asked and (accounts or c not in ACCOUNT_CAPABILITIES)]


def capability_rules(spec: dict | None) -> str:
    return "".join(CAPABILITIES[c] for c in capabilities_of(spec))


def uses_accounts(arch: dict | None) -> bool:
    """True when the architecture gives the application people who sign in, each with a role."""
    return bool((arch or {}).get("roles"))


def access_of(route: dict) -> list[str]:
    """Who may open a route: ['public'], ['owner'], ['users'] (anyone signed in) or the role ids."""
    path = route.get("path", "")
    raw = [a.strip() for a in str(route.get("access") or "").split(",") if a.strip()]
    if path == "/admin/login" or path == "/login":
        return ["public"]
    if path == "/admin" or path.startswith("/admin/"):
        return ["owner"]
    return raw or ["public"]


def has_database(kind: str) -> bool:
    return kind in ("app", "bot", "shop")


def installable(kind: str) -> bool:
    """Applications and shops install on a phone's home screen; a bot lives in Telegram and a site is just pages."""
    return kind in ("app", "shop")
