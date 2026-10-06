"""Platform files and rules for what an application can do beyond its own database: documents and tables,
e-mail, scheduled work, the AI model and outside services. Like template_extras.py, split off to keep
template.py readable. The TypeScript here is tested on a real build (see tests/test_capabilities.py for the
Python side); change it only together with a build of the stand."""
from __future__ import annotations

APP_PLATFORM = r'''import "server-only";
import { createHmac } from "node:crypto";
import { FORM_ENDPOINT } from "@/tpf/config";

/** The key this application signs its requests to the platform with. Never send it to a browser. */
export function appKey(): string {
  return createHmac("sha256", process.env.SESSION_SECRET ?? "").update("tpf-app").digest("hex");
}

/** Ask the platform to do something for this application ('mail' or 'ai'). Never throws. */
export async function callPlatform(service: "mail" | "ai", body: unknown, timeoutMs: number): Promise<{ ok: boolean; status: number; data: Record<string, unknown> }> {
  if (!FORM_ENDPOINT || !process.env.SESSION_SECRET) return { ok: false, status: 0, data: { error: "off" } };
  try {
    const r = await fetch(FORM_ENDPOINT.replace(/\/forms\/(\d+)$/, `/apps/$1/${service}`), {
      method: "POST", headers: { "content-type": "application/json", "x-tpf-app": appKey() },
      body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs), cache: "no-store",
    });
    const data = (await r.json().catch(() => ({}))) as Record<string, unknown>;
    return { ok: r.ok && data.ok === true, status: r.status, data };
  } catch {
    return { ok: false, status: 0, data: { error: "failed" } };
  }
}
'''

APP_MAIL = r'''import "server-only";
import { callPlatform } from "@/tpf/platform";

export type MailResult = { ok: true } | { ok: false; error: "address" | "limit" | "off" | "failed" };

/** Send one e-mail from the application: plain text, one recipient. The letter comes from the application's
 *  name; a reply goes to the owner. Never throws. Limits: 200 letters a day for the application (30 on the
 *  review version) and 10 a day to one address — 'limit' when they are used up. */
export async function sendMail(mail: { to: string; subject: string; text: string }): Promise<MailResult> {
  const to = mail.to.trim(), subject = mail.subject.trim().slice(0, 200), text = mail.text.trim().slice(0, 10000);
  if (!/^[^@\s<>"',;]{1,64}@[^@\s<>"',;]{1,255}\.[A-Za-z]{2,24}$/.test(to) || !subject || !text) return { ok: false, error: "address" };
  if ((globalThis as { __tpfMuteNotify?: boolean }).__tpfMuteNotify) return { ok: true };   // the platform is checking the application
  const r = await callPlatform("mail", { to, subject, text }, 15000);
  if (r.ok) return { ok: true };
  const error = String(r.data.error ?? "failed");
  return { ok: false, error: error === "address" || error === "limit" || error === "off" ? error : "failed" };
}
'''

APP_AI = r'''import "server-only";
import { callPlatform } from "@/tpf/platform";

export type AiResult<T = unknown> =
  | { ok: true; text: string; data: T | null }
  | { ok: false; error: "too-large" | "limit" | "off" | "failed" };

const TYPES = ["image/jpeg", "image/png", "image/webp", "application/pdf"];

/** Ask the AI model. `files` are photos or PDF documents to read (up to 5, 3 MB together); with `json` — a JSON
 *  Schema of the answer — the model returns `data` in that shape, otherwise `text`. Takes several seconds:
 *  call it from a server action or a route handler, never while a page renders. Never throws.
 *  'limit' — the application has used its monthly allowance; 'too-large' — the files are too big or of another type. */
export async function askAI<T = unknown>(ask: {
  prompt: string; system?: string; files?: (File | Blob)[]; json?: Record<string, unknown>; maxTokens?: number;
}): Promise<AiResult<T>> {
  const files: { type: string; data: string }[] = [];
  let size = 0;
  for (const f of (ask.files ?? []).filter((x) => x && typeof x !== "string" && x.size > 0)) {
    size += f.size;
    if (files.length >= 5 || size > 3_000_000 || !TYPES.includes(f.type)) return { ok: false, error: "too-large" };
    files.push({ type: f.type, data: Buffer.from(await f.arrayBuffer()).toString("base64") });
  }
  if (!ask.prompt.trim()) return { ok: false, error: "failed" };
  const r = await callPlatform("ai", { prompt: ask.prompt, system: ask.system, files, json: ask.json, maxTokens: ask.maxTokens }, 55000);
  if (r.ok) return { ok: true, text: String(r.data.text ?? ""), data: (r.data.data ?? null) as T | null };
  const error = String(r.data.error ?? "failed");
  return { ok: false, error: error === "too-large" || error === "limit" || error === "off" ? error : "failed" };
}
'''

APP_TICK = r'''import { timingSafeEqual } from "node:crypto";
import { onTick } from "@/lib/schedule";
import { appKey } from "@/tpf/platform";

export const dynamic = "force-dynamic";
export const maxDuration = 60;

// The platform calls this about every 15 minutes, so that the application can do its scheduled work
// (lib/schedule.ts). Only a request signed with this application's key is let in.
export async function POST(request: Request) {
  const given = Buffer.from(request.headers.get("x-tpf-app") ?? ""), expected = Buffer.from(appKey());
  if (!process.env.SESSION_SECRET || given.length !== expected.length || !timingSafeEqual(given, expected)) {
    return new Response("forbidden", { status: 403 });
  }
  try {
    const done = await onTick();
    return Response.json({ ok: true, done: typeof done === "number" ? done : 0 });
  } catch (e) {
    console.error("scheduled work failed", e);
    return Response.json({ ok: false }, { status: 500 });
  }
}
'''

APP_SERVICES = r'''import "server-only";
import { sql } from "@/tpf/db";

/** A key of an outside service that the owner entered on the order page (see `services` in the architecture),
 *  or null while it is not entered. Read it at the moment of use; never send it to a browser or log it. */
export async function serviceKey(id: string): Promise<string | null> {
  if (!/^[a-z][a-z0-9_]{1,40}$/.test(id)) return null;
  try {
    const rows = await sql<{ value: string }[]>`select value from tpf_secrets where key = ${"svc_" + id}`;
    return rows[0]?.value || null;
  } catch {
    return null;      // the table appears with the first key
  }
}

export type ServiceReply = { ok: boolean; status: number; json: unknown; text: string };

/** One request to an outside service: HTTPS only, 15 seconds at most, an answer of up to 2 MB. Never throws:
 *  on a network error `status` is 0. Check `ok` and show the user a plain message when it is false. */
export async function callService(url: string, init: { method?: string; headers?: Record<string, string>; body?: string | URLSearchParams | FormData; json?: unknown } = {}): Promise<ServiceReply> {
  let target: URL;
  try { target = new URL(url); } catch { return { ok: false, status: 0, json: null, text: "" }; }
  if (target.protocol !== "https:" || /^(localhost|127\.|10\.|192\.168\.|169\.254\.|\[)/i.test(target.hostname)) return { ok: false, status: 0, json: null, text: "" };
  try {
    const headers: Record<string, string> = { ...(init.headers ?? {}) };
    let body = init.body;
    if (init.json !== undefined) { body = JSON.stringify(init.json); headers["content-type"] = "application/json"; }
    const r = await fetch(target, { method: init.method ?? (body === undefined ? "GET" : "POST"), headers, body, redirect: "error", cache: "no-store", signal: AbortSignal.timeout(15000) });
    const text = (await r.text()).slice(0, 2_000_000);
    let json: unknown = null;
    try { json = JSON.parse(text); } catch { /* not JSON */ }
    return { ok: r.ok, status: r.status, json, text };
  } catch {
    return { ok: false, status: 0, json: null, text: "" };
  }
}
'''

APP_PDF = r'''import "server-only";
import { PDFDocument, rgb, type PDFFont, type PDFPage } from "pdf-lib";
import fontkit from "@pdf-lib/fontkit";
import { BOLD, REGULAR } from "@/tpf/pdf-font";

/** A document to print or send: an estimate, an invoice, an act, a report. A4, portrait, Latin and Cyrillic text. */
export type PdfBlock =
  | { kind: "text"; text: string; bold?: boolean; size?: number; align?: "left" | "right" | "center" }
  | { kind: "pairs"; rows: [string, string][] }                         // label — value, one per line
  | { kind: "table"; columns: { title: string; width?: number; align?: "left" | "right" }[];   // width: a share, e.g. 3 and 1
      rows: string[][]; totals?: [string, string][] }                   // totals: lines under the table, right-aligned
  | { kind: "space"; height?: number }
  | { kind: "signatures"; left: string; right: string };                // two lines to sign, with a caption under each
export type PdfDoc = { title: string; subtitle?: string; blocks: PdfBlock[]; footer?: string };

const W = 595.28, H = 841.89, M = 42, BOTTOM = 56;
const INK = rgb(0.09, 0.1, 0.12), SOFT = rgb(0.42, 0.45, 0.5), LINE = rgb(0.82, 0.84, 0.87), BAND = rgb(0.95, 0.96, 0.97);

type Ctx = { pdf: PDFDocument; page: PDFPage; y: number; regular: PDFFont; bold: PDFFont; known: Set<number> };

// Characters the font does not have would be drawn as empty boxes: replace them with "?" once, here.
function clean(ctx: Ctx, text: unknown): string {
  let out = "";
  for (const ch of String(text ?? "").replace(/\r/g, "").replace(/\t/g, "  ")) {
    out += ch === "\n" || ctx.known.has(ch.codePointAt(0) ?? 0) ? ch : "?";
  }
  return out;
}

function wrap(font: PDFFont, text: string, size: number, width: number): string[] {
  const lines: string[] = [];
  for (const paragraph of text.split("\n")) {
    let line = "";
    for (const word of paragraph.split(" ")) {
      let piece = word;
      while (font.widthOfTextAtSize(piece, size) > width && piece.length > 1) {      // one word wider than the column
        let cut = piece.length - 1;
        while (cut > 1 && font.widthOfTextAtSize(piece.slice(0, cut), size) > width) cut--;
        if (line) { lines.push(line); line = ""; }
        lines.push(piece.slice(0, cut));
        piece = piece.slice(cut);
      }
      const next = line ? `${line} ${piece}` : piece;
      if (line && font.widthOfTextAtSize(next, size) > width) { lines.push(line); line = piece; } else line = next;
    }
    lines.push(line);
  }
  return lines;
}

function room(ctx: Ctx, height: number): void {
  if (ctx.y - height >= BOTTOM) return;
  ctx.page = ctx.pdf.addPage([W, H]);
  ctx.y = H - M;
}

function lines(ctx: Ctx, text: string, o: { size: number; bold?: boolean; x?: number; width?: number; align?: string; color?: ReturnType<typeof rgb>; gap?: number }): void {
  const font = o.bold ? ctx.bold : ctx.regular, x = o.x ?? M, width = o.width ?? W - 2 * M, step = o.size * (o.gap ?? 1.4);
  for (const line of wrap(font, clean(ctx, text), o.size, width)) {
    room(ctx, step);
    const w = font.widthOfTextAtSize(line, o.size);
    const at = o.align === "right" ? x + width - w : o.align === "center" ? x + (width - w) / 2 : x;
    ctx.page.drawText(line, { x: at, y: ctx.y - o.size, size: o.size, font, color: o.color ?? INK });
    ctx.y -= step;
  }
}

function table(ctx: Ctx, block: Extract<PdfBlock, { kind: "table" }>): void {
  const size = 9.5, pad = 5, full = W - 2 * M;
  const shares = block.columns.map((c) => (c.width && c.width > 0 ? c.width : 1));
  const total = shares.reduce((a, b) => a + b, 0);
  const widths = shares.map((s) => (full * s) / total);
  const row = (cells: string[], head: boolean) => {
    const font = head ? ctx.bold : ctx.regular;
    const wrapped = block.columns.map((_, i) => wrap(font, clean(ctx, cells[i] ?? ""), size, widths[i] - 2 * pad));
    const height = Math.max(...wrapped.map((w) => w.length)) * size * 1.35 + 2 * pad;
    if (ctx.y - height < BOTTOM) {
      ctx.page = ctx.pdf.addPage([W, H]);
      ctx.y = H - M;
      if (!head) row(block.columns.map((c) => c.title), true);          // the heading again on every page
    }
    if (head) ctx.page.drawRectangle({ x: M, y: ctx.y - height, width: full, height, color: BAND });
    let x = M;
    wrapped.forEach((cell, i) => {
      cell.forEach((line, n) => {
        const w = font.widthOfTextAtSize(line, size);
        const at = block.columns[i].align === "right" ? x + widths[i] - pad - w : x + pad;
        ctx.page.drawText(line, { x: at, y: ctx.y - pad - size - n * size * 1.35, size, font, color: INK });
      });
      x += widths[i];
    });
    ctx.y -= height;
    ctx.page.drawLine({ start: { x: M, y: ctx.y }, end: { x: M + full, y: ctx.y }, thickness: 0.6, color: LINE });
  };
  row(block.columns.map((c) => c.title), true);
  for (const cells of block.rows) row(cells.map(String), false);
  ctx.y -= 6;
  for (const [label, value] of block.totals ?? []) {
    room(ctx, 16);
    const v = clean(ctx, value), l = clean(ctx, label);
    const vw = ctx.bold.widthOfTextAtSize(v, 10.5);
    ctx.page.drawText(v, { x: W - M - vw, y: ctx.y - 10.5, size: 10.5, font: ctx.bold, color: INK });
    const lw = ctx.regular.widthOfTextAtSize(l, 10.5);
    ctx.page.drawText(l, { x: W - M - Math.max(vw, 90) - 14 - lw, y: ctx.y - 10.5, size: 10.5, font: ctx.regular, color: SOFT });
    ctx.y -= 16;
  }
}

/** Build the document; returns the bytes of a PDF file. Text outside Latin and Cyrillic is replaced with "?". */
export async function buildPdf(doc: PdfDoc): Promise<Uint8Array> {
  const pdf = await PDFDocument.create();
  pdf.registerFontkit(fontkit);
  const regular = await pdf.embedFont(Buffer.from(REGULAR, "base64"), { subset: true });
  const bold = await pdf.embedFont(Buffer.from(BOLD, "base64"), { subset: true });
  const ctx: Ctx = { pdf, page: pdf.addPage([W, H]), y: H - M, regular, bold, known: new Set(regular.getCharacterSet()) };
  pdf.setTitle(clean(ctx, doc.title).slice(0, 200));
  lines(ctx, doc.title, { size: 18, bold: true, gap: 1.25 });
  if (doc.subtitle) lines(ctx, doc.subtitle, { size: 10.5, color: SOFT });
  ctx.y -= 10;
  for (const block of doc.blocks) {
    if (block.kind === "text") lines(ctx, block.text, { size: block.size ?? 10.5, bold: block.bold, align: block.align });
    else if (block.kind === "space") ctx.y -= block.height ?? 12;
    else if (block.kind === "table") table(ctx, block);
    else if (block.kind === "pairs") {
      for (const [label, value] of block.rows) {
        const l = wrap(regular, clean(ctx, label), 10, 150), v = wrap(regular, clean(ctx, value), 10, W - 2 * M - 160);
        const height = Math.max(l.length, v.length) * 14;
        room(ctx, height);
        l.forEach((line, n) => ctx.page.drawText(line, { x: M, y: ctx.y - 10 - n * 14, size: 10, font: regular, color: SOFT }));
        v.forEach((line, n) => ctx.page.drawText(line, { x: M + 160, y: ctx.y - 10 - n * 14, size: 10, font: regular, color: INK }));
        ctx.y -= height;
      }
      ctx.y -= 6;
    } else {
      room(ctx, 70);
      ctx.y -= 34;
      const half = (W - 2 * M - 30) / 2;
      [block.left, block.right].forEach((caption, i) => {
        const x = M + i * (half + 30);
        ctx.page.drawLine({ start: { x, y: ctx.y }, end: { x: x + half, y: ctx.y }, thickness: 0.7, color: INK });
        ctx.page.drawText(clean(ctx, caption).slice(0, 60), { x, y: ctx.y - 13, size: 9, font: regular, color: SOFT });
      });
      ctx.y -= 30;
    }
  }
  const pages = pdf.getPages();
  pages.forEach((page, i) => {
    const mark = `${i + 1} / ${pages.length}`;
    page.drawText(mark, { x: W - M - regular.widthOfTextAtSize(mark, 8.5), y: 30, size: 8.5, font: regular, color: SOFT });
    if (doc.footer) page.drawText(wrap(regular, clean(ctx, doc.footer), 8.5, W - 2 * M - 60)[0] ?? "", { x: M, y: 30, size: 8.5, font: regular, color: SOFT });
  });
  return pdf.save();
}

/** The PDF as an answer of a route handler: opens in the browser, with a name to save under. */
export function pdfResponse(bytes: Uint8Array, filename: string): Response {
  const safe = filename.replace(/[^\w.\- ]+/g, "_").slice(0, 80) || "document.pdf";
  return new Response(Buffer.from(bytes), {
    headers: {
      "content-type": "application/pdf",
      "content-disposition": `inline; filename="${safe.endsWith(".pdf") ? safe : safe + ".pdf"}"; filename*=UTF-8''${encodeURIComponent(filename.slice(0, 80))}`,
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
'''

APP_SHEETS = r'''import "server-only";
import { deflateRawSync, inflateRawSync } from "node:zlib";

/** Tables in and out of the application: Excel files (.xlsx) and CSV. Server only. */
export type Cell = string | number | boolean | null | undefined;

// --- CSV ---

/** CSV that Excel opens correctly: UTF-8 with a mark, ';' between cells (what Excel expects in Europe). */
export function toCsv(rows: Cell[][], delimiter = ";"): string {
  const cell = (v: Cell) => {
    let s = v === null || v === undefined ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(s) && typeof v === "string") s = "'" + s;     // a cell must not run as a formula
    return /["\n\r]/.test(s) || s.includes(delimiter) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return "﻿" + rows.map((r) => r.map(cell).join(delimiter)).join("\r\n") + "\r\n";
}

/** Rows of a CSV file as text cells; the separator (',' ';' or tab) is detected. At most `maxRows` rows are read. */
export function parseCsv(text: string, maxRows = 5000): string[][] {
  const src = text.replace(/^﻿/, "");
  const first = src.split(/\r?\n/, 1)[0] ?? "";
  const delimiter = [";", "\t", ","].sort((a, b) => first.split(b).length - first.split(a).length)[0];
  const rows: string[][] = [];
  let row: string[] = [], cell = "", quoted = false;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (quoted) {
      if (ch === '"' && src[i + 1] === '"') { cell += '"'; i++; } else if (ch === '"') quoted = false; else cell += ch;
    } else if (ch === '"' && cell === "") quoted = true;
    else if (ch === delimiter) { row.push(cell); cell = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && src[i + 1] === "\n") i++;
      row.push(cell); cell = "";
      if (row.some((c) => c.trim() !== "")) rows.push(row);
      row = [];
      if (rows.length >= maxRows) return rows;
    } else cell += ch;
  }
  row.push(cell);
  if (row.some((c) => c.trim() !== "")) rows.push(row);
  return rows;
}

// --- XLSX: a zip of small XML files ---

const CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(b: Buffer): number {
  let c = 0xffffffff;
  for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function zip(files: [string, string][]): Buffer {
  const parts: Buffer[] = [], central: Buffer[] = [];
  let offset = 0;
  for (const [name, content] of files) {
    const raw = Buffer.from(content, "utf8"), data = deflateRawSync(raw), nameB = Buffer.from(name, "utf8"), crc = crc32(raw);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(0x0800, 6); local.writeUInt16LE(8, 8);
    local.writeUInt32LE(0x00210000, 10); local.writeUInt32LE(crc, 14); local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(raw.length, 22); local.writeUInt16LE(nameB.length, 26);
    const head = Buffer.alloc(46);
    head.writeUInt32LE(0x02014b50, 0); head.writeUInt16LE(20, 4); head.writeUInt16LE(20, 6); head.writeUInt16LE(0x0800, 8);
    head.writeUInt16LE(8, 10); head.writeUInt32LE(0x00210000, 12); head.writeUInt32LE(crc, 16); head.writeUInt32LE(data.length, 20);
    head.writeUInt32LE(raw.length, 24); head.writeUInt16LE(nameB.length, 28); head.writeUInt32LE(offset, 42);
    parts.push(local, nameB, data);
    central.push(head, nameB);
    offset += 30 + nameB.length + data.length;
  }
  const dir = Buffer.concat(central), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(files.length, 8); end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(dir.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, dir, end]);
}

const esc = (s: string) => s.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
function col(n: number): string {
  let s = "";
  for (let i = n + 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s;
  return s;
}

/** An Excel file. Each sheet is rows of cells; the first row is shown in bold as the heading. Numbers stay numbers. */
export function toXlsx(sheets: { name: string; rows: Cell[][] }[]): Uint8Array {
  const list = sheets.length ? sheets.slice(0, 20) : [{ name: "Sheet1", rows: [] }];
  const names = list.map((s, i) => (s.name || `Sheet${i + 1}`).replace(/[\\/?*[\]:]/g, " ").slice(0, 31) || `Sheet${i + 1}`);
  const sheetXml = (rows: Cell[][]) => {
    const widths: number[] = [];
    const body = rows.map((row, r) => `<row r="${r + 1}">` + row.map((v, c) => {
      const ref = `${col(c)}${r + 1}`, style = r === 0 ? ' s="1"' : "";
      const text = v === null || v === undefined ? "" : String(v);
      widths[c] = Math.min(60, Math.max(widths[c] ?? 8, text.length + 2));
      if (text === "") return "";
      if (typeof v === "number" && Number.isFinite(v)) return `<c r="${ref}"${style}><v>${v}</v></c>`;
      if (typeof v === "boolean") return `<c r="${ref}"${style} t="b"><v>${v ? 1 : 0}</v></c>`;
      return `<c r="${ref}"${style} t="inlineStr"><is><t xml:space="preserve">${esc(text.slice(0, 32000))}</t></is></c>`;
    }).join("") + "</row>").join("");
    const cols = widths.length ? "<cols>" + widths.map((w, i) => `<col min="${i + 1}" max="${i + 1}" width="${w ?? 8}" customWidth="1"/>`).join("") + "</cols>" : "";
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">${cols}<sheetData>${body}</sheetData></worksheet>`;
  };
  const files: [string, string][] = [
    ["[Content_Types].xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>${list.map((_, i) => `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join("")}</Types>`],
    ["_rels/.rels", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`],
    ["xl/workbook.xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>${names.map((n, i) => `<sheet name="${esc(n)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join("")}</sheets></workbook>`],
    ["xl/_rels/workbook.xml.rels", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${list.map((_, i) => `<Relationship Id="rId${i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`).join("")}<Relationship Id="rId${list.length + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`],
    ["xl/styles.xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs></styleSheet>`],
    ...list.map((s, i): [string, string] => [`xl/worksheets/sheet${i + 1}.xml`, sheetXml(s.rows.slice(0, 100000))]),
  ];
  return new Uint8Array(zip(files));
}

const unesc = (s: string) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'")
  .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n))).replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
  .replace(/&amp;/g, "&");

/** Rows of the first sheet of an Excel file as text cells (dates come as Excel's day numbers). Throws on a file
 *  that is not .xlsx. At most `maxRows` rows are read; the file may be up to 4 MB. */
export function parseXlsx(bytes: Uint8Array | ArrayBuffer, maxRows = 5000): string[][] {
  const b = Buffer.from(bytes as ArrayBuffer);
  if (b.length > 4_500_000) throw new Error("file too large");
  let end = -1;
  for (let i = b.length - 22; i >= Math.max(0, b.length - 66000); i--) if (b.readUInt32LE(i) === 0x06054b50) { end = i; break; }
  if (end < 0) throw new Error("not an xlsx file");
  const count = b.readUInt16LE(end + 10);
  let at = b.readUInt32LE(end + 16);
  const entries = new Map<string, Buffer>();
  let unpacked = 0;
  for (let n = 0; n < count && at + 46 <= b.length; n++) {
    if (b.readUInt32LE(at) !== 0x02014b50) break;
    const method = b.readUInt16LE(at + 10), size = b.readUInt32LE(at + 20), nameLen = b.readUInt16LE(at + 28);
    const extra = b.readUInt16LE(at + 30), comment = b.readUInt16LE(at + 32), local = b.readUInt32LE(at + 42);
    const name = b.toString("utf8", at + 46, at + 46 + nameLen);
    at += 46 + nameLen + extra + comment;
    if (!/^xl\/(workbook\.xml|sharedStrings\.xml|_rels\/workbook\.xml\.rels|worksheets\/sheet\d+\.xml)$/.test(name)) continue;
    const start = local + 30 + b.readUInt16LE(local + 26) + b.readUInt16LE(local + 28);
    const packed = b.subarray(start, start + size);
    const data = method === 0 ? packed : inflateRawSync(packed, { maxOutputLength: 40_000_000 });
    unpacked += data.length;
    if (unpacked > 60_000_000) throw new Error("file too large");
    entries.set(name, data);
  }
  const shared: string[] = [];
  const sst = entries.get("xl/sharedStrings.xml")?.toString("utf8") ?? "";
  for (const m of sst.matchAll(/<si>([\s\S]*?)<\/si>/g)) {
    shared.push(unesc([...m[1].replace(/<rPh[\s\S]*?<\/rPh>/g, "").matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map((t) => t[1]).join("")));
  }
  // the first sheet in the workbook's own order, not by file name
  let path = "xl/worksheets/sheet1.xml";
  const wb = entries.get("xl/workbook.xml")?.toString("utf8") ?? "", rels = entries.get("xl/_rels/workbook.xml.rels")?.toString("utf8") ?? "";
  const rid = /<sheet\b[^>]*\br:id="([^"]+)"/.exec(wb)?.[1];
  if (rid) {
    for (const m of rels.matchAll(/<Relationship\b[^>]*>/g)) {
      if (new RegExp(`\\bId="${rid}"`).test(m[0])) {
        const target = /\bTarget="([^"]+)"/.exec(m[0])?.[1];
        if (target) path = target.startsWith("/") ? target.slice(1) : "xl/" + target.replace(/^\.\//, "");
      }
    }
  }
  const sheet = entries.get(path)?.toString("utf8");
  if (sheet === undefined) throw new Error("not an xlsx file");
  const rows: string[][] = [];
  for (const r of sheet.matchAll(/<row\b[^>]*>([\s\S]*?)<\/row>/g)) {
    const row: string[] = [];
    for (const c of r[1].matchAll(/<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g)) {
      const ref = /\br="([A-Z]+)\d+"/.exec(c[1])?.[1];
      let index = row.length;
      if (ref) { index = 0; for (const ch of ref) index = index * 26 + ch.charCodeAt(0) - 64; index -= 1; }
      if (index > 500) continue;
      const type = /\bt="([^"]+)"/.exec(c[1])?.[1], inner = c[2] ?? "";
      const v = /<v>([\s\S]*?)<\/v>/.exec(inner)?.[1] ?? "";
      let text = type === "s" ? shared[Number(v)] ?? "" : type === "inlineStr"
        ? unesc([...inner.matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map((t) => t[1]).join("")) : type === "b" ? (v === "1" ? "TRUE" : "FALSE") : unesc(v);
      if (!type && /^-?\d+\.\d{10,}$/.test(text)) text = String(Number(Number(text).toPrecision(12)));   // 0.1+0.2 stored by Excel
      while (row.length < index) row.push("");
      row[index] = text;
    }
    if (row.some((x) => x.trim() !== "")) rows.push(row.map((x) => x ?? ""));
    if (rows.length >= maxRows) break;
  }
  return rows;
}

/** A file to download, as an answer of a route handler. `kind` picks the type by extension: xlsx or csv. */
export function fileResponse(content: Uint8Array | string, filename: string): Response {
  const csv = typeof content === "string";
  const safe = filename.replace(/[^\w.\- ]+/g, "_").slice(0, 80) || (csv ? "table.csv" : "table.xlsx");
  return new Response(csv ? content : Buffer.from(content), {
    headers: {
      "content-type": csv ? "text/csv; charset=utf-8" : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "content-disposition": `attachment; filename="${safe}"; filename*=UTF-8''${encodeURIComponent(filename.slice(0, 80))}`,
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
'''

APP_PDF_FONT = r'''// DejaVu Sans, Latin and Cyrillic only (Bitstream Vera licence: free to embed and redistribute).
export const REGULAR = "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BOcAewfbAHsEZgBxBOwAcQTsAHEE7ABxBOwAcQI5/8cCOQCQAjn/3gI5//QE5QBxBRIAugTlAHEE5QBxBOUAcQTlAHEE5QBxBrQA2QTlAEgFEgCuBRIArgUSAK4FEgCuBLwAPQUUALoEvAA9BXkAEATnAHsFeQAQBOcAewV5ABAE5wB7BZYAcwRmAHEFlgBzBGYAcQWWAHMEZgBxBZYAcwRmAHEGKQDJBRQAcQYzAAoFFABxBQ4AyQTsAHEFDgDJBOwAcQUOAMkE7ABxBQ4AyQTsAHEFDgDJBOwAcQYzAHMFFABxBjMAcwUUAHEGMwBzBRQAcQYzAHMFFABxBgQAyQUS/+UHVADJBY8AeAJc/+QCOf/TAlwAAwI5//ICXP/1Ajn/5AJcALACOQCWAlwAyQI5AMEEuADJBHIAwQJc/5YCOf/bBT8AyQSiALoEogC6BHUAyQI5AMEEdQDJAjkAiAR1AMkDAADBBHUAyQK8AMEEf//yAkYAAgX8AMkFEgC6BfwAyQUSALoF/ADJBRIAugaCAM0F/ADJBRIAugZMAHME5QBxBkwAcwTlAHEGTABzBOUAcQiPAHMILwBxBY8AyQNKALoFjwDJA0oAggWPAMkDSgC6BRQAhwQrAG8FFACHBCsAbwUUAIcEKwBvBRQAhwQrAG8E4//6AyMANwTj//oDIwA3BOP/+gMjADcF2wCyBRIArgXbALIFEgCuBdsAsgUSAK4F2wCyBRIArgXbALIFEgCuBdsAsgUSAK4H6QBEBosAVgTj//wEvAA9BOP//AV7AFwEMwBYBXsAXAQzAFgFewBcBDMAWALRAC8COf/bAosAsgQAAMEEAADBBAAAxwQAAO4EAAFMBAAAtgQAAPAAAP2iAAD8xQAA/h8AAP1qBQ4AyQUOAMkGSv/6BOEAyQWWAHMFFACHAlwAyQJcAAYCXP+WCMAAVAhcAMkGSv/6Ba4AyQX8AMkE4AAjBgQAyQV5ABAFfQDJBX0AyQThAMkGQABlBQ4AyQieACgFIQCHBfwAyQX8AMkFrgDJBgQAVAbnAMkGBADJBkwAcwYEAMkE0wDJBZYAcwTj//oE4AAjBuMAeQV7AD0GNgDJBXwArwiOAMkIwADJBqkAPAcPAMkFfQDJBZYAbwijANMFjwCIBOcAewTvAHAEtwC6BDQAugWIAGsE7ABxBzUARgRBAIUFMwC6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AAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3AAAAAAAJgAAAAAAAAAAAAD/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3AAAAAD/kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAA/9wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/kAAA/9P/yf9EAAAAAP63/2H/RAAAAAAAAAAAAAAAAAAAAAD/3P/cAAAAAAAAAAAAAAAA/0QAAAAA/5AAAAAA/2sAAAAA/7f/awAAAAD/kAAAAAAAAP9EAAAAAAAAAAD/RP+QAAD/t/+Q/0T/RAAAAAAAAAAAAAAAAP+QAAAAAP9r/7cAAP/c/9z/kAAAAAAAAP9E/9MAAP6IAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP+3AAAAAAAAAAD/mgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9P/0//JAAAAAP/cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/7f/wf+3AAD/twAAAAD/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/7f/wf+QAAD/KQAAAAD/3AAA/5AAAAAAAAAAAP+QAAAAAP9h/8kAAP+3AAD/twAA/9wAAAAA/5oAAAAAAAAAAAAA/5oAAAAAAAD/mgAAAAAAAP9r/30AAAAA/5D/3P+aAAD/mv+aAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/8H/wQAAAAD/3AAAAAAALwAAAAAAAAAAAAAAAP+3AAAAAP7m/5r/H/9EAAD+8AAAAAAAAAAA/9wAAAAAAAAAAAAA/9wAAAAAAAD/3AAAAAAAAP9EAAAAAAAAAAAAAP/cAAD/3P/cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/mH95gAAAAAAOf+t/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3AAA/33/kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9P/3P9EAAD/0/7BAAD/fQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/0wAA/6QAAAAA/7cAAAAA/9MAAP/c/7f/3P/cAAD/3AAAAAAAAAAA/9wAAAAAAAD/pP+3AAD/t//cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACYAJv63AAAAOQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9P/3P99AAD/rf+3/8H/rQAA/5oAAAAAAAAAAAAAAAAAAP9rAAD/kP+tAAD/fQAA/9MAAAAA/6QAAAAAAAAAAAAA/6QAAAAAAAD/pAAAAAAAAP+Q/5D/3AAA/5r/0/+kAAD/pP+kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/2v/ff/cAAAAAAAAAAAAJgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/RP8N/x//YQAA/4gAAAAAAAAAAAAAAAAAAP/cAAAAAAAAAAAAAAAA/q3+pAAA/qQAAAAA/8EAAAAA/qT+0/6tAAD+yQAA/q0AAP7B/0T/kAAA/4j/Ef7gAAD+9P7nAAAAAP6kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/0/74AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/iP74/1n/fQAAAAAAAAAAAAAAAP/cAAAAAAAAAAAAAAAAAAAAAAAA/2EAAAAA/2EAAAAA/9MAAAAA/2EAAAAAAAD/dQAAAAAAAP/J/07/kAAAAAD/Yf9hAAD/Yf91AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP7mAAD/rf8V/4j/kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/30AAAAA/4gAAAAA/9MAAAAA/4j/pAAAAAD/twAAAAAAAP/c/5D/3AAAAAD/ff+IAAD/iP+3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9wAAP74AAD/mgAAAAAAAAAA/2sAAAAAAAAAAP99AAAAAP/cAAAAAAAAAAAAAAAAAAAAAAAA/6QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/5AAAAAA/2sAAP+kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/2H/rf/TAAD/Df5h/vD/YQAA/5AAAAAAAAAAAP+QAAAAAAAAAAAAAAAAAAAAAAAA/uYAAAAA/vAAAAAA/7cAAAAA/vAAAAAAAAD/FQAAAAAAAAAA/x//awAA/5D+5v7wAAD+8P8VAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/5D/3P74AAD/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3P/cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/kP9r/7cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9wAAAAA/9wAAP/c/7f/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEEAAP8VAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9wAAAAA/7cAAAAAAAAAAAAA/7cAAAAAAAD/wQAAAAAAAP+3AAAAAAAAAAD/3P+3AAD/t//BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/2v/kP+kAAAAJv/cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/wQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/2v/t/99AAD/ff9E/9wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/0//c/9MAAP/cAAAAAP/c/9P/3AAAAAAAAAAAAAD/yQAA/7cAAAAAAAAAAP/TAAD/0wAAAAAAAP/TAAAAAAAAAEgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVv7JAAD/yf9h/5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3P7wAAAAAP9E/5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP8pAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3AAA/8EAAAAAAAAAAAAA/8EAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/BAAD/wQAAAAAAAP/cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3P7c/2sAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP7TAAAAAAAAAAAAAP/c/9z/3AAA/9z/3AAAAAAAAP+QAAD/kP/cAAD/awAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/c/9wAAP/cAAAAAACX/9wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/t/+3/9z/3AAAAAD/3P/cAAAAAP9EAAD/Tv+Q/5D/HwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/c/9wAAP/cAAAAAAAA/9wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/0//c/9wAOQAA/9wAAAAA/9wAAP/c/9wAAP9hAAD/ff+QAAD/YQAAAAD/3P/c/9z/twAAAAAAAAAA/9wAAAAA/9wAAP+I/60AAP91/7cAAAAA/9wAAP/cAAD/3AAAADkAAP/c/9wAAP/c/9wAAP/c/9wAAAAA/9z/3P9hAAAAAP+Q/63/Yf91/vj/AwAvAAD/0//c/9wAOQAA/9wAAAAA/9wAAP/c/9wAAP9hAAD/ff+QAAD/YQAAAAD/3P/c/9z/twAAAAAAAAAA/9wAAAAA/9wAAP+I/60AAP91/7cAAAAA/9wAAP/cAAD/3AAAADkAAP/c/9wAAP/c/9wAAP/c/9wAAAAA/9z/3P9hAAAAAP+Q/63/Yf91/vj/AwAvAAD/0//c/9wAOQAA/9wAAAAA/9wAAP/c/9wAAP9hAAD/ff+QAAD/YQAAAAD/3P/c/9z/twAAAAAAAAAA/9wAAAAA/9wAAP+I/60AAP91/7cAAAAA/9wAAP/cAAD/3AAAADkAAP/c/9wAAP/c/9wAAP/c/9wAAAAA/9z/3P9hAAAAAP+Q/63/Yf91/vj/AwAvAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/63/pP+QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJgAAAAAAAP9r/7cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9wAAP9EAAAAJgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/5D/kP+tAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/6T/kP+3AAD/0//c/9wAOQAA/9wAAAAA/9wAAP/c/9wAAP9hAAD/ff+QAAD/YQAAAAD/3P/c/9z/twAAAAAAAAAA/9wAAAAA/9wAAP+I/60AAP91/7cAAAAAAAAAAP/cAAD/3AAAADkAAAAA/9wAAP/c/9z/3P/cAAAAAAAA/9z/3P9hAAAAAP+Q/63/Yf91/vj/AgAvAAD/0//c/9wAOQAA/9wAAAAA/9wAAP/c/9wAAP9hAAD/ff+QAAD/YQAAAAD/3P/c/9z/twAAAAAAAAAA/9wAAAAA/9wAAP+I/60AAAAA/7cAAAAAAAAAAP/cAAD/3AAAADkAAAAA/9wAAP/c/9z/3P/cAAAAAAAA/9wAAP9hAAAAAP+Q/63/YQAA/vj/AgAvAAAAAAAAAAD/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/3AAAAAD/kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9z/3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9P/yf9EAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/PwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/ff9E/9wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/0//c/9MAAP/cAAAAAP/c/9P/3AAAAAAAAAAAAAD/yQAA/7cAAAAAAAAAAP/TAAD/0wAAAAAAAP/TAAAAAAAA/9wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVv7JAAD/RP8N/x//YQAA/4gAAAAAAAAAAAAAAAAAAP/cAAAAAAAAAAAAAAAA/q3+pAAA/qQAAAAA/8EAAAAA/qT+0/6tAAD+yQAA/q0AAP7B/0T/kAAA/4j+rf6kAAD+pP7JAAAAAP6kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/0/74AAAAAAAAAAD/0/+3AAAAAAAAAEsAcgA5AEsAAP9EAAD/iP+t/5r/DQAAAAAAAAAAAAAAAAAAAAAAAAAAACYAAAAAAAAAAP/JAAAAAP/cAAAAAAAAAAAAAAAAAAAAJgAAAAAAAAAAAAAAAAAAAAAAAAAAAEsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD++P/B/7f/wf/B/7f/wf+3/7cAAAAAAAAAAAAA/4gAAP/cAAAAAAAAAAD/twAAAAAAAP+Q/2v/kAAAAAAAAP+3/7cAAP+3AAAAAP59/7cAAAAA/7f/awAAAAAAAAAAAAAAAAAAAAAAAAAA/7cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJv+3/5D/t/+3/7cAL/+Q/5AAAP7mAAD+iP8D/7f+iAAAAAAAAAAAAAD/3AAAAAAAAP+3/7f/twAAAAAAAP8V/zwAAP+QAAAAAAAm/5AAAAAA/7f/twAAAAAAAAAAAAAAAAAAAAAAAAAA/7cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAXgAOACIAIwAkACUAJwAoACkAKwAsAC0AMAAxADIAMwA0ADUANgA3ADgAOQA6ADsARgBHAEwATwBQAFMAVwBYAFkAWgBrAHsAgACBAIIAgwCEAIYAhwCQAJIAkwCUAJUAlgCZAJoAmwCcAJ0AngCfAKgAqQCqAKsAsACxALIAswC0ALUAtgC9AL8AwADCAMQAxgDMAM4A0ADbAN4A+QD9AQABCAEUARUBGAEZAR4BIAEkAS4BOAE9Aa8BuQG7AAIATgAiACIAAQAjACMAAgAkACQAAwAlACUABAAnACcABQAoACgABgApACkABwArACsACAAsACwACQAtAC0ACgAwADAACwAxADEADAAyADIADQAzADMADgA0ADQADwA1ADUAEAA2ADYAEQA3ADcAEgA4ADgAEwA5ADkAFAA6ADoAFQA7ADsAFgBGAEYAFwBHAEcAGABMAEwAGQBPAE8AGgBQAFAAGwBTAFMAHABXAFcAHQBYAFgAHgBZAFkAHwBaAFoAIABrAGsAIQB7AHsAIgCAAIAAIwCBAIIAJACDAIMAJQCEAIQAJACGAIYAJgCHAIcAJwCQAJAABACSAJYACwCZAJwAEQCdAJ0AFQCeAJ4AKACfAJ8AKQCoAKsAFwCwALAAKgCxALEAGgCyALYAGwC9AL0AIAC/AL8AIADAAMAAKwDCAMIAKwDEAMQALADGAMYAJwDMAMwAJwDOAM4ABADQANAALQDbANsAFwDeAN4ABgD5APkACgD9AP0ACgEAAQAALgEIAQgAGgEUARQADgEVARUAHAEYARgADgEZARkALwEeAR4ADwEgASAADwEkASQAMAEuAS4AEQE4ATgAFQE9AT0AFgGvAa8AMQG5AbkAMgG7AbsAMwACAIwADgAOAAEADwAPAAIAGwAbAAMAIgAiAAQAIwAjAAUAJAAkAAYAJQAlAAcAJwAnAAgAKAAoAAkAKQApAAgAKwArAAoALAAtAAgAMAAwAAsAMQAxAAgAMgAyAAwAMwAzAAgANAA0AA0ANQA1AA4ANgA2AA8ANwA3ABAAOAA4ABEAOQA5ABIAOgA6ABMAOwA7ABQAQgBCABUARABEABYARQBFABcARgBGABgARwBHABkASABJABoASgBKABsATQBNABwATgBOABoATwBPAB0AUABQAB4AUgBSABcAUwBTAB8AVABUACAAVQBVACEAVgBWACIAVwBXACMAWABYACQAWQBZACUAWgBaACYAawBrACcAewB7ACgAgACEAAQAhgCGACkAhwCHACoAkgCWAAsAmQCcAA8AnQCdABMAngCfAAgAoACgACsAoQChABUAogClACsApwCnABYAqACoACwAqQCpABgAqgCrACwAsACwAC0AsQCxAB0AsgCyAC4AswCzAB4AtAC2AC4AuQC5AC8AugC6ACIAuwC8AC8AvQC9ACYAvwC/ACYAwADAADAAwQDBADEAwgDCADAAwwDDADEAxADEADAAxQDFADEAxgDGAAYAxwDHADIAyADIADMAyQDJADQAygDKADMAywDLADUAzADMAAYAzQDNADIAzgDOAAcAzwDPADYA0QDRADcA0wDTADgA1QDVADgA1wDXADgA2QDZADgA2wDbABgA3ADcADMA3gDeADkA3wDfABoA4ADgADMA4gDiADoA7wDvADsA8wDzADsA+QD5AAgA/QD9AAgBCAEIAB0BDAEMADMBDQENADwBDgEOADMBDwEPADwBEAEQAD0BEQERADwBFAEUAAgBFQEVAB8BFwEXADsBGAEYAAgBGQEZAB8BGgEaAA0BHAEcAA0BHgEeAA0BHwEfACABIAEgAA0BIQEhACABIgEiAD4BIwEjADUBJAEkAA4BJQElACEBJgEmAD8BKQEpAEABKwErAEABLQEtAEABLgEuAA8BLwEvACIBMQExAEABMwEzAEABNAE0AEEBNQE1AEIBNgE2AEMBNwE3AEQBOAE4ABMBPQE9ABQBuQG5AEUBugG6AEYBuwG7AEcAAQAAAAoAkgCUABRERkxUAHphcmFiAIRhcm1uAIRicmFpAIRjYW5zAIRjaGVyAIRjeXJsAIRnZW9yAIRncmVrAIRoYW5pAIRoZWJyAIRrYW5hAIRsYW8gAIRsYXRuAIRtYXRoAIRua28gAIRvZ2FtAIRydW5yAIR0Zm5nAIR0aGFpAIQABAAAAAD//wAAAAAAAAAAAAAAAAABAAAACgDgAOgAUAA8DAAH3QAAAAACggAABGAAAAXVAAAAAAAABGAAAAAAAAAAAAAAAAAAAARgAAAAAAAAAWgAAARgAAAAVQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABDgAAAnYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFoAAAEOAAAAWgAAAFoAAAEOAAAAAAAAAAAAAAEOAAAAWgAAAFoAAAEOAAAAWgAAAFoAAABaAAABcgAAAFoAAABaAAACOAAA+48AAAA8AAAAAAAAAAAAKAAeADAABwADADoAYgCKALIA2gEWATQBcAGOAawAAQAHAAkACgA8AD4AXABdAF4AAQADAB4AQABvAAQAAAAAAAAAAwHKAAAAKAl1AAAByQAoACgJjQABAcgAKAAACZYAAAAEAAAAAAAAAAMBzQAAACgJdQAAAcwAKAAoCY0AAQHLACgAAAmWAAAABAAAAAAAAAADAdAAAAAoCXUAAAHPACgAKAmNAAEBzgAoAAAJcQAAAAQAAAAAAAAAAwHTAAAAKAlmAAAB0gAoACgJfgABAdEAKAAACXEAAAAEAAAAAAAAAAUB1gAAACgJcgAAAdcAKAAoCZgAAQHVACgAKAmKAAAB1wAoACgJmAABAdQAKAAACYMAAAAEAAAAAAAAAAIAXQAAACgIAAAAAF0AKAAACAAAAQAEAAAAAAAAAAUB2gAAACgJcgAAAdcAKAAoCZgAAQHZACgAKAmKAAAB1wAoACgJmAABAdgAKAAACYMAAAAEAAAAAAAAAAIAHgAAACgFAgAAAB4AKAAABQIAAQAEAAAAAAAAAAIAQAAAACgEKAAAAEAAKAAABCgAAQAEAAAAAAAAAAIAbwAAACgCVgAAAG8AKAAAAlYAAQAA";
export const BOLD = "AAEAAAAOAIAAAwBgR0RFRgORBFcAAGyMAAAAIkdQT1OBV41iAABssAAAFCZHU1VCJ6Q/wwAAgNgAAACWT1MvMm4KE4QAAGlcAAAAVmNtYXDZvNJrAABptAAAAKxnYXNwAAcABwAAbIAAAAAMZ2x5ZpTQpDIAAADsAABdFGhlYWQoakw8AABhyAAAADZoaGVhDq8JOgAAaTgAAAAkaG10eKH8u1gAAGIAAAAHNmxvY2HkoM1WAABeIAAAA6ZtYXhwAhIDywAAXgAAAAAgbmFtZSwMQXIAAGpgAAAB/nBvc3T/2wBaAABsYAAAACAAAgEfAAAChwXVAAUACQAAASERAyEDESERIQEfAWgz/v4zAWj+mAXV/cP+XgGi/cz+nAACAMMDqgNoBdUAAwAHAAABESMRIxEjEQNo7cvtBdX91QIr/dUCKwAAAgCLAAAGKQW+ABsAHwAAAQMhEzMDIRUhAyEVIQMjEyEDIxMhNSETITUhEwEhAyEDj2ABCGHdYQEV/rZFARz+sGDdYP74YN9g/ukBSEb+5QFSYAFQ/vhGAQgFvv5/AYH+f9X+7tf+gQF//oEBf9cBEtUBgf2q/u4AAwCg/tMFBgYUACMAKgAxAAABIwMuAScRHgEXEScuATU0Nj8BMxUeARcVLgEnERceARUUBgcDEQ4BFRQWExE+ATU0JgMbogF96m9z63kh78n14wGiZMhlZMhlIP7N9PeiR1VO8FdXUP7TAS0FLikBBjs/BAE3Biq0qbPJCefjCCIb/iovBf7hBii7t7jFDgNCAQUERTU7Q/6x/uoBQkJEQwAABQBC/+MHwwXwAAsAFwAbACcAMwAAASIGFRQWMzI2NTQmJzIWFRQGIyImNTQ2ASMBMyEyFhUUBiMiJjU0NhciBhUUFjMyNjU0JgYzR05NSEhMTUe61ta6utfX/SXdA6Xe+4261dW6utXVukhOTkhITU4CaHtyc3t7c3J7qNi9vdvbvbzZ/NMGDdm9vdravb3ZqHxyc319c3J8AAIAe//jBqQF8AAmADAAAAkBPgE3IQYCBwEhJw4BIyAANTQ2Ny4BNTQ2MzIWFxEuASMiBhUUFgMOARUUFjMyNjcDHwGZNTcFATcPb2MBJf5YYmnogv75/ruPoioo/tNbxWteqFBNVTGXQUKqd0N0MgPf/j5Grm62/uRr/r5tRkQBFduS4Wo1ajqjxB0d/uowLjs2Ilf+0y93R3OiKSkAAQDDA6oBsAXVAAMAAAERIxEBsO0F1f3VAisAAAEAsP7yAwQGEgANAAABISYCNTQSNyEGAhUUEgME/teZkpOYASmAgH/+8vcBvdvbAcH17f473d3+OgABAKT+8gL4BhIADQAAEzYSNTQCJyEWEhUUAgekgICAgAEpmJOSmf7y7gHG3d0Bxe31/j/b2/5D9wAAAQApAjkEBgXwABEAAAENAQclESMRBSctATcFETMRJQQG/rYBSkz+s6r+skwBTv6yTAFOqgFNBMGtro24/qgBWLiNrq2NtgFY/qi2AAEA2QAABdsFBAALAAABESEVIREjESE1IRED0QIK/fbu/fYCCgUE/fTs/fQCDOwCDAAAAQBt/t0COQGDAAUAABMhEQMjE9EBaPfVZAGD/s/+iwF1AAEAbwG8AuMC3wADAAATIREhbwJ0/YwC3/7dAAABANEAAAI5AYMAAwAAEyERIdEBaP6YAYP+fQAAAQAA/0IC7AXVAAMAAAEzASMCDt798d0F1fltAAIAYv/jBS8F8AALABcAAAEQJiMiBhEQFjMyNgEQACEgABEQACEgAAOuaXx8amp8e2oBgf7A/tr+2f7AAUABJwEmAUAC7AEY5eX+6P7l6OgBGP6N/m0BkwFzAXQBk/5tAAEA5wAABQQF1QAKAAATIREFESUhESERIfABVP6jAVsBbgFU++wBCgPFSAEGSPs1/vYAAQCiAAAE3wXwABgAAAEhESERAT4BNTQmIyIGBxE+ATMgBBUUBgcCTgKR+8MCIUlGjXVa1nqC/noBDAEpfsoBG/7lARsB4UJ+RGmATUwBSCst7NN607EAAAEAif/jBO4F8AAoAAABHgEVFAQhIiYnER4BMzI2NTQmKwE1MzI2NTQmIyIGBxE+ATMgBBUUBgO6l53+rP66c+dxbNVnmaOno5qikY6Kfl2+XnLgbAEjASGKAyUnwZXe5yUlASk2N2pjZmn4W11WXiopARogIL/Ag6cAAgBcAAAFMwXVAAIADQAACQEhAyERMxEjESERIREC8v5aAaZAAazV1f6U/WoEmP2PA678Uv7p/vABEAFKAAABAJ7/4wUCBdUAHQAAEyERIRU+ATMgABUUACEiJicRHgEzMjY1NCYjIgYH2QO9/XYsWTABEQEw/rX+2n/5e3rbYYyhoYxTvGwF1f7l5wwN/u/08v7uMTIBL0ZGiXV2iCstAAIAf//jBSMF7gALACQAAAEiBhUUFjMyNjU0JgERLgEjIgYHPgEzMgAVFAAhIAAREAAhMhYC5WVlZWVmZWUBdl+oUKzAEEKaW+UBGf7G/vj+3f7BAXUBRWfCAuGDg4ODg4ODgwLN/uwtK7+8MTH+9Nnw/t8BiQFpAXIBpyAAAAEAiQAABO4F1QAGAAATIRUBIQEhiQRl/br+iQIn/TEF1dn7BAS6AAADAH3/4wUSBfAACwAjAC8AAAEiBhUUFjMyNjU0JiUuATU0JCEgBBUUBgceARUUBCEgJDU0NhMUFjMyNjU0JiMiBgLJbHR0bGtycv58iIoBGgERAQ8BGouImJv+2f7e/t3+15vyY1xaYmJaXGMCnHZubnV1bm91fymqf73Gxb5/qikqvZDe4+PekL0BVVlgYFlZX2AAAgBq/+MFDgXuABgAJAAANxEeATMyNjcOASMiADU0ACEgABEQACEiJgEyNjU0JiMiBhUUFs1cqFKswBFEmlrl/ucBOQEHASQBQP6K/rppwAF/ZWZmZWVmZiEBFCsrv7wyMgEL2vEBIv52/pj+jv5ZHwLug4OChISCg4MAAAIA5QAAAk4EYAADAAcAABMhESERIREh5QFp/pcBaf6XBGD+ff6m/n0AAAIAgf7dAk4EYAAFAAkAABMhEQMjExEhESHlAWn41WQBaf6XAYP+z/6LAXUEDv59AAEA2QA9BdsExwAGAAAJAhUBNQEF2/w8A8T6/gUCA83+tP62+gHP7AHPAAIA2QEnBdsD2wADAAcAABMhFSEVIRUh2QUC+v4FAvr+A9vr3O0AAQDZAD0F2wTHAAYAABM1ARUBNQHZBQL6/gPFA836/jHs/jH6AUoAAAIAjQAABB8F8AAdACEAAAEhNTQ2PwE+ATU0JiMiBgcRPgEzMgQVFAYPAQ4BFQUhESECxf6XQmpAOTVgVlG8ZnnIXfQBAE5eQEQq/pcBaf6XAfgxUn9iOjRcLkZPQ0IBOioox79im1k5Pkstwf6cAAACAIf+nAdvBaAACwBNAAABFBYzMjY1NCYjIgYBDgEjIiY1NDYzMhYXNTMRPgE1NCYnJiQjIgYHBgIVFBIXFgQzMjY3FwYEIyIkJyYCNTQSNzYkMzIEFx4BFRAAISMDP2laWWprWlhpAZoehVms19irWYUe0XyOOjtf/uOmdNRalKVrZWQBA5N+/Flrff7ZmLn+uICAhoh+fgFPtOABbntLTf66/tcnAht7jo96eY2N/lpHT/nIyPpQR4P9SxPJnWSvSXqEPTti/sm1lf77ZGJnXlCiYWeDfX0BSb22AUp9fIiroWLlfv7x/tQAAAIACgAABicF1QAHAAoAAAEhAyEBIQEhASEDBEb9pl/+fQIpAcsCKf59/agBmcwBEP7wBdX6KwIlAlIAAAMAvAAABYkF1QAIABEAIAAAATI2NTQmKwEREzI2NTQmKwERAR4BFRQEKQERISAEFRQGAxJbXl5b1eJ0dXR14gJIfIj+3P7W/YECQgE3ARdmA5NQTk1R/sT9c2JjYWH+eQIZJMKN2NQF1bzPbZkAAQBm/+MFXAXwABkAACUOASMgABEQACEyFhcRLgEjIgIVFBIzMjY3BVxq5n3+i/5MAbQBdX3mamvQc87s7M5z0GtSNzgBoQFlAWYBoTg3/stJRP746Of++ERJAAACALwAAAY5BdUACAAXAAABETMyNjU0JiMBISAEFxYSFRQCBwYEKQECPYrs+fjt/fUBlgFUAU13aWZmaXj+sP6w/moEsvxx6t/e6AEjYXRl/vinqf73ZXRhAAABALwAAAThBdUACwAAEyERIREhESERIREhvAQP/XICZ/2ZAqT72wXV/t3+6v7d/qr+3QAAAQC8AAAEywXVAAkAABMhESERIREhESG8BA/9cgJn/Zn+fwXV/t3+6v7d/YcAAAEAZv/jBfoF8AAdAAAlBgQjIAAREAAhMgQXES4BIyICFRQSMzI2NxEjESEF+pD+yqX+i/5MAbwBgpUBEXl993zm+fDdPGcp6wJYb0ZGAaEBZQFpAZ44N/7LR0b+/+/t/v4PEAEiAQIAAQC8AAAF9gXVAAsAABMhESERIREhESERIbwBgQI4AYH+f/3I/n8F1f3HAjn6KwJ5/YcAAAEAvAAAAj0F1QADAAATIREhvAGB/n8F1forAAAB/43+ZgI9BdUACwAAEyEREAAhIxEzMjY1vAGB/tH+zU48eHsF1fq8/un+7AEjhoIAAAEAvAAABnEF1QAKAAATIREBIQkBIQERIbwBgQIrAb/9MQMZ/h79rv5/BdX93wIh/T387gJM/bQAAQC8AAAE4QXVAAUAABMhESERIbwBgQKk+9sF1ftO/t0AAAEAvAAABzkF1QAMAAATIQkBIREhEQEjAREhvAHqAVQBVgHp/pT+qPT+qP6TBdX84QMf+isERPzbAyX7vAAAAQC8AAAF9gXVAAkAABMhAREhESEBESG8Aa4CHwFt/lL94f6TBdX8AAQA+isEAPwAAAACAGb/4wZmBfAACwAXAAABIgIVFBIzMhI1NAIDIAAREAAhIAAREAADZrDCwrCxwsKxAWgBmP5o/pj+mf5nAZkE2f787Ov+/AEE6+wBBAEX/mT+lf6W/mQBnAFqAWsBnAACALwAAAWJBdUACgATAAATISAEFRQEISMRIQERMzI2NTQmI7wCfwEdATH+z/7j/v5/AYHVcHp6cAXV/err/f36BL7+X21kZGwAAAIAZv7VBmYF8AAPABsAAAUjIAAREAAhIAARFAIHASEBIgIVFBYzMhI1NAIDjx7+j/5mAZkBZwFrAZXXygEt/pH+47DCvrSxwsIbAZgBbAFrAZz+aP6R/P6UXP6wBgT+/Ozw/wEE6+wBBAACALwAAAYABdUACAAcAAABMjY1NCYrARkCIREhIAQVFAYHHgEXEyEDLgEjAt95aWl5ov5/AkwBJwETj5BPfUDR/ma2N3FeAz9aZ2ZY/oH+9v3LBdXG1pS+LRJ/gf5YAXNwUgAAAQCT/+MFLQXwACcAAAERLgEjIgYVFBYfAR4BFRQEISIkJxEWBDMyNjU0Ji8BLgE1NCQhMgQEy3vqaIqEWXWk+dL+2/7Tjv7ij48BC3x+hluIleDPASABDnsBBAWm/sQ3OExQPEMYITLMvPfxNjUBRUxNVE5GTB4hMNKy3/AlAAABAAoAAAVqBdUABwAAEyERIREhESEKBWD+Ef5//hAF1f7d+04EsgAAAQC8/+MFwwXVABEAABMhERQWMzI2NREhERAAISAAEbwBgXmJinkBgf7C/rr+u/7CBdX8gbmfn7kDf/yB/sP+ygE2AT0AAAEACgAABicF1QAGAAATIQkBIQEhCgGDAYwBiwGD/df+NQXV+7IETvorAAEAPQAACJMF1QAMAAATIQkBIQkBIQEhCQEhPQFxAQIBAAFzAQABAgFu/qD+RP7x/vT+RAXV+8MEPfvDBD36KwRv+5EAAQAnAAAGAgXVAAsAAAkBIQkBIQkBIQkBIQP8Agb+b/6j/qb+bQIG/g4BkgFHAUYBlAL6/QYB/v4CAvoC2/4fAeEAAf/sAAAF3wXVAAgAAAMhCQEhAREhERQBpQFUAVQBpv3H/n8F1f3sAhT8oP2LAnUAAQBcAAAFcQXVAAkAABMhFQEhESE1ASFzBOf83wM4+usDIfz2BdXp/Df+3ekDyQAAAQCw/vIDHQYUAAcAABMhFSERIRUhsAJt/ucBGf2TBhTh+qDhAAABAAD/QgLsBdUAAwAABQEzAQIO/fLdAg++BpP5bQABAIv+8gL4BhQABwAAASE1IREhNSEC+P2TARn+5wJt/vLhBWDhAAEAzwOoBeUF1QAGAAAJASMJASMBA9UCEPH+Zv5n8gIQBdX90wEt/tMCLQAAAQAA/h0EAP7bAAMAAAEVITUEAPwA/tu+vgABAF4E7gKTBmYAAwAACQEjAQF5ARrE/o8GZv6IAXgAAAIAWP/jBMUEewAKACUAAAEiBhUUFjMyNj0BJREhNQ4BIyImNTQkITM1NCYjIgYHET4BMyAEAqJwcVtRZYoBaf6XSLSBrtkBDwEi04aOc8ZVc+h0AS8BDQH4TEpETZFtKYf9gaZmXcuixbgcVU8uLgERHB3vAAACAKz/4wVeBhQACwAcAAAlMjY1NCYjIgYVFBYDPgEzMgAREAAjIiYnFSERIQMAc3l5c3N7e3tKtHXPAQr+9s91tEr+mgFm56igoKipn5+pAtViXf63/v3+/f63XWKiBhQAAAEAWP/jBDUEewAZAAABES4BIyIGFRQWMzI2NxEOASMgABEQACEyFgQ1SZNPlqenllSXQFStV/7R/qoBVgEvWKsEPf7cMjCvnZ2vMjH+2x8fATcBFQEVATcfAAIAXP/jBQ4GFAAQABwAAAERIREhNQ4BIyIAERAAMzIWAzI2NTQmIyIGFRQWA6YBaP6YSrJ1z/72AQrPdLOic3l5c3J5eQO8Alj57KJjXAFJAQMBAwFJXfzJqKCgqKigoKgAAgBY/+MFCgR7ABQAGwAAARUhHgEzMjY3EQ4BIyAAERAAISAABTQmIyIGBwUK/LsNnIxx7X1//n/+0P6vAUsBIgEIAT3+kHdgaIIQAjNmfn5DRP7sMDEBNQEXARIBOv7Ck2Z9dW4AAQAnAAADjQYUABMAAAEVIyIGHQEhESERIREjETM1NDYzA43GTDwBMv7O/pqysszWBhTrN0RO/wD8oANgAQBOt68AAgBc/kYFDgR5ABwAKAAAJQ4BIyIANTQAMzIWFzUhERAAISImJxEeATMyNjUDIgYVFBYzMjY1NCYDpkqydc3+9AEMzXWySgFo/qv+vGnEY160W7Ck7G98eHNwfHy+YlwBQ/r7AUFcY6b8Ef7y/uMgIQEXNjWapAMGpJaan6SVlqQAAAEArAAABRIGFAAXAAABESE1ETQmJy4BIyIGFREhESERPgEzMhYFEv6YDRAVSC5wgP6aAWZRtm7CyQKq/VZvAZmTbhojJ62Z/dkGFP2oYl3uAAACAKwAAAISBhQAAwAHAAATIREhESERIawBZv6aAWb+mgRg+6AGFP7cAAAC/7z+RgISBhQACwAPAAATIREUBisBNTMyNjURIREhrAFm2M2xPmZMAWb+mgRg+7Th7etchwYA/twAAQCsAAAFeQYUAAoAABMhEQEhCQEhAREhrAFmAZwBoP3dAk7+Tv5L/poGFPyxAZv9/v2iAdP+LQABAKwAAAISBhQAAwAAEyERIawBZv6aBhT57AAAAQCqAAAHtAR7ACUAAAE+ATMyFhURIRE+ATU0JiMiBgcRIRE0JiMiBhURIREhFT4BMzIWBLpEu3DByv6YAQFGTmZvAv6YQFJncP6YAWhCq2d0sgOmaG3u4/1WAkgNHBp3a6if/doCSLprqZ392QRgpF9gcAABAKwAAAUSBHsAFwAAAREhNRE0JicuASMiBhURIREhFT4BMzIWBRL+mA0QFUgucID+mgFmUbZuwskCqv1WbwGbkW4aIyetmf3ZBGCkYl3uAAIAWP/jBScEewALABcAAAEiBhUUFjMyNjU0JgMgABEQACEgABEQAALBd319d3V8fHUBIQFF/rv+3/7e/rkBRwN7q6Ghq6uhoasBAP7I/uz+7P7IATgBFAEUATgAAgCs/lYFXgR7ABAAHAAAJREhESEVPgEzMgAREAAjIiYTIgYVFBYzMjY1NCYCEv6aAWZKtHXPAQr+9s91tKRze3tzc3l5ov20BgqkYl3+t/79/v3+t10DN6mfn6mooKCoAAACAFz+VgUOBHkACwAcAAABIgYVFBYzMjY1NCYTDgEjIgAREAAzMhYXNSERIQK6cnl5cnN5eXlKsnXP/vYBCs91skoBaP6YA3eooKCoqKCgqP0rY1wBSQEDAQMBR1xjpvn2AAEArAAAA+wEewARAAABLgEjIgYVESERIRU+ATMyFhcD7C9dL4qV/poBZkWzfRIqKAMvFhWxpf38BGC4bmUDBQABAGr/4wRiBHsAJwAAAREuASMiBhUUFh8BBBYVFAQhIiYnER4BMzI2NTQmLwEuATU0NjMyFgQXc9ZfZmNLYT8BE77++P76b+19a+F0aWpJbT/vwPT8Y9oEPf7wMDAzNSsuCwkjoKuztCMjARA0NDo5MC8NCB6ipbKsHgAAAQAbAAADpAWeABMAAAERIREhERQWOwERISImNREjETMRAjMBcf6PPly4/s3UsbKyBZ7+wv8A/iVON/8AsdQB2wEAAT4AAAEAoP/jBQYEYAAZAAATESEVFAIVFBYXHgEzMjY1ESERITUOASMiJqABaAIOERZHLnCAAWb+mlG1bcLLAbQCrHBb/u0uh3cbIyasmQIp+6CiYl3uAAEAHwAABRkEYAAGAAATIQkBIQEhHwFmARcBFgFn/kf+dwRg/PoDBvugAAEASAAABx0EYAAMAAATIRsBIRsBIQEhCwEhSAFcvL0BK7y9AVz+2f55vbz+eQRg/PwDBP0EAvz7oAMC/P4AAQAfAAAFCgRgAAsAAAkBIRsBIQkBIQsBIQHH/mwBe+XoAXv+bAGo/oX8+f6FAj0CI/60AUz93/3BAWL+ngABABn+RgUSBGAADwAAEyEJASEBDgErATUzMjY/ARkBZgEtAQABZv4pR72bz3BbUxcKBGD9CAL4+za7les6Sx8AAQBcAAAERgRgAAkAABMhFQEhESE1ASF1A9H9sgJO/BYCTv3LBGD6/Zr/APoCZgAAAQEA/rIEsgYUACQAAAUVIyImPQE0JisBNTMyNj0BNDY7ARUjIgYdARQGBx4BHQEUFjMEstnayGyOPT2ObMja2UWNVVpub1lVjW3hsMHAlnXfdJbNwa/hV46mnY4ZG46cpo9XAAEBBP4dAecGHQADAAABESMRAefjBh34AAgAAAABAQD+sgSyBhQAJAAABTMyNj0BNDY3LgE9ATQmKwE1MzIWHQEUFjsBFSMiBh0BFAYrAQEARoxVWm9vWlWMRtnayGyOPT2ObMja2W1Xj6acjhsZjp2mjlfhr8HNlnTfdZbAwbAAAQDZAbIF2wNSAB0AAAEVDgEjIicmJyYnJiMiBgc1PgEzMhcWFxYXFjMyNgXbarNga48OCAcPm15YrGJrsmBrjw8HBw+bXlapA1L0UEU6BgMDBj1NU/RQRToGAwMGPUsAAgEf/osChwRgAAUACQAAARETIRMRAREhEQEfMwECM/6YAWj+iwI9AaL+Xv3DBHEBZP6cAAIArv7HBIkFmAAGACMAAAEOARUUFhcBES4BKwERPgE3EQ4BKwERIxEkABE0ACURMxMeAQK+Tk1NTgHLSo9BD1mXOVOSOgqi/vr+9gEOAQKiAUeTA1osk2xtlCoDOf7cMDL9aQEyL/7bHiD+5AEgKAEuAQL0ASMjAR/+4QMdAAEAfQAABOcF8AAbAAABES4BIyIGHQEhFSERIREhETMRIzUzNRA2ITIWBNtGlE12cQF1/osCGvuW48LC/gETXLUFuv7iJyZ9g6rv/rr+9gEKAUbvqgEP+BsAAAIASgA9BM8ExQAjAC8AAAEHJzcuATU0NjcnNxc+ATMyFhc3FwceARUUBgcXBycOASMiJjcyNjU0JiMiBhUUFgGyz5nPHBweHtGZzzBsPTZsOc+Yzx0cHR7Pms8uaj86bKZbgH9cW4B+AQzPms8xaz8/bC7Nms8eHRscz5rPN242P2kvz5nOHh0btn9cXH9/XF1+AAABABkAAAV5BdUAGAAAASERIREhNSE1JyE1IQEhCQEhASEVIQcVIQVO/jn+g/46AcYx/msBJP6xAY8BIQEgAZD+sAEl/moxAccBoP5gAaDCQlbAAhv+MwHN/eXAVkIAAgEE/qIB5wWYAAMABwAAAREjERMRIxEB5+Pj4wWY/QoC9vwA/QoC9gAAAgAO/z0D+AXwADMAPwAAARUuASMiBhUUFxYXHgEVFAYHHgEVFAYjIiYnNR4BMzI2NTQnJicuATU0NjcuATU0NjMyFgEOARUUFhc+ATU0JgN1Y545S0y8Gg3Sn3F1TUvy1VW1ZnO2OUFOtCQTy6BvcUtB5clUtP6aREN7tkFGigW24ycnMS9DTwsFWa19dZ8wKXFJkacdHe0pKzIoRkoOCFezgmiaMzNvS5CiHf2FHEwyQ2JCF080Q2oAAgDFBTsDOwYxAAMABwAAEzMVIyUzFSPF6+sBi+vrBjH29vYAAwEbAAAG5QXNABkAMQBJAAABFS4BIyIGFRQWMzI2NxUOASMiJjU0NjMyFiciBgcOARUUFhceATMyNjc+ATU0JicuAScyBBcWEhUUAgcGBCMiJCcmAjU0Ejc2JAUrOW85cX9+ckBzLkGDPtP+/tNFgO550FdXV1dXVtF5e85XV1dXV1jPeZgBB21tbGxtbf75mJj++W1tbGxtbQEHBGbXJSOAcnN+JCPVFhfqwsPpFbdXV1fPennPV1ZWVVdXz3l6z1dYVppubW3++pqY/vttbW5ubW0BBZiaAQZtbW4AAwCeAXUD6QXwAAMADgApAAATIRUhASIGFRQWMzI2PQElESM1DgEjIiY1NDY7ATU0JiMiBgc1PgEzMhawAy380wHThWhCOllyAQz1N4xekaTS4olZVVemT1ypS+DYAj3IAsQ0PjM6clcWVP5Af0xIhnSNhBQ4OyMjtBwcrwAAAgCeAIkEagQnAAYADQAAARUNARUBNQEVDQEVATUCi/7bASX+EwPM/twBJP4TBCfy3d3yAXG6AXPy3d3yAXG6AAEA2QEfBdsDjQAFAAATIREjESHZBQLr++kDjf2SAYEAAQBvAbwC4wLfAAMAABMhESFvAnT9jALf/t0AAAQBGwAABuUFzQAXACAANABMAAABIgYHDgEVFBYXHgEzMjY3PgE1NCYnLgEDIxUzMjY1NCYnMhYVFAYHHgEfASMnLgErAREjEQEyBBcWEhUUAgcGBCMiJCcmAjU0Ejc2JAQAedBXV1dXV1bReXvOV1dXV1dYz7IjI05PTSuwrmlgKUcdb+VrJjodDNUBMZgBB21tbGxtbf75mJj++W1tbGxtbQEHBTNXV1fPennPV1ZWVVdXz3l6z1dYVv7ZzzU0NDKKd3lWcBEWUDrd1U5B/pwDRAE3bm1t/vqamP77bW1ubm1tAQWYmgEGbW1uAAABAMUFWAM7BhQAAwAAEyEVIcUCdv2KBhS8AAIAsgNkA0wF/gALAB0AAAEiBhUUFjMyNjU0JicyFhceARUUBgcOASMiJjU0NgIASGRjSUhkZUdCejAvMTEtMHxEjb/BBVxkSEhiY0dIZKIzLzB4REN5LTAzv42NwQACANkAAAXbBQQACwAPAAABESEVIREjESE1IREBIRUhA9ECCv327v32Agr99gUC+v4FBP6e7P6eAWLsAWL76u4AAQBtApwDDgXwABgAAAEhFSE1AT4BNTQmIyIGBzU+ATMyFhUUBgcBnAFy/V8BOT00STs+jlRXo0uetEdlA0SomQEKNVAoMj4tL7obG4FvSHlWAAEAWgKNAxIF8AAoAAABHgEVFAYjIiYnNR4BMzI2NTQmKwE1MzI2NTQmIyIGBzU+ATMyFhUUBgJQXGbGyVGUREKAPF9oa3JKVGJaTlA0e0ZBl1ensVoEYBJuUYGBFxauJCVAO0A9iS8zLS0aG6YREnBpRWAAAQFtBO4DogZmAAMAAAEhASMChwEb/o/EBmb+iAAAAQCu/lQFogRgACAAABMRIREUFjMyNjURIREUFjMyNjcVDgEjIiYnDgEjIiYnEa4BaWRmZ2QBaCEnEiETNV0tWXEjL4dZSmge/lQGDP11dHFxdAKL/RNHOAoM+hcWS1NPTy8w/hIAAQCB/zsEZAXVAA0AAAEhESMRIxEjES4BNTQkAlwCCL69vszeAQQF1flmBgf5+QNOGduyvugAAQDRAgYCOQOJAAMAABMhESHRAWj+mAOJ/n0AAAEBBv5vAssAAAATAAAhHgEVFAYjIiYvAR4BMzI2NTQmJwJaOjd7fzBmNAEyUyE6QSstPmovX1sNDZgQDy4oGlI8AAEAewKcAw4F3wAKAAATMxEHNTczETMVIY3P4eXizP1/AzkCCTSgMf1anQAAAwB1AXUEDgXwAAsADwAbAAABMhYVFAYjIiY1NDYDIRUhASIGFRQWMzI2NTQmAkLV9/bW1vf3xgM3/MkBnFRbW1RTW1sF8N6+vtzcvr7e/E3IA9F+dHR8fHR0fgACAMEAiQSNBCcABgANAAAJARUBNS0CARUBNS0BAqAB7f4TASX+2/4hAev+FQEk/twEJ/6Nuv6P8t3d8v6Nuv6P8t3d//8AZP/jB6gF8BAnAcAEev1kECcBvwOWAAAQBgB56QD//wBk/+MH5QXwECcBvwOWAAAQJwByBNf9ZBAGAHnpAP//AGj/4weoBfAQJwHABHr9ZBAnAb8DlgAAEAYAcw4AAAIAjf5uBB8EYAAdACEAAAEhFRQGDwEOARUUFjMyNjcRDgEjIiQ1NDY/AT4BNSUhESEB5wFpQW1AODRgVlG9ZXfLXPT/AE5eQEQqAWn+lwFpAmYxUX5kOjNcL0ZQREL+xioox75jm1g6PUwtwwFkAP//AAoAAAYnB2sSJgAiAAAQBwHMBQABdf//AAoAAAYnB2sSJgAiAAAQBwHKBQABdf//AAoAAAYnB2sSJgAiAAAQBwHNBRgBdf//AAoAAAYnB3MSJgAiAAAQBwHLBRgBe///AAoAAAYnB2sSJgAiAAAQBwHJBRIBdQADAAoAAAYnB20AEgAeACEAAAkBIQMhAyEBLgE1NDYzMhYVFAYlFBYzMjY1NCYjIgYDIQMECAIf/n1e/aZf/n0CHxcWp3Z0qBb+d002Nk1ONTZNSgGZzAW4+kgBEP7wBbgiSyt1qKh1L0x7Nk1NNjZNTfufAlIAAgAAAAAIGQXVAAMAEwAACQEhEQEhESERIREhESERIREhAyEDe/8AAXn+fQWR/XMCZv2aAqT72/4Sk/6NBNX9ngJiAQD+3f7q/t3+qv7dAV7+ogD//wBm/m8FXAXwEiYAJAAAEAcAeAFzAAD//wC8AAAE4QdrEiYAJgAAEAcBzAS0AXX//wC8AAAE4QdrEiYAJgAAEAcBygS0AXX//wC8AAAE4QdrEiYAJgAAEAcBzQS0AXX//wC8AAAE4QdrEiYAJgAAEAcByQS0AXX//wAWAAACPQdrEiYAKgAAEAcBzANkAXX//wC8AAACsgdrEiYAKgAAEAcBygNkAXX//wADAAAC9QdrEiYAKgAAEAcBzQN8AXX//wBBAAACtwdrEiYAKgAAEAcByQN8AXUAAgAhAAAGTAXVAAwAHwAAAREzESMRMzI2NTQmIwEhIAQXFhIVFAIHBgQpAREjETMCUOvriez5+O399gGVAVUBTHhoZ2doef6w/rD+a66uBLL+v/78/rbq397oASNhdGX++Kep/vdldGECbQEEAP//ALwAAAX2B20SJgAvAAAQBwHLBTUBdf//AGb/4wZmB2sSJgAwAAAQBwHMBU4Bdf//AGb/4wZmB2sSJgAwAAAQBwHKBU4Bdf//AGb/4wZmB2sSJgAwAAAQBwHNBU4Bdf//AGb/4wZmB20SJgAwAAAQBwHLBWcBdf//AGb/4wZmB2sSJgAwAAAQBwHJBWYBdQABAQAAKQW0BNsACwAACQIHCQEnCQE3CQEFtP5OAbKo/k7+TqgBsv5OqAGyAbIEM/5O/lCoAbD+UKgBsAGyqP5OAbIAAAMALf+2BpYGHwAJABMAKwAAAR4BMzISNTQmLwEuASMiAhUUFhcBLgE1EAAhMhYXNxcHHgEVEAAhIiYnBycCXDSDU7HCDxBNM4JSsMIODv7qSkoBmQFnmvhmx3HJTUz+aP6Ymf9mynEBcz47AQTrRHUxkzo5/vzsQHEu/upk+pcBawGcS03Hc8dj/5r+lv5kT0/Lcf//ALz/4wXDB2sSJgA2AAAQBwHMBScBdf//ALz/4wXDB2sSJgA2AAAQBwHKBScBdf//ALz/4wXDB2sSJgA2AAAQBwHNBUABdf//ALz/4wXDB2sSJgA2AAAQBwHJBUABdf///+wAAAXfB2sSJgA6AAAQBwHKBM0BdQACALwAAAWJBdUADAAVAAABESERIREzIAQVFAQhAxEzMjY1NCYjAj3+fwGB/gEdATH+z/7j/tVwenpwAQL+/gXV/vz96+r9Arr+XW1jZW4AAAEArP/jBWgGFAAwAAATNCQhIAQdAQ4BFRQWHwEeARUUBiMiJic1HgEzMjY1NCYvAS4BNTQ2Ny4BIyIGFREhrAEOAREBBgEMl5AxXUV0a+XnQYpKOHM2SFg3YkZYVIuRAWBbZWb+mgRa3tzg2kcKTkolOTQlQKl1vbwZGPQbHEg5L0Q3JzGHWnSeMlVZbm37tAD//wBY/+MExQZmEiYAQgAAEAcAQQC6AAD//wBY/+MExQZmEiYAQgAAEAcAdAC6AAD//wBY/+MExQZmEiYAQgAAEAcBQgC6AAD//wBY/+MExQY5EiYAQgAAEAcBRwC6AAD//wBY/+MExQYxEiYAQgAAEAcAaAC6AAD//wBY/+MExQcbEiYAQgAAEAcBRQC6AAAAAwBY/+MIAAR7AAYAEQA+AAABNCYjIgYHBSIGFRQWMzI2PQEBPgEzMhYXPgEzIAARFSEeATMyNjcRDgEjIiQnDgEjIiY1NCQhMzU0JiMiBgcGj3dgZ4AQ/eFwcVtRZYr9XnffYZbZR03MegEJAT38ug6bjXHtfX//frP+90hl34vC4gEPASLTho5zxlUCqmZ9dW6yTEpETZFtKQJKHB1NT01P/sL+9mZ+fkNE/uwwMWtka2TFqMW4HFVPLi4A//8AWP5vBDUEexImAEQAABAHAHgAuAAA//8AWP/jBQoGZhImAEYAABAHAEEA2QAA//8AWP/jBQoGZhImAEYAABAHAHQA2QAA//8AWP/jBQoGZhImAEYAABAHAUIA2QAA//8AWP/jBQoGMRImAEYAABAHAGgA2QAA////1QAAAhIGZhImAPEAABAHAEH/dwAA//8ArAAAAxkGZhImAPEAABAHAHT/dwAA////5QAAAtcGZhImAPEAABAHAUL/XgAA//8AIwAAApkGMRImAPEAABAHAGj/XgAAAAIAWP/jBScGFAAOACgAAAEuASMiBhUUFjMyNjU0JhMWEhUQACEgABE0ACEyFhcnBSclJyEXJRcFA5g3bDR1f4JydXwNo3Vq/rv+3/7e/rkBLQEILk4kvv6LJQEzvAFgbwF4I/7FAucbG4V5lKiroS1cAZSI/v+U/uz+yAE4ARTnAQkNDtt3gWHKdHKBYP//AKwAAAUSBjkSJgBPAAAQBwFHAPIAAP//AFj/4wUnBmYSJgBQAAAQBwBBANcAAP//AFj/4wUnBmYSJgBQAAAQBwB0ANcAAP//AFj/4wUnBmYSJgBQAAAQBwFCAL8AAP//AFj/4wUnBjkSJgBQAAAQBwFHAL4AAP//AFj/4wUnBjESJgBQAAAQBwBoAL4AAAADANkAVgXbBK4AAwAHAAsAAAEhESERIREhBSEVIQLBATP+zQEz/s3+GAUC+v4Bi/7LBFj+y4HsAAMATv+iBSkEwQAJABMAKwAAAS4BIyIGFRQWHwEeATMyNjU0JicBLgE1EAAhMhYXNxcHHgEVEAAhIiYnBycDWB1LL3d9BwdIH08wdXwHB/07Q0QBRwEiarNLk22NRkX+u/7fbLZNlHADRBwbq6EpQRuLHh6roStDHf3kTsh7ARQBOCwsnmWVUMp+/uz+yC0tm17//wCg/+MFBgZmEiYAVgAAEAcAQQDyAAD//wCg/+MFBgZmEiYAVgAAEAcAdADyAAD//wCg/+MFBgZmEiYAVgAAEAcBQgDUAAD//wCg/+MFBgYxEiYAVgAAEAcAaADUAAD//wAZ/kYFEgZmEiYAWgAAEAcAdACcAAAAAgCs/lYFXgYUABAAHAAAJREhESERPgEzMgAREAAjIiYTIgYVFBYzMjY1NCYCEv6aAWZKtHXPAQr+9s91tKRze3tzc3l5ov20B779qGJd/rf+/f79/rddAzepn5+pqKCgqP//ABn+RgUSBjESJgBaAAAQBwBoAJwAAP//AAoAAAYnB08QJwBvARgBOxIGACIAAP//AFj/4wTFBhoQJwBvAIkABhIGAEIAAP//AAoAAAYnB3oQJwFEARUBNBIGACIAAP//AFj/4wTFBj0QJwFEANr/9xIGAEIAAP//AAr+bwYnBdUQJwFGAt8AABIGACIAAP//AFj+bwTFBHsQJwFGAZwAABIGAEIAAP//AGb/4wVcB2sSJgAkAAAQBwHKBWYBdf//AFj/4wR1BmYSJgBEAAAQBwB0ANMAAP//AGb/4wVcB2sQJwHNBY8BdRIGACQAAP//AFj/4wRWBmYQJwFCAN0AABIGAEQAAP//AGb/4wVcB2sQJwHQBY8BdRIGACQAAP//AFj/4wQ1BhQQJwFJBN8AABIGAEQAAP//AGb/4wVcB2sSJgAkAAAQBwHOBWYBdf//AFj/4wRMBmYSJgBEAAAQBwFDANMAAP//ALwAAAY5B2sSJgAlAAAQBwHOBQsBdf//AFz/4wb4BhQQJgBFAAAQBwHICCD/rP//ACEAAAZMBdUQBgCQAAAAAgBc/+MFqAYUABgAJAAAAREhNSE1IRUzFSMRITUOASMiABEQADMyFgMyNjU0JiMiBhUUFgOm/roBRgFompr+mEqydc/+9gEKz3SzonN5eXNyeXkDvAEZzXJyzfsromNcAUkBAwEDAUld/MmooKCoqKCgqP//ALwAAAThB08QJwBvAMQBOxIGACYAAP//AFj/4wUKBhsQJwBvAK0ABxIGAEYAAP//ALwAAAThB2sQJwHPBLQBdRIGACYAAP//AFj/4wUKBkYQJwFEANkAABIGAEYAAP//ALwAAAThB2sQJwHQBLQBdRIGACYAAP//AFj/4wUKBhQQJwFJBNsAABIGAEYAAP//ALz+bwTiBdUQJwFGAeAAABIGACYAAP//AFj+bwUKBHsQJwFGAZgAABIGAEYAAP//ALwAAAThB2sSJgAmAAAQBwHOBMkBdf//AFj/4wUKBmYSJgBGAAAQBwFDANMAAP//AGb/4wX6B2sQJwHNBaQBdRIGACgAAP//AFz+RgUOBmYQJwFCALoAABIGAEgAAP//AGb/4wX6B2sSJgAoAAAQBwHPBTEBdf//AFz+RgUOBkYSJgBIAAAQBwFEAN0AAP//AGb/4wX6B2sQJwHQBaQBdRIGACgAAP//AFz+RgUOBhQQJwFJBLwAABIGAEgAAP//AGb+NgX6BfAQJwFMBV8AHxIGACgAAP//AFz+RgUOBh8QJwFLBEoBnRIGAEgAAP//ALwAAAX2B2sQJwHNBVkBdRIGACkAAP///+0AAAUSB2sQJwHNA2YBdRIGAEkAAAACALwAAAcOBdUAEwAXAAABIRUhNSEVMxUjESERIREhESM1MwUVITUBSAGBAjgBgYyM/n/9yP5/jIwBgQI4BdW7u7vC+6gCef2HBFjCwry8AAEApgAABawGFAAfAAABESE1ETQmJy4BIyIGFREhESM1MzUhFSEVIRE+ATMyFgWs/pgNEBVILnCA/pqgoAFmAWv+lVG2bsLJAqr9Vm8BmZNuGiMnrZn92QTnwmtrwv7VYl3uAP//ACAAAALYB20QJwHLA3wBdRIGACoAAP//AAMAAAK7BjkQJwFH/18AABIGAPEAAP//AEEAAAK3B08QJwBv/3wBOxIGACoAAP//ACQAAAKaBhsQJwBv/18ABxIGAPEAAP//ACwAAALMB2sQJwHPA3wBdRIGACoAAP//AA8AAAKvBkYQJwFE/18AABIGAPEAAP//ALz+bwLtBdUQJgFG6wASBgAqAAD//wCs/m8CwgYUECYBRsAAEgYASgAA//8AvAAAAj0HaxImACoAABAHAdADgAF1AAEArAAAAhIEYAADAAATIREhrAFm/poEYPugAP//ALz+ZgU2BdUQJwArAvkAABAGACoAAP//AKz+RgTQBhQQJwBLAr4AABAGAEoAAP///43+ZgL1B2sQJwHNA3wBdRIGACsAAP///7z+RgLYBmYQJwFC/18AABIGAUAAAP//ALz+UwZxBdUQJwFMBWYAPBIGACwAAP//AKz+UwV5BhQQJwFMBOIAPBIGAEwAAAABAKwAAAV5BGAACgAAEyERASEJASEBESGsAWYBnAGg/d0CTv5O/kv+mgRg/mUBm/3+/aIB0/4t//8AvAAABOEHbBAnAcoDvwF2EgYALQAA//8ArAAAAtsHbBAnAcoDjQF2EgYATQAA//8AvP5TBOEF1RAnAUwEngA8EgYALQAA//8Akf5TAi8GFBAnAUwDLwA8EgYATQAA//8AvAAABOEF1RAnAcgGBv9vEgYALQAA//8ArAAAA9YGFBAnAcgE/v+tEAYATQAA//8AvAAABOEF1RAnAHcCggC6EgYALQAA//8ArAAAA98GFBAnAHcBpgC2EAYATQAAAAH/pAAABOwF1QANAAATIRE3FwERIREhEQcnJccBgf6P/nMCpPvblI8BIwXV/mC5wf7w/gb+3QIMar7FAAH/2wAAAx8GFAALAAATIRE3FwcRIREHJzfHAWiBb/D+mH1v7AYU/gtYmqT8xwKBVpqjAP//ALwAAAX2B2wQJwHKBSsBdhIGAC8AAP//AKwAAAUSBm0QJgB0fQcSBgBPAAD//wC8/lMF9gXVECcBTAUpADwSBgAvAAD//wCs/lMFEgR7ECcBTASvADwSBgBPAAD//wC8AAAF9gdrEiYALwAAEAcBzgVxAXX//wCsAAAFEgZmEiYATwAAEAcBQwCpAAD//wBpAAAHIQXVECcATwIPAAAQBgFB6AAAAQCs/mYF2AXwAB0AACUQBwYhIxEzMjY1ERAnJiMiBhURIREhFT4BMzISEQXYhJf+zU48eH8xQpGdsv6QAXRv6JHj7ZH+13iKASOKfgIiATZFXObK/SYF1eOHd/7E/tMAAQCs/kYFEgR7ACQAAAERFAcGIyE1MzI2NRE0JyYnJicmIyIHBhURIREhFTY3NjMyFxYFEm5szf7npmZMBgcQFSQkLnBAQP6aAWZRW1tuwmVkAqr9at95dutchwH2kTc3GiMUE1dWmf3ZBGCkYi4vd3f//wBm/+MGZgdPECcAbwFmATsSBgAwAAD//wBY/+MFJwYbECcAbwDBAAcSBgBQAAD//wBm/+MGZgdrECcBzwVgAXUSBgAwAAD//wBY/+MFJwZMECcBRAC/AAYSBgBQAAD//wBm/+MGZgdrECcB0QVOAXUSBgAwAAD//wBY/+MFJwZmECcBSADXAAASBgBQAAAAAgBm//4IwQXXAAgAHwAAASMgBBUUBCEzAyERIREhESERIREhIgYjIAAREAAhMhYEnGn+3/7iAR8BIGlaBGj9cwJm/ZoCpPuBDS8M/kb+JgHaAboLMASy4uTl5ASy/t3+6v7d/qr+3QIBhQFpAWgBgwIAAwBY/+MIXgR7AAYAJwAzAAABNCYjIgYHBRUhHgEzMjY3EQYEIyImJw4BIyAAERAAITIWFz4BMyAAJSIGFRQWMzI2NTQmBu53YGiCEANB/LsNnIxx7X1+/wB+pdZIUtWC/t7+uQFHASKGzlFSx4cBFgFC+mN3fX13dXx8AqpmfXVud2Z+fkNE/uwwMVFXVFQBOAEUARQBOFJWV1H+xjqroaGrq6GhqwD//wC8AAAGAAdsECcBygS5AXYSBgAzAAD//wCsAAAEHwZtECYAdH0HEgYAUwAA//8AvP5TBgAF1RAnAUwFLgA8EgYAMwAA//8Akf5TA+wEexAnAUwDLwA8EgYAUwAA//8AvAAABgAHaxImADMAABAHAc4EyQF1//8ArAAAA+wGZhImAFMAABAGAUNVAP//AJP/4wUtB2wQJwHKBLkBdhIGADQAAP//AGr/4wRiBm0QJgB0fQcSBgBUAAD//wCT/+MFLQdrECcBzQTBAXUSBgA0AAD//wBq/+MEYgZmECYBQloAEgYAVAAA//8Ak/5vBS0F8BImADQAABAHAHgA3QAA//8Aav5vBGIEexImAFQAABAGAHhiAP//AJP/4wUtB2sSJgA0AAAQBwHOBMkBdf//AGr/4wRiBmYQJwFKBFwAABIGAFQAAP//AAr+bwVqBdUQJwB4AL0AABIGADUAAP//ABv+bwOkBZ4QJgB4AAASBgBVAAD//wAKAAAFagdxEiYANQAAEAcBzgS3AXv//wAbAAAEDwaDEiYAVQAAEAcByAU3AB0AAQAKAAAFagXVAA8AABMhESERMxEjESERIxEzESEKBWD+Eff3/n/39/4QBdX+3f5L/vz+BwH5AQQBtQAAAQAbAAADpAWeAB0AAAERIREhFSERIRUUFxY7AREhIicmPQEjETM1IxEzEQIzAXH+jwFx/o8fH1y4/s3UWFmysrKyBZ7+wv8Ajv8ATU4bHP8AWFnUTQEAjgEAAT4A//8AvP/jBcMHbRAnAcsFPwF1EgYANgAA//8AoP/jBQYGORAnAUcA8gAAEgYAVgAA//8AvP/jBcMHTxAnAG8BQAE7EgYANgAA//8AoP/jBQYGGhAnAG8A0wAGEgYAVgAA//8AvP/jBcMHaxAnAc8FQAF1EgYANgAA//8AoP/jBQYGRhAnAUQA8gAAEgYAVgAA//8AvP/jBcMHbhImADYAABAHAUUBRABT//8AoP/jBQYHDRImAFYAABAHAUUA3P/y//8AvP/jBcMHaxAnAdEFJwF1EgYANgAA//8AoP/jBQYGZhAnAUgA8gAAEgYAVgAA//8AvP5vBcMF1RImADYAABAHAUYBNAAA//8AoP5vBboEYBImAFYAABAHAUYCuAAA//8APQAACJMHchAnAc0GaAF8EgYAOAAA//8ASAAABx0GZhAnAUIBsgAAEgYAWAAA////7AAABd8HchAnAc0E5QF8EgYAOgAA//8AGf5GBRIGZhAnAUIAlQAAEgYAWgAA////7AAABd8HaxImADoAABAHAckE5QF1//8AXAAABXEHbBAnAcoEuQF2EgYAOwAA//8AXAAABEYGbRAmAHR9BxIGAFsAAP//AFwAAAVxB28QJwHQBNIBeRIGADsAAP//AFwAAARGBhQQJwFJBFYAABIGAFsAAP//AFwAAAVxB2sSJgA7AAAQBwHOBM8Bdf//AFwAAARGBmYSJgBbAAAQBgFDVAAAAQAnAAADjQYUABAAACkBESMRMzU0NjMhFSMiBwYVAj/+mrKyzNYBEsZMHh4DYAEATrev6xsdQwAB/7z+RgISBGAACwAAEyERFAYrATUzMjY1rAFm2M2xPmZMBGD7tOHt61yH//8AgQNYAjkF1RAGAbYAAAABAIcE7gN5BmYABgAAATMBIycHIwGH8gEAssfHsgZm/ojh4QABAIcE7gN5BmYABgAACQEzFzczAQGH/wCyx8ey/wAE7gF44+P+iAAAAQCwBR0DUAZGAA0AABMzHgEzMjY3Mw4BIyImsI8LY1NTYwuPBq6cnK4GRkZKSkaQmZkAAAIA4wThAx0HGwALABcAAAEUFjMyNjU0JiMiBgc0NjMyFhUUBiMiJgF9TTY3TE02N0yap3Z2p6d2dqcF/jdMTTY2TU02dqendnanpwABAVb+bwMCAAAAEwAAITMOARUUFjMyNjcVDgEjIiY1NDYBxY0yJjsxJ00oN14pc3s2Q0kaJzEPEJwLC1xWNW0AAQCkBRsDXAY5AB4AAAEnJicmIyIGHQEjNDYzMhYfAR4BMzI2PQEzFAYjIiYCAjcEBi8ZJCaLZ10kSSk9FiUPJCiLZ10kQwVUJQIEHz47CIiUGx4rDxBAOQiIlBgAAAIAwQTuA9UGZgADAAcAAAEzAyMBMwEjAYPZ+KMCLef+8K4GZv6IAXj+iAAAAf1LBPD+sQYUAAMAAAEhESH9SwFm/poGFP7c///8hQTu/3cGZhAHAUP7/gAAAAH91ANY/3IEggADAAADIRMz6P683sADWAEqAAAB/WL+F/8A/0EAAwAABSEDI/28AUTewL/+1gD//wC8AAAE4QdrECcBzAU3AXUSBgFiAAD//wC8AAAE4QdrECcByQSuAXUSBgFiAAAAAQAK/mYGYgXVACMAACUQBwYhIxEzMjc2PQE0JyYnJicmIyERIREhESERIREhMhcWFQZimJf+zU48eD49BwYQFSQkLv6U/n/+vgSy/hECCsBmZZH+6YqKASNDQ4KDkzc3GiMUE/2HBLIBI/7d/up3deX//wC8AAAE4QdsECcBygS5AXYSBgFgAAAAAQBm/+MFXAXwABgAAAEeATMyNxEGIyAAERAAITIXESYjIgYHIRECAxHK2NfP1vf+i/5MAbQBdffWz9fYyhECoQJYeOaN/stvAaEBZgFlAaFv/suN5nj+3QD//wCT/+MFLQXwEgYANAAA//8AvAAAAj0F1RIGACoAAP//AEEAAAK3B2sQBgCPAAD///+N/mYCPQXVEgYAKwAAAAIAXgAACNEF1QAIAB0AAAEyNjU0JisBERMhESEVEAIFESQSGQEhETMgBBUUBAZneXBven6a/eX+28L93QEAZAQnmgEbATP+zQEGXl1bXf6N/voEs1z92/4GOAEjLwEoAkEBGv3H3/Du3wACALwAAAh6BdUACAAbAAABNCYrAREzMjYBIREhESERMyAEFRQEKQERIREhBvlven5+eXD5wwGBAdQBgZoBGwEz/s3+5f3l/iz+fwHBW13+jV4Ecf3HAjn9x9/w7t8Cef2HAAEACgAABmIF1QAaAAABMhcWFREhPQE0JyYnJicmIyERIREhESERIREE18BmZf5/BwYQFSQkLv6U/n/+vgSy/hEDnHd15f41b6WTNzcaIxQT/YcEsgEj/t3+6gD//wC8AAAGbAdsEiYBZwAAEAcBygU5AXb//wC8AAAF9gdrEiYBZQAAEAcBzAVwAXX//wA7AAAF7gdrEiYBcAAAEAcBzwUcAXUAAQC8/r8F9gXVAAsAACERIREhESERIREhEQPr/tz99QGBAjgBgf6/AUEF1ftOBLL6K///AAoAAAYnBdUSBgAiAAAAAgC8AAAFiQXVAAoAGQAAATI3NjU0JyYrARETIREhESERMyAXFhUUBwYDH3k2Ojo1euL+/YEEaf0Y/gEboZKSoQEGLTFdWzAt/o3++gXV/t3+6nVq8O5qdQD//wC8AAAFiQXVEgYAIwAAAAEAvAAABOEF1QAFAAAzESERIRG8BCX9XAXV/t37TgAAAgB7/r8GpQXVAAUAFAAAASERIRUQBT4BGQEhETMRIREhESERArEBoP69/d1oOgRF0/7d/Bz+3QEjA49b/YC0RcsCiAEa+079nAFB/r8CZP//ALwAAAThBdUSBgAmAAAAAQAeAAAJrQXVABMAADMJASEBESERASEJASEBBxEhEScBHgJj/d4BlAIyAYECMgGU/d4CY/5Y/lOy/n+y/lMDfwJW/ZgCaP2YAmj9qvyBAnXD/k4BssP9iwAAAQCH/+MFKAXwACgAAAEeARUUBCEiJicRHgEzMjY1NCYrATUzMjY1NCYjIgYHETYkMyAEFRQGA/SXnf6s/pyT6mxs1Zmjo6fBuMCvjoqIjvZFQwEnXgFHAU2KAyUnwZXe5yYkASk2N2pjZmn4W11WXjEiARoXKb/Ag6cAAAEAvAAABfYF1QAJAAABESERASERIREBBfb+k/3h/lIBbQIfBdX6KwQA/AAF1fwABAD//wC8AAAF9gdrEiYBZQAAEAcBzwU4AXUAAQC8AAAGbAXVAAsAABMhEQEhCQEhAQcRIbwBgQJaAbT9rwJy/lj+P8b+fwXV/ZgCaP2j/IgCfMr+TgAAAQBeAAAF6QXVAA0AADMRNhIZASERIREhFRACXv5mBCf+f/7bwgEjHAFLAjEBGvorBLJb/dz+Bf//ALwAAAc5BdUSBgAuAAD//wC8AAAF9gXVEgYAKQAA//8AZv/jBmYF8BIGADAAAAABALwAAAX2BdUABwAAAREhESERIREF9v5//cj+fwXV+isEsvtOBdX//wC8AAAFiQXVEgYAMQAA//8AZv/jBVwF8BIGACQAAP//AAoAAAVqBdUSBgA1AAAAAQA7AAAF7gXVABAAACUGBwYhIxEzMjc2NwEhCQEhA6MoO3j+rEZqjCEIB/3nAZIBSwFCAZT4VTZtASNFDw8ET/1YAqgAAAMAZgAAB4gF1QAGAA0AHwAAARQWFxEOAQU0JicRPgEBIRUEABEQAAUVITUkABEQACUB9JuoqJsEB5uoqJv9PAGBAW8BYf6f/pH+f/6R/p4BYgFvAvmWhg4CVQ6HlpaHDv2rDoYDcpQe/u7+6P7o/u8esrIeAREBGAEYARIe//8AJwAABgIF1RIGADkAAAABALz+vwbxBdUACwAAKQERIREhESERMxEhBc767gGBAjgBgfv+3QXV+04EsvtO/ZwAAAEApQAABbsF1QAPAAAhESEiJjURIREUFjMhESERBDr99r7NAYFKXgFsAYECOurnAcr+7ex5Anj6KwABALwAAAklBdUACwAAASERIREhESERIREhBbEB8wGB95cBgQHzAYEBIwSy+isF1ftOBLIAAQC8/r8KIAXVAA8AAAEzESERIREhESERIREhESEJJfv+3fe/AYEB8wGBAfMBgQEj/ZwBQQXV+04EsvtOBLIAAAIAZAAABx4F1QAIABUAAAEyNjU0JisBEQEhESERMyAEFRQEKQEEtHlwb3ri/n/+EwNu/gEbATP+zf7l/YEBBl5dW13+jQOsASP9x9/w7t///wC8AAAHjgXVECYBeQAAEAcBUwVRAAAAAgC8AAAFiQXVAAoAFwAAATI3NjU0JyYrARElFAcGKQERIREzIBcWAx95Njo6NXriA0ySof7l/YEBgf4BG6GSAQYtMV1bMC3+jcfuanUF1f3HdWoAAQCD/+MFeQXwABcAABMWMzI2NyERIS4BIyIHETYzIAAQACEiJ4PP19jKEf1fAqERytjXz9b3AXUBtP5M/ov31gGHjeZ4ASN45o0BNW/+X/01/l9vAAACALz/4wjzBfAAFAAgAAABNjc2ISAAERAAISAnJicjESERIREBIgIVFBIzMhI1NAIC/By6ugFnAWgBmP5o/pj+mbq6HL/+fwGBA7awwsKwscLCA3v7vb3+ZP6V/pb+ZL29+/2oBdX9pgFe/vzs6/78AQTr7AEEAAACAIMAAAVtBdUACAAWAAABFBY7AREjIgYJAS4BNTQkKQERIREjAQJKaXnAwHlp/jkBdEziARMBJwJq/n+D/rQEAGdaAX9Y+5oCeivXleDk+isCNf3L//8AWP/jBMUEexIGAEIAAAACAFj/4wU+BlcAHgAqAAATJyY1NDc2NzYlNjcXBgUGBwYHNjMgABEQACEgABE0ASIGFRQWMzI2NTQmbgcPOmuPdgHrMjlQTP56q0Z0CZPdASEBRf68/t7+3v65Amh2fX12dnx8Am2nQ0PEgOwwKScECd4UIg8wT5Vb/sj+7P7s/sgBOAEUJgEmq6Ghq6uhoasAAAMArAAABLYEYAAIABEAIAAAATI2NTQmKwEVEzI2NTQmKwEVASEyFhUUBgceARUUBiMhArs+QEA+qbVPUFBPtf6aAgH531JNY23q7v3OArozMjIyyf4mPz8+PvoDgI2bUnMcG5Fqop8AAQCsAAAD/QRgAAUAADMRIRUhEawDUf4VBGDd/H0AAgBz/uUGAwRgAA4AFAAAEz4BETUhETMRIREhESERASMVEAchr3JgA8i6/wD8cP8AA3D8UwFPAQAm/QFp1Pyg/eUBG/7lAhsCYB/+kdL//wBY/+MFCgR7EgYARgAAAAEAHgAAB9gEYAATAAAzCQEhAREhEQEhCQEhAQcRIREnAR4B+/4sAYgBewFmAXsBiP4sAfv+jv6iWv6aWv6iApkBx/6PAXH+jwFx/jn9ZwHKV/6NAXNX/jYAAAEAZP/jBCQEewAgAAABMzI2NTQjIgcRNjMyFhUUBxYVFAQhIicRFjMyNjU0ISMBIKSTa+zFerbX/Ojb9/7w/t3ZtOCFkoP+4J4CukAsZ0UBAzCPl8YzPeGtrjgBFF5PMosAAQCsAAAE7wRgAAkAAAERIREBIREhEQEE7/6a/pf+jAFmAWkEYPugAlT9rARg/awCVP//AKwAAATvBh4SJgGFAAAQBwFEAM7/2AABAKwAAAVQBGAACwAAEyERASEJASEBBxEhrAFmAY8BiP4iAgX+jv6ZZf6aBGD+hQF7/jn9ZwHPYP6RAAABAHEAAAUwBGAADwAAMxE2NzYRNSERIREjFRAHAnGxKB4DyP6a/EWMAQAkdVkBt7f7oANgJf5JgP78AAABAKwAAAXdBGAADAAAEyEbASERIREDIwMRIawBnPz8AZ3+m73rvv6aBGD90AIw+6ACe/5cAaT9hQAAAQCsAAAE2wRgAAsAABMhESERIREhESERIawBZgFjAWb+mv6d/poEYP5WAar7oAHZ/icA//8AWP/jBScEexIGAFAAAAABAKwAAATbBGAABwAAAREhESERIREE2/6a/p3+mgRg+6ADYPygBGD//wCs/lYFXgR7EgYAUQAA//8AWP/jBDUEexIGAEQAAAABAAgAAASaBGAABwAAEyEVIREhESEIBJL+av6b/mkEYN38fQOD//8AGf5GBRIEYBIGAFoAAAADAHH+Vgd/BhQACgAkAC8AAAEiBhUUFjMyNxEmEyERNjMyABEQACMiJxEhEQYjIgAREAAzMhcFIgcRFjMyNjU0JgKdQXl5QWs9PT0BZmqRzwEK/vbPkWr+mmqRz/72AQrPkWoCDms9PWtBeXkDd6igoKhKAfxKAp3+Hkn+t/79/v3+t0n+KgHWSQFJAQMBAwFJSbtK/gRKqKCgqP//AB8AAAUKBGASBgBZAAAAAQCs/uUFlQRgAAsAAAERIREhESERIREzEQSV/BcBZgFjAWa6/uUBGwRg/KADYPyg/eUAAAEAhAAABJYEYAAPAAAhESEiJjURIRUUFjsBESERAzD+kJamAWY6TMABZgGxsa4BUMeyWQHS+6AAAQCsAAAHxgRgAAsAAAEhESERIREhESERIQTsAXQBZvjmAWYBdAFmAQADYPugBGD8oANgAAEArP7lCIAEYAAPAAApAREhESERIREhESERMxEhB4D5LAFmAXQBZgF0AWa6/wAEYPygA2D8oANg/KD95QAAAgAoAAAFsQRgAAgAFAAAJTI2NTQmKwEVBREhNSERMzIWEAYjA8NPUE9Qtf6a/oAC5svj9ffh4D8/Pj764AOD3f5Xp/6UpAD//wCsAAAGlgRgECcA8QSEAAAQBgGZAAAAAgCsAAAEtQRgAAoAFwAAATQnJisBFTMyNzYBIREzMhcWFRQHBiMhA2YoJ1C1tU8oKP1GAWbL43p7e3rj/c8BXj8fHvogHwNB/ldTVLa2UlIAAAEAif/jBGYEewAWAAABIS4BIAcRNjMgABAAISInERYzMjY3IQEMAdoNj/7Oj6quAS8BVv6q/tGwqIGgm5YN/iQCl0uZYgEkPv7J/db+yT4BJWOWVQAAAgCs/+MHbAR7ABQAIAAAEyERMzY3NiEgABEQACEgJyYnIxEhASIGFRQWMzI2NTQmrAFmkhSWlgEiASEBRf67/t/+3paVFJP+mgRad319d3V8fARg/j/Ajo7+yP7s/uz+yI+QwP4+A3uroaGrq6GhqwAAAgA/AAAEegRgAAgAFgAAARQWOwERIyIGCQEuATU0NjMhESERIwMB6U9bgYFbT/5WASVVftfxAiH+mn76Av5IQAEPPvy5Ad8xq2qfnPugAZn+ZwD//wBY/+MFCgZtEiYBggAAEAcAQQCUAAf//wBY/+MFCgYxEiYBggAAEAcAaADFAAAAAQAo/kYFWgYUAB4AABM1MxEhESEVIRE2MzISERAABzU+ATU0JiMiBh0BIREosgFmAfD+EJ7AyfP+ie98eF14U4D+mgOD3QG0/kzd/tm//u3+6P7F/rQj1hPR1rl+rZnHA4MA//8ArAAABCgGbRImAYAAABAHAHQAhgAHAAEAWP/jBDUEewAYAAABIR4BMzI3EQYjIAAREAAhMhcRJiMiBgchA7L+JA2Wm6CBqLD+0f6qAVYBL66qj5mZjw0B2gHOVZZj/ts+ATcBFQEVATc+/tximUsA//8Aav/jBGIEexIGAFQAAP//AKwAAAISBhQSBgBKAAD//wAjAAACmQYxEAYArwAA////vP5GAhIGFBIGAEsAAAACAFoAAAeKBGAAFQAeAAABIREzMhYQBiMhESMVEAcCIRE2NzYRATI2NTQmKwEVAVEDyJnj9fXj/gH8RYz+dLEoHgRLT1BQT4MEYP5Xp/6UpANgJf5JgP78AQAkdVkBt/03Pz8+PvoAAAIArAAAB0wEYAAIABoAACUyNjU0JisBFRchESERIREhESERIREzMhYQBgVeT1BTTIOZ/gH+nf6aAWYBYwFmmeP19eA/Pz4++uAB2f4nBGD+VgGq/len/pSkAAEAKAAABUAGFAAbAAATNTMRIREhFSERPgEzMhYVESE1NCYjIgYdASERKLIBZgHw/hBRtm7Cyf6YVFRyfv6aA4PdAbT+TN3+2WJd7uP+trzhcK2ZxwODAP//AKwAAAVQBm0SJgGHAAAQBwB0AMMAB///AKwAAATvBm0SJgGFAAAQBwBBAQoAB///ABn+RgUSBh4SJgGQAAAQBwFEAJb/2AABAKz+5QTbBGAACwAAASERIREhESERIREhA0T/AP5oAWYBYwFm/mn+5QEbBGD8oANg+6AAAQC8AAAE4QcHAAcAADMRIREhESERvAMCASP9XAXVATL9q/tOAAABAKwAAAP9BZoABwAAMxEhETMRIRGsAnTd/hUEYAE6/en8fQABAG8BvALjAt8AAwAAEyERIW8CdP2MAt/+3QD//wBvAbwC4wLfEgYBrwAAAAEAbgGwBSMCsgADAAATIREhbgS1+0sCsv7+AAABAG4BsAOSArIAAwAAEyERIW4DJPzcArL+/gAAAQBuAbAHkgKyAAMAABMhESFuByT43AKy/v4AAAEAAAGwCAACsgADAAARIREhCAD4AAKy/v4AAQDTA1gCiwXVAAUAAAEhERMzAwIn/qzj1WQDWAEdAWD+oAAAAQCBA1gCOQXVAAUAABMhEQMjE+UBVOPVZAXV/uP+oAFgAAEAk/8GAkwBgwAFAAATIREDIxP4AVTm02UBg/7j/qABYAABAIEDWAI5BdUABQAAARETIwMRAdVk1eMF1f7j/qABYAEdAAACANMDWASFBdUABQALAAABIRETMwMBIRETMwMEIf6s49Vk/gb+rOPVZANYARsBYv6e/uUBHQFg/qAAAgC8A1gEbwXVAAUACwAAASERAyMTASERAyMTASEBVOTVZQH6AVTk1WUF1f7j/qABYAEd/uH+ogFeAAIAk/8GBEYBgwAFAAsAABMhEQMjEwEhEQMjE/gBVObTZQH6AVTk1WUBg/7j/qABYAEd/uH+ogFeAAABAScBkQP2BGAAFwAAATQ2Nz4BMzIWFx4BFRQGBw4BIyImJy4BASc1MzWCSUmDMjQ1NjMzg0pJgjMyNgL6SoIyMzU2MjSBSUqDMzM2NjMzgwAAAwCiAAAHXgGDAAMABwALAAABIREhASERIQEhESEF9gFo/pj6rAFo/pgCqgFo/pgBg/59AYP+fQGD/n0ABwBC/+MLVgXwAAsAFwAjAC8AMwA/AEsAAAEiBhUUFjMyNjU0JicyFhUUBiMiJjU0NgEiBhUUFjMyNjU0JicyFhUUBiMiJjU0NgEjATMDMhYVFAYjIiY1NDYXIgYVFBYzMjY1NCYJx0hOTkhHTExHutXWubrY1/jFSE5OSEhNTke61dW6utXVAYfdA6XeEbrW1rq619e6R05NSEhMTQJoe3Jze3tzcnuo2L2929u9vNkCOHxyc319c3J8qNm9vdravb3Z+fMGDf0g2L2929u9vNmoe3Jze3tzcnsAAAH+aP/jAu4F8AADAAAHIwEzuOADpuAdBg0AAgA4ApwDLgXfAAMADwAAATADMwMwMxEzFSMVIzUhNQG/7+8S+ImJ5v55BRz+vQIG/fqboqKoAAAB/9n/4wUIBfAAMQAAJQ4BIyAAJyM3My4BNTQ2NyM3MzYAITIWFxEuASMiBgchByEOARUUFhchByEeATMyNjcFCF/PcP76/plL2VhiAQEBAbpYgU0BZQEGcM9fUbhjf7MtAhtW/hMCAQEBAa1Z/tUyr35jtVRSNzgBBfXDDh8cHSAPw/YBAjg3/stOT3t2wxAkJA0fEcN6ek9PAAABAFj/4waIBfAAOgAAATAhFSEGFRQWMzIkNzARBgQjICcmNzAjNTM2NzY3MCE1ITY1NCYjIgYHMBE2JDMgBBUUFTAzFSMGBwYEgQIH/BQGhn58AQuPj/7ijv7TkpQBy/AbKTZe/jgD/gqEimjqe4sBBHsBDgEgy/8RIkwCjMIVGU5UTUz+uzU2eXj2wjgpNSfCFhxQTDg3ATwlJfDfCwvCICNMAAIAGAAABXgF1QADAAsAABMhESEVIREhESERIRgFYPqgBWD+Ev5+/hAF1f7dZP7d/NUDKwACAEAAAAU9BdUABwAdAAABETMyNjQmIwEhIAQQBCEjFSEVIRUhNSM1MzUjETMCoSVwenpw/loBzwEdATH+z/7jTgGt/lP+f+Dg4OAEvv6vT7ROARfV/izWjuXj4+WOARcAAAQARQAACTwF1QAHAB0AJQApAAAAIgYUFjI2NAEhARE0NiEVIgYVESEBERQGITUyNjUAIBYQBiAmEBEhFSEINXI+PnI++EkBrgIfuAEsVyD+Uv3huP7UVyAGrAEqqqr+1qoCfv2CA5ZvznFxzgKu/AACqsiO3T47+4EEAP1WyI7dPjsC18P+rMTEAVT9XsgAAAIBJwOTBlIF1QAMABQAAAEXNzMRIxEDIwMRIxEjFSMRIxEjNQSBd3fjqolMiaxxrqysBdXj4/2+AbX/AAEA/ksCQo/+TQGzjwAAAQDZAgwF2wL4AAMAABMhFSHZBQL6/gL47AAB/W0E7v7YBmYAAwAAASEDI/29ARunxAZm/ogAAvzFBQD/OwX2AAMABwAAATMVIyUzFSP8xevrAYvr6wX29vb2AAAB/W0E7v9OBfYAAwAAASEBI/4zARv+48QF9v74AAAB/KQE7v9cBfgAIwAAAScmJyYjIgYdASM0NjU0NjMyFh8BHgEzMjY1MxQGFRQGIyIm/gI4AwctHCAoiwJrVyVKJzsVJxAlJ4sCa1cmRgUfIwIEGjwyBgUUBWqCGRgnDg88OQYUBWqBFgAAAfyyBO7+kwX2AAMAAAETIwH9zcbE/uMF9v74AQgAAfyHBO7/eQX2AAYAAAEhEyMnByP9ZgE037LHx7IF9v74oaEAAfyHBO7/eQX2AAYAAAEDMxc3MwP9Zt+yx8ey3wTuAQiiov74AAAB/LAE7v9QBfYADQAAATMeATMyNjczDgEjIib8sI8VYExMYBWPEKyUlKwF9j08PD2Bh4cAAf13BQD+iQX2AAMAAAEhFSH9dwES/u4F9vYAAAL8oATu//gF9gADAAcAAAEhASMDIQEj/t0BG/7jxLEBG/7jxAX2/vgBCP74AAABAAAB0gNOACsAeAAMAAEAAAAAAAAAAAAAAAAACAAEAAAAAAAAABkALQBpALoBBwFWAWQBgQGeAcQB3QHuAfwCCgIYAkgCYQKNAsoC6QMaA1kDbQO3A/UECgQiBDcESgReBJYFDAUqBWEFjwW8BdYF7QYhBjsGSQZiBn4GjwauBscG9wccB1EHgwfDB9cH+ggPCDEIUghqCIIIlQikCLcIzQjaCOoJJQlWCYMJtAnnCggKSQpyCocKpArACs4LCAswC14LjwvAC+AMHgxBDGsMgAyfDL4M3gz2DSgNNg1oDZgNmA2yDfEOHg5oDpcOrA8JDxsPig/JD+kP+RAHEH4QixC6ENoRAxE9EUwRfxGaEagRyRHfEg0SLxI/Ek8SXxKXEqMSrxK7EscS0xMOEzgTRBNQE1wTaBN0E4ATjBOYE6QT2xPnE/MT/xQLFBcUIxRFFI8UmxSnFLMUvxTLFPMVPBVIFVQVYBVsFXgVhBXjFe8V+xYHFhMWHxYrFjcWQxZPFpYWohauFroWxhbSFt4W+hdDF08XWxdnF3MXfxewF7wXyBfUF+AX7Bf4GAQYEBgcGCgYNBhAGEwYWBhkGHAYfBiEGL4YyhjWGOIY7hj6GQYZEhkeGSoZNhlCGU4ZWhlmGXIZfhmKGZYZohmuGdYaCBoUGiAaLBo4GkQaUBpbGmYachqAGowamBqkGrAavBrIGuQa8Br8GwgbFBsgGywbOBtEG2IbfBuIG5MbnxurG7cbwxvPG/8cOBxEHFAcXBxoHHQcgBy5HQ0dGR0kHTAdPB1IHVMdXx1qHXYdgR2NHZgdpB2wHbwdxx3THd8d/R4sHjgeRB5QHlweaB50HoAejB6YHqQesB68Hsge1B7gHuwe+B8EHw8fGx8nHzMfPh9aH3AfeB+KH54fuB/eH/4gLSBDIFEgWiBoIHYggiCOIMcg0yEAIQghECEYISAhVSGGIbQhwCHMIdgh8SH5IiYiLiI+ImYibiKbItki8iL+IxwjOCNAI0gjUCNkI2wjdCN8I58j3iPmI/8kHCQ2JFYkfiSKJLQk3yUbJUYlTiWXJcsl2iYBJgkmNiZnJoAmjCaqJsgm5Sb/JwcnGycjJysnPidGJ5QnnCe2J9In7CgLKC8oOyhkKI8oyCjyKP4pCik8KUgpdSl9KYUpjSmVKckp9ioiKi4qOipGKmAqcyqFKpMqmyqpKrcqxSrSKuQq9SsGKxgrNStSK28rmCu2LCQsMSxNLJss7S0HLTgtgC2lLbItwC3TLeIuFy4mLjguSy5lLnMuigAAAAEAAAACXrh+jnX+Xw889QAfCAAAAAAA4PrROQAAAADg+tE593L8rg/NCWcAAQAIAAIAAAAAAAAEzQBmAskAAAOmAR8EKwDDBrQAiwWRAKAIBABCBvoAewJzAMMDqACwA6gApAQvACkGtADZAwoAbQNSAG8DCgDRAuwAAAWRAGIFkQDnBZEAogWRAIkFkQBcBZEAngWRAH8FkQCJBZEAfQWRAGoDMwDlAzMAgQa0ANkGtADZBrQA2QSkAI0IAACHBjEACgYZALwF3wBmBqQAvAV3ALwFdwC8BpEAZgayALwC+gC8Avr/jQYzALwFGQC8B/YAvAayALwGzQBmBd0AvAbNAGYGKQC8BcMAkwV1AAoGfwC8BjEACgjTAD0GKwAnBcv/7AXNAFwDqACwAuwAAAOoAIsGtADPBAAAAAQAAF4FZgBYBboArAS+AFgFugBcBW0AWAN7ACcFugBcBbIArAK+AKwCvv+8BVIArAK+AKwIVgCqBbIArAV/AFgFugCsBboAXAPyAKwEwwBqA9MAGwWyAKAFNwAfB2QASAUpAB8FNwAZBKgAXAWyAQAC7AEEBbIBAAa0ANkCyQAAA6YBHwWRAK4FkQB9BRcASgWRABkC7AEEBAAADgQAAMUIAAEbBIMAngUrAJ4GtADZA1IAbwgAARsEAADFBAAAsga0ANkDgQBtA4EAWgQAAW0F4wCuBRcAgQMKANEEAAEGA4EAewSDAHUFKwDBCEgAZAhIAGQISABoBKQAjQYxAAoGMQAKBjEACgYxAAoGMQAKBjEACgiuAAAF3wBmBXcAvAV3ALwFdwC8BXcAvAL6ABYC+gC8AvoAAwL6AEEGtAAhBrIAvAbNAGYGzQBmBs0AZgbNAGYGzQBmBrQBAAbNAC0GfwC8Bn8AvAZ/ALwGfwC8Bcv/7AXnALwFwQCsBWYAWAVmAFgFZgBYBWYAWAVmAFgFZgBYCGIAWAS+AFgFbQBYBW0AWAVtAFgFbQBYAr7/1QK+AKwCvv/lAr4AIwV/AFgFsgCsBX8AWAV/AFgFfwBYBX8AWAV/AFgGtADZBX8ATgWyAKAFsgCgBbIAoAWyAKAFNwAZBboArAU3ABkGMQAKBWYAWAYxAAoFZgBYBjEACgVmAFgF3wBmBL4AWAXfAGYEvgBYBd8AZgS+AFgF3wBmBL4AWAakALwFugBcBrQAIQW6AFwFdwC8BW0AWAV3ALwFbQBYBXcAvAVtAFgFdwC8BW0AWAV3ALwFbQBYBpEAZgW6AFwGkQBmBboAXAaRAGYFugBcBpEAZgW6AFwGsgC8BbL/7QfKALwGUgCmAvoAIAK+AAMC+gBBAr4AJAL6ACwCvgAPAvoAvAK+AKwC+gC8Ar4ArAX0ALwFfACsAvr/jQK+/7wGMwC8BVIArAVSAKwFGQC8Ar4ArAUZALwCvgCR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ACIAIgAFACQAJAAGACsAKwAHADAAMAAIADQANAAJADUANQAKADYANgALADcANwAMADgAOAANADkAOQAOADoAOgAPAEIAQgAQAEQARAARAEYARgASAEoASgATAE0ATQAUAFAAUAAVAFMAUwAWAFQAVAAXAFYAVgAYAFcAVwAZAFgAWAAaAFoAWgAbAIAAhAAFAIYAhgAcAIcAhwAGAJIAlgAIAJgAmAAdAJkAnAALAJ0AnQAPAKAAoAAeAKEAoQAQAKIApQAeAKYApgAfAKcApwARAKgAqAAgAKkAqQASAKoAqwAgALIAsgAhALMAswAVALQAtgAhALgAuAAiALkAuQAjALoAugAYALsAvAAjAL0AvQAbAL8AvwAbAMAAwAAkAMEAwQAlAMIAwgAkAMMAwwAlAMQAxAAkAMUAxQAlAMYAxgAGAMcAxwARAMwAzAAGAM0AzQARANMA0wAmANUA1QAmANcA1wAmANkA2QAmANsA2wASAQ0BDQAmAQ8BDwAmAREBEQAmARIBEgAnARMBEwAoARUBFQAWARcBFwApARkBGQAWARoBGgAqARwBHAAqAR4BHgAJAR8BHwAXASABIAAJASEBIQAXASIBIgArASQBJAAsASgBKAAtASkBKQAuASoBKgAtASsBKwAuASwBLAAtAS0BLQAuAS4BLgALAS8BLwAYATABMAAtATEBMQAuATMBMwAuATQBNAAvATYBNgAwATcBNwAxATgBOAAPAbYBtgAyAbcBtwAzAboBugA0AbsBuwA1AAAAAQAAAAoAkgCUABRERkxUAHphcmFiAIRhcm1uAIRicmFpAIRjYW5zAIRjaGVyAIRjeXJsAIRnZW9yAIRncmVrAIRoYW5pAIRoZWJyAIRrYW5hAIRsYW8gAIRsYXRuAIRtYXRoAIRua28gAIRvZ2FtAIRydW5yAIR0Zm5nAIR0aGFpAIQABAAAAAD//wAAAAAAAAAAAAAAAA==";
'''

SCHEDULE_PATH = "lib/schedule.ts"
TICK_PATH = "app/api/tpf-tick/route.ts"
CAPS_PACKAGES = {"pdf-lib": "^1.17.1", "@pdf-lib/fontkit": "^1.1.1"}

DOCUMENTS_RULES = (
    "DOCUMENTS AND TABLES: the application makes PDF documents and exchanges tables with Excel. "
    "PDF: `import { buildPdf, pdfResponse, type PdfDoc } from '@/tpf/pdf'` (server only). `const bytes = await "
    "buildPdf({ title, subtitle?, blocks, footer? })`, where blocks are, top to bottom: { kind: 'pairs', rows: "
    "[[label, value], …] } for the details under the title, { kind: 'text', text, bold?, size?, align? }, { kind: "
    "'table', columns: [{ title, width?, align? }], rows: string[][], totals?: [[label, value], …] }, { kind: "
    "'space' } and { kind: 'signatures', left, right }. Every cell is a finished string: format money, dates and "
    "numbers yourself, in the application's language; width is a share (5 for the wide column, 1 for a narrow one), "
    "align 'right' for numbers. The page is A4 portrait; long text wraps and a table continues on the next page "
    "with its heading repeated. Only Latin and Cyrillic text is drawn — anything else comes out as '?'. Serve a "
    "document from a GET route handler next to the record it belongs to (app/estimates/[id]/pdf/route.ts) that "
    "first signs the visitor in and checks that this person may see the record, then `return pdfResponse(bytes, "
    "'Estimate 12.pdf')`; link to it with a plain <a target=\"_blank\">. Do not make PDF in the browser or by hand. "
    "TABLES: `import { toXlsx, toCsv, parseXlsx, parseCsv, fileResponse } from '@/tpf/sheets'` (server only). "
    "Export: a GET route handler under the page it belongs to (app/timesheet/export/route.ts) checks access the "
    "same way the page does, applies the same filters from the URL, and returns `fileResponse(toXlsx([{ name: "
    "'Sheet', rows }]), 'Timesheet.xlsx')`; rows are arrays of cells with the headings first, numbers passed as "
    "numbers so that Excel can add them up (`fileResponse(toCsv(rows), 'x.csv')` only when the spec asks for CSV). "
    "Import: a plain form with <input type=\"file\" name=\"file\" accept=\".xlsx,.csv\" required> and a server "
    "action that checks access, refuses a file over 4 MB, reads the rows with `parseXlsx(await "
    "file.arrayBuffer())` inside try/catch (it throws on a file that is not .xlsx) or `parseCsv(await "
    "file.text())`, takes the first row as headings, validates every row exactly as the manual form does, inserts "
    "the valid rows in one transaction (`sql.begin`), and then tells the user how many rows were added and which "
    "row numbers were skipped and why (the first 20). Cells arrive as text; accept dates written DD.MM.YYYY or "
    "YYYY-MM-DD and say so next to the form. Next to every import form give a template to download: the export "
    "with the headings only. "
)

EMAIL_RULES = (
    "E-MAIL FROM THE APPLICATION: `import { sendMail } from '@/tpf/mail'` (server only): `const sent = await "
    "sendMail({ to, subject, text })` sends one plain-text letter to one address; it never throws and answers "
    "{ ok: true } or { ok: false, error } with 'address' (not an e-mail address), 'limit' (the application may send "
    "200 letters a day, and 10 a day to one address), 'off' or 'failed'. The letter arrives from the application's "
    "name and a reply goes to the owner. Call it from a server action or from scheduled work AFTER the record is "
    "saved, and never let a failed letter fail the action: keep the outcome with the record (a column such as "
    "notified_at, or a letters table with the status) so that the owner sees in /admin what was not delivered. "
    "Write to people who expect it: the staff (their addresses come from directory() or the application's own "
    "tables) and a customer about their own request, booking or document. Never mail an address typed into a "
    "public form anything but the confirmation of that very request, and only when the form has passed its "
    "honeypot and validation; never send the same text to a list of addresses the user pasted, and never more "
    "than 50 letters in one action. No HTML, no attachments, no newsletters or advertising: a letter that carries "
    "a document gives a link to its page in the application. Write every letter in the application's language, "
    "with the application's name in the subject and no secrets, passwords or sign-in links in the text. "
    "notifyOwner stays the way to tell the owner about a new record. "
)

SCHEDULE_RULES = (
    "SCHEDULED WORK: write lib/schedule.ts exporting `async function onTick(): Promise<number>`. The platform "
    "calls it about every 15 minutes (through its own route app/api/tpf-tick, which you do not write); a call can "
    "come late, be skipped or come twice, and has 50 seconds. So work is never timed by the call itself: every job "
    "finds what is due in the database and claims it in the same statement before acting — "
    "`update reminders set sent_at = now() where sent_at is null and due_at <= now() returning …` — and takes at "
    "most 50 items a call (the rest wait for the next). A job that runs once a day keeps its last day in a table "
    "(jobs: name TEXT PRIMARY KEY, last_day DATE) and claims the day the same way: `insert … on conflict (name) do "
    "update set last_day = excluded.last_day where jobs.last_day < excluded.last_day returning 1`. Times of day "
    "are compared in the business's own time zone, written out in SQL (`now() AT TIME ZONE 'Europe/Minsk'`), "
    "never in the server's. onTick wraps each job in its own try/catch, so that one failing job does not stop the "
    "others, and returns how many items it handled. No setTimeout, setInterval, cron packages or background "
    "threads, and nothing is scheduled from a page render. data_model has the columns the jobs claim by (due_at, "
    "sent_at) and the jobs table; the architecture names every job, when it runs and what it does. "
)

AI_RULES = (
    "AI IN THE APPLICATION: `import { askAI } from '@/tpf/ai'` (server only). `const r = await askAI({ prompt, "
    "system?, files?, json?, maxTokens? })` asks a language model and takes several seconds; it never throws and "
    "answers { ok: true, text, data } or { ok: false, error } with 'too-large' (files: photos and PDF only, up to 5 "
    "and 3 MB together), 'limit' (the application's monthly allowance is used up), 'off' or 'failed'. With `json` — "
    "a JSON Schema — the answer comes in `data` in that shape (to read a receipt: { type: 'object', properties: "
    "{ total: { type: 'number' }, date: { type: 'string' }, seller: { type: 'string' } }, required: ['total'] }); "
    "without it, in `text`. Pass the File objects from the form as `files`. Call it only from a server action or a "
    "route handler that a signed-in person started with a button — never while a page renders, never for an "
    "anonymous visitor, and from scheduled work for at most 5 items a call. What the model returns is a "
    "suggestion, never a fact: validate `data` like any other input (types, ranges, lengths), put it into the form "
    "for the person to check and correct before it is saved, and mark on the page that the values were recognised "
    "automatically. Keep the result with the record so that the same file is not sent twice. Every error has its "
    "own plain message, and the feature's absence never blocks the manual way of doing the same thing. Each person "
    "gets a daily limit counted in the database (30 requests unless the spec says otherwise). An assistant chat "
    "keeps its messages in a table and sends the last 20 with each question; its `system` text says what the "
    "application is and what the assistant may talk about, and it is given only the records this person may see. "
    "Never put passwords, keys or other people's personal data into a prompt. "
)

SERVICES_RULES = (
    "OUTSIDE SERVICES: the application talks to the services the architecture lists in `services`, with keys the "
    "owner enters on the order page — you never see them. `import { serviceKey, callService } from "
    "'@/tpf/services'` (server only): `const key = await serviceKey('crm_token')` gives the key by its id from the "
    "architecture, or null while the owner has not entered it; `const r = await callService(url, { method?, "
    "headers?, json?, body? })` makes one HTTPS request (15 seconds, never throws) and answers { ok, status, json, "
    "text } with status 0 when the service could not be reached. Put each service in its own file lib/services/"
    "<id>.ts: the base URL as one constant, one small function per call, every path and field name in that file "
    "and nowhere else. You cannot try the service, so use only endpoints and fields you are certain exist in its "
    "public API, keep to the calls the spec names, and send requests only to the hosts in `services`. While a key "
    "is missing the feature shows the owner a plain 'not connected yet' note in /admin and everything else works. "
    "A call never decides whether the application's own action succeeds: save the record first, call the service "
    "after, and keep the outcome with the record (synced_at and a short error text) with a 'send again' button "
    "for the owner. Never show a key, a service's raw answer or its error text to anyone but the owner in /admin, "
    "never log a key, and never pass a key or a full URL from the browser. Requests coming in from a service "
    "(webhooks) are not available. /admin has a page that lists each service, whether its key is entered, what is "
    "sent to it and when it last worked. "
)

CAPS_RULES = {"documents": DOCUMENTS_RULES, "email": EMAIL_RULES, "schedule": SCHEDULE_RULES, "ai": AI_RULES, "integrations": SERVICES_RULES}

# What the reviewer holds the finished code to, per capability (see agents/qa.py).
CAPS_CRITERIA = {
    "documents": [
        "PDF documents are built only with buildPdf from @/tpf/pdf and served by GET route handlers that sign the visitor "
        "in and check that this person may see the record before pdfResponse; money, dates and numbers are formatted in "
        "the application's language",
        "Exports are GET route handlers that check access like their page and return fileResponse from @/tpf/sheets with "
        "numbers passed as numbers; every import checks access and file size, reads with parseXlsx in try/catch or "
        "parseCsv, validates each row like the manual form, inserts in one transaction, reports added and skipped "
        "rows, and offers a template to download",
    ],
    "email": [
        "Letters go only through sendMail from @/tpf/mail, after the record is saved, and a failed letter never fails the "
        "action: its outcome is kept with the record and visible to the owner; no letter goes to an address from a "
        "public form except the confirmation of that request, none carries passwords or sign-in links, and no action "
        "sends more than 50",
    ],
    "schedule": [
        "lib/schedule.ts exports onTick; every job claims what is due in the database in the same statement before "
        "acting, takes at most 50 items, compares times of day in a named time zone, has its own try/catch, and nothing "
        "uses setTimeout, setInterval or the time of the call itself",
    ],
    "ai": [
        "The model is asked only through askAI from @/tpf/ai, from a server action or route handler started by a "
        "signed-in person; its answer is validated, shown for the person to check before saving and marked as "
        "recognised automatically; every askAI error has a message, the manual way always works, and requests are "
        "limited per person per day in the database",
    ],
    "integrations": [
        "Each service in the architecture has lib/services/<id>.ts with its base URL and calls; keys come only from "
        "serviceKey and requests go only through callService to the hosts the architecture lists; a missing key shows "
        "a 'not connected' note and breaks nothing; records are saved before the call and keep its outcome with a way "
        "to send again; keys and raw service answers never reach anyone but the owner",
    ],
}

# What the architecture must contain for each capability (see agents/architect.py).
CAPS_DESIGN = {
    "documents": "a GET route for every PDF document and every export, and for every import the page that holds its form",
    "email": "which events send a letter, to whom, and the column or table that keeps each letter's outcome",
    "schedule": "every job with when it runs and what it does, the columns jobs claim by and the jobs table",
    "ai": "where a person starts each AI action, the table that keeps its result and the per-person daily counter",
    "integrations": "`services` with every outside service, its base URL, its keys and exactly which calls are made, "
                    "the columns that keep each record's sync outcome, and the /admin page that shows the services",
}


# --- Search engines: every product with public pages is built ready for them. The platform owns the plumbing
# (where the site lives, robots.txt, the sitemap, safe structured data); the developer writes lib/seo.ts and
# each page's own title and description. Tested on a static export and on a server build. ---

SEO_LIB = r'''import type { Metadata } from "next";
import { SEO } from "@/lib/seo";

// Where this site lives. Vercel gives the project's own domain (the client's, once it is attached); the copy
// the client reviews before release lives in a "…-preview" project and must never appear in search results.
const host = process.env.VERCEL_PROJECT_PRODUCTION_URL || process.env.VERCEL_URL || "";
export const SITE_URL = host ? `https://${host}` : "http://localhost:3000";
export const INDEXABLE = Boolean(host) && !host.includes("-preview");

const clip = (text: string, max: number) => (text.length > max ? text.slice(0, max - 1).trimEnd() + "…" : text);

/** The whole site's metadata, for app/layout.tsx: `export const metadata = siteMetadata()`. */
export function siteMetadata(extra: Metadata = {}): Metadata {
  return {
    metadataBase: new URL(SITE_URL),
    title: { default: clip(SEO.title, 65), template: `%s — ${SEO.name}` },
    description: clip(SEO.description, 170),
    applicationName: SEO.name,
    alternates: { canonical: "/" },
    robots: INDEXABLE ? { index: true, follow: true } : { index: false, follow: false },
    openGraph: { type: "website", siteName: SEO.name, locale: SEO.locale, url: "/", title: clip(SEO.title, 65), description: clip(SEO.description, 170) },
    twitter: { card: "summary" },
    formatDetection: { telephone: false },
    ...extra,
  };
}

/** One page's metadata: `export const metadata = pageMetadata({ title, description, path: "/menu" })`.
 *  The title is the page's own (the site's name is added to it); the description is one or two sentences. */
export function pageMetadata(page: { title: string; description: string; path: string }): Metadata {
  const title = clip(page.title, 60), description = clip(page.description, 170);
  return { title, description, alternates: { canonical: page.path }, openGraph: { title, description, url: page.path } };
}

/** For a page that is nobody's search result: sign-in, the admin area, an account, an order by its link. */
export const PRIVATE_PAGE: Metadata = { robots: { index: false, follow: false } };
'''

SEO_JSONLD = r'''/** Structured data for search engines (schema.org): `<JsonLd data={{ "@context": "https://schema.org", "@type":
 *  "LocalBusiness", name, telephone, address }} />`. Only facts that are on the page itself. */
export default function JsonLd({ data }: { data: Record<string, unknown> }) {
  return <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(data).replace(/</g, "\\u003c") }} />;
}
'''

SEO_ROBOTS = r'''import type { MetadataRoute } from "next";
import { INDEXABLE, SITE_URL } from "@/tpf/seo";

export const dynamic = "force-static";

export default function robots(): MetadataRoute.Robots {
  if (!INDEXABLE) return { rules: { userAgent: "*", disallow: "/" } };         // the review copy stays out of search
  return { rules: { userAgent: "*", allow: "/", disallow: ["/admin", "/login", "/account", "/api/"] }, sitemap: `${SITE_URL}/sitemap.xml` };
}
'''

SEO_SITEMAP = r'''import type { MetadataRoute } from "next";
import { SEO } from "@/lib/seo";
import { INDEXABLE, SITE_URL } from "@/tpf/seo";

export const dynamic = "force-static";

export default function sitemap(): MetadataRoute.Sitemap {
  if (!INDEXABLE) return [];
  const pages = [...new Set(["/", ...SEO.pages])].filter((p) => /^\/[a-z0-9\-/]*$/.test(p) && !/^\/(admin|login|account|api)(\/|$)/.test(p));
  return pages.map((path) => ({ url: SITE_URL + (path === "/" ? "/" : path.replace(/\/$/, "")), priority: path === "/" ? 1 : 0.7 }));
}
'''

SEO_CONFIG_PATH = "lib/seo.ts"
SEO_FILES = {"tpf/seo.ts": SEO_LIB, "tpf/JsonLd.tsx": SEO_JSONLD, "app/robots.ts": SEO_ROBOTS, "app/sitemap.ts": SEO_SITEMAP}

SEO_RULES = (
    "SEARCH ENGINES: the public pages are built to be found. The platform owns app/robots.ts, app/sitemap.ts, "
    "`@/tpf/seo` and `@/tpf/JsonLd` (do not write them). You write lib/seo.ts: `export const SEO = { name, title, "
    "description, locale, pages }` — name is the business's name as on the site; title is the home page's title, "
    "30–60 characters, saying what the business is and where (\"Кофейня «Зерно» в Минске — кофе и десерты\"); "
    "description is 120–160 characters in plain words; locale is like 'ru_RU' or 'en_US'; pages lists every public "
    "page a stranger should find in search, as paths ('/', '/menu'), without pages that need signing in and "
    "without paths that contain [segments]. In app/layout.tsx: `import { siteMetadata } from '@/tpf/seo'` and "
    "`export const metadata = siteMetadata()` (other metadata the template asks for goes in as its argument: "
    "`siteMetadata({ appleWebApp: …, icons: … })`), and <html lang> is the site's language. Every other public "
    "page: `export const metadata = pageMetadata({ title, description, path })` from '@/tpf/seo' — its own title "
    "of 15–60 characters without the business's name (it is added), its own description of 80–160 characters, "
    "both different from every other page's and written for a person choosing a result, not as a list of "
    "keywords. A page whose content comes from the database uses `export async function generateMetadata` "
    "returning `pageMetadata({...})` built from the record. Every page that is nobody's search result — sign-in, "
    "everything under /admin, an account, a cart, a checkout, an order or document opened by its link — has "
    "`export const metadata = PRIVATE_PAGE` from '@/tpf/seo' (in a client component's page, put it in a layout.tsx "
    "next to it). Each page has exactly one <h1> that says what the page is, headings go h1 → h2 → h3 without "
    "skipping, the main content is in <main>, navigation in <nav>, and links say where they lead (never \"here\" "
    "or \"more\"). Images have alt text that describes them (alt=\"\" only for decoration) and width and height. "
    "On the home page render `<JsonLd data={…} />` from '@/tpf/JsonLd' with schema.org data of the fitting type "
    "(LocalBusiness or a narrower one such as Restaurant, Store, BeautySalon; Organization when there is no place "
    "to visit): name, description, and only those of telephone, email, address, openingHours and priceRange that "
    "the spec's client_content gives — never an invented or placeholder value, no ratings, no reviews. Do not add "
    "a meta keywords tag, hidden text, or text repeated for search engines. "
)

# What the reviewer holds the finished code to (see agents/qa.py).
SEO_CRITERIA = [
    "Search engines: lib/seo.ts exports SEO with a home title of 30–60 characters, a description of 120–160 and every "
    "public page in pages; the layout exports siteMetadata() and sets <html lang>; every other public page has its "
    "own pageMetadata title and description, unlike any other page's; sign-in, admin, account, cart, checkout and "
    "pages opened by a private link export PRIVATE_PAGE",
    "Each page has exactly one h1 and headings in order, content in <main>, descriptive link texts and image alt "
    "texts; the home page has JsonLd whose every value is a fact from the spec's client_content, with no invented "
    "contact details, ratings or reviews; there is no keywords tag or hidden text",
]
