"""Platform-owned parts for applications whose people work together: stored files (receipts, documents),
pages that refresh themselves (a chat), and the phone's location sent with a form (clocking in on site)."""

APP_FILES = '''import "server-only";
import { randomBytes } from "crypto";
import { sql } from "@/tpf/db";

export type StoredFile = { id: string; name: string; mime: string; size: number; createdAt: Date };

/** One file may be this large after the browser has shrunk a photo (see FileInput). */
export const MAX_FILE_BYTES = 4_000_000;
const MAX_TOTAL_BYTES = 300_000_000;        // everything this application stores, together

// What a file really is, read from its first bytes: the name and the browser's word for it prove nothing.
function sniff(b: Buffer): string | null {
  if (b.length > 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "image/jpeg";
  if (b.length > 8 && b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "image/png";
  if (b.length > 12 && b.subarray(0, 4).toString("latin1") === "RIFF" && b.subarray(8, 12).toString("latin1") === "WEBP") return "image/webp";
  if (b.length > 5 && b.subarray(0, 5).toString("latin1") === "%PDF-") return "application/pdf";
  return null;
}

function cleanName(name: string): string {
  const base = name.normalize("NFC").replace(/[\\u0000-\\u001f\\u007f"\\\\/:*?<>|]+/g, " ").trim().slice(0, 120);
  return base || "file";
}

/** Store a file from a form field. Photos (JPEG, PNG, WebP) and PDF only, up to MAX_FILE_BYTES.
 *  Returns its id: keep the id in your own table next to the record the file belongs to. */
export async function saveFile(file: File | null, ownerId?: number):
    Promise<{ ok: true; file: StoredFile } | { ok: false; error: "empty" | "too-large" | "type" | "full" }> {
  if (!file || typeof file === "string" || file.size === 0) return { ok: false, error: "empty" };
  if (file.size > MAX_FILE_BYTES) return { ok: false, error: "too-large" };
  const data = Buffer.from(await file.arrayBuffer());
  const mime = sniff(data);
  if (!mime) return { ok: false, error: "type" };
  const used = await sql<{ total: string }[]>`select coalesce(sum(size), 0)::text as total from tpf_files`;
  if (Number(used[0].total) + data.length > MAX_TOTAL_BYTES) return { ok: false, error: "full" };
  const id = randomBytes(16).toString("hex");
  const name = cleanName(file.name);
  const rows = await sql<{ createdAt: Date }[]>`insert into tpf_files (id, owner_id, name, mime, size, data)
    values (${id}, ${ownerId ?? null}, ${name}, ${mime}, ${data.length}, ${data}) returning created_at as "createdAt"`;
  return { ok: true, file: { id, name, mime, size: data.length, createdAt: rows[0].createdAt } };
}

/** Name, type and size of a stored file, or null. */
export async function fileInfo(id: string): Promise<StoredFile | null> {
  if (!/^[0-9a-f]{32}$/.test(id)) return null;
  const rows = await sql<StoredFile[]>`select id, name, mime, size, created_at as "createdAt" from tpf_files where id = ${id}`;
  return rows[0] ?? null;
}

/** The file as an HTTP response, for a route handler. Decide who may see it BEFORE calling this:
 *  it serves the file to whoever asks. */
export async function serveFile(id: string): Promise<Response> {
  if (!/^[0-9a-f]{32}$/.test(id)) return new Response("not found", { status: 404 });
  const rows = await sql<{ name: string; mime: string; data: Buffer }[]>`select name, mime, data from tpf_files where id = ${id}`;
  const file = rows[0];
  if (!file) return new Response("not found", { status: 404 });
  const disposition = file.mime === "application/pdf" ? "attachment" : "inline";
  return new Response(new Uint8Array(file.data), { headers: {
    "content-type": file.mime, "content-length": String(file.data.length),
    "content-disposition": `${disposition}; filename*=UTF-8''${encodeURIComponent(file.name)}`,
    "x-content-type-options": "nosniff", "content-security-policy": "default-src 'none'; sandbox",
    "cache-control": "private, max-age=3600" } });
}

/** Remove a stored file (when its record is deleted or the file is replaced). */
export async function deleteFile(id: string): Promise<void> {
  if (/^[0-9a-f]{32}$/.test(id)) await sql`delete from tpf_files where id = ${id}`;
}
'''

APP_FILE_INPUT = '''"use client";
import { useRef, useState } from "react";

// A file field for photos and PDF. A phone photo is several megabytes; before the form is sent it is redrawn
// at most 2000 px wide as a JPEG, which keeps a receipt readable and fits the upload limit.
const LIMIT = 4_000_000;

async function shrink(file: File): Promise<File> {
  if (!/^image\\/(jpeg|png|webp)$/.test(file.type) || file.size < 900_000) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 2000 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((done) => canvas.toBlob(done, "image/jpeg", 0.82));
    return blob && blob.size < file.size ? new File([blob], file.name.replace(/\\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" }) : file;
  } catch {
    return file;
  }
}

export default function FileInput({ name, required, tooLarge, className, id }: {
  name: string; required?: boolean; className?: string; id?: string;
  /** Shown when the file is still over the limit, in the site's language. */
  tooLarge: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [problem, setProblem] = useState("");
  async function changed() {
    const el = input.current, file = el?.files?.[0];
    if (!el || !file) return;
    const ready = await shrink(file);
    if (ready.size > LIMIT) {
      el.value = "";
      setProblem(tooLarge);
      return;
    }
    setProblem("");
    if (ready !== file) {
      const list = new DataTransfer();
      list.items.add(ready);
      el.files = list.files;
    }
  }
  return (
    <>
      <input ref={input} id={id} className={className} type="file" name={name} required={required}
        accept="image/jpeg,image/png,image/webp,application/pdf" onChange={changed} />
      {problem ? <p role="alert">{problem}</p> : null}
    </>
  );
}
'''

APP_AUTO_REFRESH = '''"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

/** Re-reads the page's data from the server every few seconds while the tab is visible — how a chat or a
 *  shared list shows what other people have just added. Renders nothing. */
export default function AutoRefresh({ seconds = 8 }: { seconds?: number }) {
  const router = useRouter();
  useEffect(() => {
    const every = Math.max(4, seconds) * 1000;
    const tick = () => { if (document.visibilityState === "visible") router.refresh(); };
    const timer = setInterval(tick, every);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", tick); };
  }, [router, seconds]);
  return null;
}
'''

APP_GEO_FORM = '''"use client";
import { useRef, useState, type ReactNode } from "react";

/** A form that sends the phone's location with it: fields `lat`, `lng` and `accuracy` (metres) are added
 *  when the button is pressed, then the server action runs. Texts come from the page, in its language. */
export default function GeoForm({ action, children, button, locating, denied, className, buttonClassName }: {
  action: (form: FormData) => void | Promise<void>;
  children?: ReactNode;                 // hidden fields and anything else the action needs
  button: string;                       // the button's label
  locating: string;                     // shown while the phone finds its position
  denied: string;                       // shown when location is off or refused
  className?: string; buttonClassName?: string;
}) {
  const form = useRef<HTMLFormElement>(null);
  const [state, setState] = useState<"idle" | "locating" | "denied">("idle");
  const located = useRef(false);
  function submit(event: React.FormEvent<HTMLFormElement>) {
    if (located.current) {              // second pass: the fields are filled, let the action run
      located.current = false;
      return;
    }
    event.preventDefault();
    if (!("geolocation" in navigator)) {
      setState("denied");
      return;
    }
    setState("locating");
    navigator.geolocation.getCurrentPosition((position) => {
      const el = form.current;
      if (!el) return;
      const set = (name: string, value: number) => {
        let field = el.querySelector<HTMLInputElement>(`input[name="${name}"]`);
        if (!field) {
          field = document.createElement("input");
          field.type = "hidden";
          field.name = name;
          el.appendChild(field);
        }
        field.value = String(value);
      };
      set("lat", position.coords.latitude);
      set("lng", position.coords.longitude);
      set("accuracy", Math.round(position.coords.accuracy));
      setState("idle");
      located.current = true;
      el.requestSubmit();
    }, () => setState("denied"), { enableHighAccuracy: true, timeout: 20000, maximumAge: 30000 });
  }
  return (
    <form ref={form} action={action} onSubmit={submit} className={className}>
      {children}
      <button type="submit" className={buttonClassName} disabled={state === "locating"}>{state === "locating" ? locating : button}</button>
      {state === "denied" ? <p role="alert">{denied}</p> : null}
    </form>
  );
}
'''

APP_GEO = '''export type Position = { lat: number; lng: number; accuracy: number };

/** The location a GeoForm sent, or null when it is missing or not a place on Earth. */
export function readPosition(form: FormData): Position | null {
  const lat = Number(form.get("lat")), lng = Number(form.get("lng")), accuracy = Number(form.get("accuracy") ?? 0);
  if (form.get("lat") === null || form.get("lng") === null) return null;
  if (!Number.isFinite(lat) || !Number.isFinite(lng) || Math.abs(lat) > 90 || Math.abs(lng) > 180) return null;
  if (lat === 0 && lng === 0) return null;
  return { lat, lng, accuracy: Number.isFinite(accuracy) && accuracy >= 0 ? Math.min(Math.round(accuracy), 100000) : 0 };
}

/** Distance in metres between two points on the ground. */
export function distanceMeters(a: { lat: number; lng: number }, b: { lat: number; lng: number }): number {
  const rad = (d: number) => (d * Math.PI) / 180;
  const h = Math.sin(rad(b.lat - a.lat) / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(rad(b.lng - a.lng) / 2) ** 2;
  return Math.round(2 * 6371000 * Math.asin(Math.min(1, Math.sqrt(h))));
}
'''

FILES_PATH = "tpf/files.ts"

UPLOADS_RULES = (
    "FILES: signed-in people (or the owner in /admin) can attach photos and PDF documents — a receipt, an estimate, "
    "a document. Storage belongs to the platform: `import { saveFile, fileInfo, serveFile, deleteFile, MAX_FILE_BYTES, "
    "type StoredFile } from '@/tpf/files'` (server only). In the form use the platform's field "
    "`import FileInput from '@/tpf/FileInput'`: `<FileInput name=\"receipt\" required tooLarge=\"…\" />` inside a plain "
    "<form action={serverAction}> (it shrinks phone photos before sending; one file per field). In the action: "
    "`const saved = await saveFile(form.get('receipt') as File | null, user.id)`; on `{ ok: false, error }` show a "
    "message for 'empty', 'too-large' (over 4 MB), 'type' (only JPEG, PNG, WebP and PDF are accepted) or 'full' "
    "(the application's storage is full); on success keep `saved.file.id` (TEXT) and, if you show it, "
    "`saved.file.name` in your own table next to the record it belongs to. Files are private: write a route handler "
    "app/files/[id]/route.ts whose GET first signs the visitor in (requireUser or requireAdmin), then finds the "
    "record that holds this file id in your tables and checks that this person may see that record, and only then "
    "`return serveFile(id)`; anyone else gets a 404. Link to files as /files/<id>; show images with a plain <img>. "
    "When a record is deleted or its file replaced, call deleteFile(oldId). Never accept a file from a visitor who "
    "is not signed in, never store file bytes in your own tables, and never build a file URL from a name. "
)

CHAT_RULES = (
    "MESSAGES BETWEEN PEOPLE: a chat is a table of messages (id, the chat or project it belongs to, user_id, text, "
    "created_at) written through a server action and read by a server component. The server action starts with "
    "requireUser, checks that this user takes part in that chat or project, trims the text and refuses empty "
    "messages and ones over 2000 characters. The page shows the latest 100 messages oldest first, each with the "
    "author's name from directory() and the time, the reader's own messages set apart, and a form at the bottom; "
    "put `<AutoRefresh seconds={8} />` from '@/tpf/AutoRefresh' on it so new messages appear without reloading. "
    "A person sees only the chats they take part in — that rule is checked in the page AND in the action. To show "
    "unread counts, keep the time each user last opened each chat. There are no push notifications, no typing "
    "indicators and no attachments in messages unless FILES is also available. "
)

GEO_RULES = (
    "LOCATION AND HOURS: a worker clocks in and out on site from the phone. Use the platform's form "
    "`import GeoForm from '@/tpf/GeoForm'`: `<GeoForm action={clockIn} button=\"…\" locating=\"…\" denied=\"…\">hidden "
    "fields</GeoForm>` asks the phone for its position when pressed and adds lat, lng and accuracy to the form; in "
    "the action `const where = readPosition(form)` and `distanceMeters(a, b)` come from '@/tpf/geo'. The action "
    "starts with requireUser, takes the time from the database's now() (never from the browser), stores the "
    "position, its accuracy and the distance to the object, and refuses a second open shift for the same person. "
    "An object's own position is set by a manager standing there with another GeoForm ('remember this place') or "
    "typed as latitude and longitude; without it distance cannot be checked and the shift is recorded as "
    "'place not confirmed'. A clock-in farther than the allowed radius (200 m plus the reported accuracy unless the "
    "spec says otherwise) is still recorded but marked 'away from the object' for the manager to see — do not "
    "block people over a bad GPS fix. Hours are computed from closed shifts in SQL; a manager can correct or close "
    "a shift by hand, and the correction is marked. Location is read only at the moment a button is pressed: there "
    "is no tracking in the background and no map tiles or geocoding service; link an address to "
    "https://www.google.com/maps/search/?api=1&query=<lat>,<lng> when a map is wanted. "
)
