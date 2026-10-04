"""The payment code the platform puts into every shop it builds (TypeScript, owned by the platform).

One interface for the shop's own code — paymentsEnabled(), createCheckout(), onPaid() — over three
providers: Stripe, YooKassa (Russia) and bePaid (Belarus). The money always goes to the shop owner's own
account with the provider; the platform only keeps the owner's keys where the shop can read them.

How a payment is confirmed differs by provider, and the shop's code does not see the difference:
- Stripe signs its events; the webhook route verifies the signature.
- YooKassa and bePaid notifications carry no secret the shop could check, so they are never trusted:
  the shop asks the provider itself whether the payment went through, both when a notification arrives
  and when the customer comes back from the payment page.
"""

SHOP_PAYMENTS = r'''import "server-only";
import { createHmac, randomUUID, timingSafeEqual } from "crypto";
import { sql } from "@/tpf/db";

// The owner's payment keys are kept by the platform in a table this application can only read.
type Provider = "stripe" | "yookassa" | "bepaid";
type Secrets = { provider: Provider | ""; key: string; shopId: string; webhookSecret: string; test: boolean; at: number };
let cached: Secrets | null = null;

async function secrets(): Promise<Secrets> {
  if (cached && Date.now() - cached.at < 60_000) return cached;
  const v = new Map<string, string>();
  try {
    const rows = await sql`select key, value from tpf_secrets
      where key like 'pay_%' or key like 'stripe_%' or key like 'yookassa_%' or key like 'bepaid_%'`;
    for (const r of rows) v.set(String(r.key), String(r.value));
  } catch {
    // the owner has not connected payments yet
  }
  const get = (k: string) => v.get(k) ?? "";
  const named = get("pay_provider");
  const provider: Provider | "" = named === "stripe" || named === "yookassa" || named === "bepaid" ? named
    : get("stripe_secret_key") ? "stripe" : "";
  cached = provider === "yookassa"
    ? { provider, key: get("yookassa_secret_key"), shopId: get("yookassa_shop_id"), webhookSecret: "", test: get("yookassa_secret_key").startsWith("test_"), at: Date.now() }
    : provider === "bepaid"
      ? { provider, key: get("bepaid_secret_key"), shopId: get("bepaid_shop_id"), webhookSecret: "", test: get("bepaid_test") === "1", at: Date.now() }
      : { provider, key: get("stripe_secret_key"), shopId: "", webhookSecret: get("stripe_webhook_secret"), test: !get("stripe_secret_key").includes("_live_"), at: Date.now() };
  return cached;
}

/** True once the owner has connected a payment provider. Until then the shop takes orders without online payment. */
export async function paymentsEnabled(): Promise<boolean> {
  const s = await secrets();
  if (s.provider === "stripe") return Boolean(s.key && s.webhookSecret);
  return Boolean(s.provider && s.key && s.shopId);
}

export type CheckoutLine = { name: string; unitAmount: number; quantity: number };
export type Checkout = { url: string; sessionId: string };
/** What the customer paid: amountTotal in the currency's smallest unit, currency as a lowercase ISO code. */
export type Payment = { sessionId: string; amountTotal: number; currency: string; email: string };

type Options = {
  reference: string; lines: CheckoutLine[]; currency: string; successUrl: string; cancelUrl: string; customerEmail?: string;
};

function basic(s: Secrets): string {
  return "Basic " + Buffer.from(`${s.shopId}:${s.key}`).toString("base64");
}

/** 12345 -> "123.45": YooKassa takes amounts as decimal strings. */
function decimal(minor: number): string {
  return `${Math.floor(minor / 100)}.${String(minor % 100).padStart(2, "0")}`;
}

/** "123.45" -> 12345, without floating point. */
function minor(value: unknown): number {
  const m = /^(\d+)(?:\.(\d{1,2}))?$/.exec(String(value ?? ""));
  return m ? Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0")) : NaN;
}

async function call(what: string, url: string, init: RequestInit): Promise<{ status: number; body: Record<string, unknown> } | null> {
  try {
    const r = await fetch(url, { ...init, signal: AbortSignal.timeout(15000) });
    const body = (await r.json().catch(() => ({}))) as Record<string, unknown>;
    if (!r.ok) console.error(`${what}: ${r.status} ${String((body.description ?? body.message ?? (body.error as { message?: string } | undefined)?.message) ?? "").slice(0, 200)}`);
    return { status: r.status, body };
  } catch (e) {
    console.error(`${what}: ${e instanceof Error ? e.name : "error"}`);
    return null;
  }
}

/** Remember which provider payment belongs to which order, so it can be checked with the provider later. */
async function remember(provider: Provider, paymentId: string, reference: string): Promise<boolean> {
  try {
    await sql`insert into tpf_payments (payment_id, provider, reference) values (${paymentId}, ${provider}, ${reference})
              on conflict (payment_id) do nothing`;
    return true;
  } catch (e) {
    console.error("payment not recorded:", e instanceof Error ? e.message : "error");
    return false;
  }
}

/**
 * Start a card payment and return the page to send the customer to, or null if it could not be started.
 * `reference` is your own order's unguessable token; it comes back in onPaid(). `unitAmount` is an integer in
 * the currency's smallest unit (cents, kopecks). Amounts must come from your database, never from the browser.
 * successUrl and cancelUrl are absolute addresses of this shop.
 */
export async function createCheckout(opts: Options): Promise<Checkout | null> {
  const s = await secrets();
  if (!(await paymentsEnabled()) || opts.lines.length === 0) return null;
  for (const line of opts.lines) {
    if (!Number.isInteger(line.unitAmount) || line.unitAmount < 0 || !Number.isInteger(line.quantity) || line.quantity < 1) {
      throw new Error("createCheckout: unitAmount and quantity must be whole numbers");
    }
  }
  if (s.provider === "stripe") return stripeCheckout(s, opts);

  // YooKassa and bePaid: one amount for the whole order. The customer returns through the platform's own
  // route, which asks the provider whether the payment went through before showing your success page.
  let success: URL;
  try {
    success = new URL(opts.successUrl);
  } catch {
    throw new Error("createCheckout: successUrl must be an absolute address");
  }
  const total = opts.lines.reduce((sum, l) => sum + l.unitAmount * l.quantity, 0);
  if (total <= 0) return null;
  const back = `${success.origin}/api/tpf-pay-return?ref=${encodeURIComponent(opts.reference)}&to=${encodeURIComponent(success.pathname + success.search)}`;
  const description = opts.lines.map((l) => (l.quantity > 1 ? `${l.name} × ${l.quantity}` : l.name)).join(", ").slice(0, 120);
  const currency = opts.currency.toUpperCase();

  if (s.provider === "yookassa") {
    const r = await call("yookassa payment", "https://api.yookassa.ru/v3/payments", {
      method: "POST",
      headers: { authorization: basic(s), "content-type": "application/json", "idempotence-key": randomUUID() },
      body: JSON.stringify({
        amount: { value: decimal(total), currency }, capture: true, description,
        confirmation: { type: "redirect", return_url: back }, metadata: { reference: opts.reference },
      }),
    });
    const id = String(r?.body.id ?? "");
    const url = String((r?.body.confirmation as { confirmation_url?: string } | undefined)?.confirmation_url ?? "");
    if (!r || r.status >= 300 || !id || !url) return null;
    return (await remember("yookassa", id, opts.reference)) ? { url, sessionId: id } : null;
  }

  const r = await call("bepaid checkout", "https://checkout.bepaid.by/ctp/api/checkouts", {
    method: "POST",
    headers: { authorization: basic(s), "content-type": "application/json", accept: "application/json", "x-api-version": "2" },
    body: JSON.stringify({ checkout: {
      test: s.test, transaction_type: "payment", attempts: 3,
      settings: { success_url: back, decline_url: opts.cancelUrl, fail_url: opts.cancelUrl, cancel_url: opts.cancelUrl,
                  notification_url: `${success.origin}/api/tpf-pay` },
      order: { currency, amount: total, description, tracking_id: opts.reference },
      ...(opts.customerEmail ? { customer: { email: opts.customerEmail } } : {}),
    } }),
  });
  const checkout = (r?.body.checkout ?? {}) as { token?: string; redirect_url?: string };
  if (!r || r.status >= 300 || !checkout.token || !checkout.redirect_url) return null;
  return (await remember("bepaid", checkout.token, opts.reference)) ? { url: checkout.redirect_url, sessionId: checkout.token } : null;
}

async function stripeCheckout(s: Secrets, opts: Options): Promise<Checkout | null> {
  const form = new URLSearchParams({
    mode: "payment", client_reference_id: opts.reference, success_url: opts.successUrl, cancel_url: opts.cancelUrl,
    "metadata[reference]": opts.reference,
  });
  if (opts.customerEmail) form.set("customer_email", opts.customerEmail);
  opts.lines.forEach((line, i) => {
    form.set(`line_items[${i}][quantity]`, String(line.quantity));
    form.set(`line_items[${i}][price_data][currency]`, opts.currency.toLowerCase());
    form.set(`line_items[${i}][price_data][unit_amount]`, String(line.unitAmount));
    form.set(`line_items[${i}][price_data][product_data][name]`, line.name.slice(0, 250));
  });
  const r = await call("stripe checkout", "https://api.stripe.com/v1/checkout/sessions", {
    method: "POST", body: form,
    headers: { authorization: `Bearer ${s.key}`, "content-type": "application/x-www-form-urlencoded" },
  });
  const id = String(r?.body.id ?? ""), url = String(r?.body.url ?? "");
  return r && r.status < 300 && id && url ? { url, sessionId: id } : null;
}

/** Ask the provider about one payment. null: not paid. "unknown": the provider did not answer; ask again later. */
async function paidAt(s: Secrets, provider: string, paymentId: string, reference: string): Promise<Payment | null | "unknown"> {
  if (provider !== s.provider) return null;      // made with a provider the owner has since replaced
  if (provider === "yookassa") {
    const r = await call("yookassa status", `https://api.yookassa.ru/v3/payments/${encodeURIComponent(paymentId)}`, { headers: { authorization: basic(s) } });
    if (!r || r.status >= 500) return "unknown";
    const b = r.body, amount = (b.amount ?? {}) as { value?: string; currency?: string };
    if (r.status !== 200 || b.status !== "succeeded" || b.paid !== true) return null;
    if ((b.metadata as { reference?: string } | undefined)?.reference !== reference) return null;
    const total = minor(amount.value);
    return Number.isInteger(total) ? { sessionId: paymentId, amountTotal: total, currency: String(amount.currency ?? "").toLowerCase(), email: "" } : null;
  }
  if (provider === "bepaid") {
    const r = await call("bepaid status", `https://checkout.bepaid.by/ctp/api/checkouts/${encodeURIComponent(paymentId)}`, {
      headers: { authorization: basic(s), accept: "application/json", "x-api-version": "2" } });
    if (!r || r.status >= 500) return "unknown";
    const c = (r.body.checkout ?? {}) as Record<string, unknown>;
    const order = (c.order ?? r.body.order ?? {}) as { amount?: number; currency?: string; tracking_id?: string };
    const test = c.test ?? r.body.test;
    if (r.status !== 200 || c.status !== "successful" || !Number.isInteger(order.amount)) return null;
    if (order.tracking_id !== undefined && order.tracking_id !== reference) return null;
    if (test === true && !s.test) return null;     // a test payment never pays a real order
    return { sessionId: paymentId, amountTotal: Number(order.amount), currency: String(order.currency ?? "").toLowerCase(), email: "" };
  }
  return null;
}

/**
 * Used by the platform's routes: was the order with this reference paid? The answer comes from the provider
 * itself, so neither a notification nor an address can fake a payment. "unknown" means the provider could
 * not be reached and the question should be asked again.
 */
export async function settleReference(reference: string): Promise<Payment | null | "unknown"> {
  if (!reference || reference.length > 200) return null;
  const s = await secrets();
  let rows: { payment_id: string; provider: string }[];
  try {
    rows = await sql`select payment_id, provider from tpf_payments where reference = ${reference} order by created_at desc limit 5`;
  } catch {
    return null;
  }
  let unknown = false;
  for (const row of rows) {
    const result = await paidAt(s, String(row.provider), String(row.payment_id), reference);
    if (result === "unknown") unknown = true;
    else if (result) return result;
  }
  return unknown ? "unknown" : null;
}

/** Used by the platform's notification route: which order a YooKassa or bePaid notification is about. */
export function notificationReference(body: unknown): string {
  const b = (body ?? {}) as { object?: { metadata?: { reference?: unknown } }; transaction?: { tracking_id?: unknown } };
  const ref = b.object?.metadata?.reference ?? b.transaction?.tracking_id;
  return typeof ref === "string" ? ref : "";
}

/** Used by the platform's Stripe webhook route: the event, if Stripe really sent it. */
export async function verifyStripeEvent(payload: string, header: string): Promise<Record<string, unknown> | null> {
  const { webhookSecret } = await secrets();
  if (!webhookSecret) return null;
  const parts = new Map(header.split(",").map((p) => p.split("=") as [string, string]));
  const timestamp = parts.get("t") ?? "";
  const given = header.split(",").filter((p) => p.startsWith("v1=")).map((p) => p.slice(3));
  if (!timestamp || Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) return null;
  const expected = Buffer.from(createHmac("sha256", webhookSecret).update(`${timestamp}.${payload}`).digest("hex"));
  const ok = given.some((sig) => { const g = Buffer.from(sig); return g.length === expected.length && timingSafeEqual(g, expected); });
  if (!ok) return null;
  try {
    return JSON.parse(payload) as Record<string, unknown>;
  } catch {
    return null;
  }
}
'''

# YooKassa and bePaid tell the shop about a payment here. The notification itself proves nothing — anyone can
# send one — so it is only a reason to ask the provider.
SHOP_PAY_NOTIFY = '''import { onPaid } from "@/lib/payments";
import { notificationReference, settleReference } from "@/tpf/payments";

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return new Response("bad request", { status: 400 });
  }
  const reference = notificationReference(body);
  if (!reference) return Response.json({ received: true });
  const payment = await settleReference(reference);
  if (payment === "unknown") return new Response("retry", { status: 503 });   // the provider sends it again
  if (payment) {
    try {
      await onPaid(reference, payment);
    } catch (e) {
      console.error("onPaid failed:", e instanceof Error ? e.message : "error");
      return new Response("retry", { status: 500 });
    }
  }
  return Response.json({ received: true });
}
'''

# The customer comes back from a YooKassa or bePaid payment page through here: the shop asks the provider
# whether the payment went through, records it, and only then shows the shop's own success page.
SHOP_PAY_RETURN = '''import { onPaid } from "@/lib/payments";
import { settleReference } from "@/tpf/payments";

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function GET(request: Request) {
  const here = new URL(request.url);
  const reference = here.searchParams.get("ref") ?? "";
  const to = here.searchParams.get("to") ?? "/";
  const payment = await settleReference(reference);
  if (payment && payment !== "unknown") {
    try {
      await onPaid(reference, payment);
    } catch (e) {
      console.error("onPaid failed:", e instanceof Error ? e.message : "error");   // the notification will try again
    }
  }
  const path = to.startsWith("/") && !to.startsWith("//") && !to.includes("\\\\") ? to : "/";
  return new Response(null, { status: 303, headers: { location: path } });
}
'''

# Before a shop is shown to the client, the platform has it create throwaway orders and "pay" them through its
# own onPaid: the one piece of a shop that nothing else exercises until a real customer pays. The route answers
# only to a token the platform puts into the review build's secrets for the length of the check.
SHOP_PAY_CHECK = '''import { timingSafeEqual } from "crypto";
import { createCheckOrder, isPaid, onPaid, removeCheckOrder } from "@/lib/payments";
import { sql } from "@/tpf/db";
import type { Payment } from "@/tpf/payments";

export const dynamic = "force-dynamic";
export const maxDuration = 60;

type Step = { name: string; ok: boolean; detail?: string };
type CheckOrder = { reference: string; amountTotal: number; currency: string };

export async function POST(request: Request) {
  let token = "";
  try {
    const rows = await sql`select value from tpf_secrets where key = 'check_token'`;
    token = String(rows[0]?.value ?? "");
  } catch {
    // no secrets table: this is not a build under check
  }
  const given = Buffer.from(request.headers.get("x-tpf-check") ?? ""), expected = Buffer.from(token);
  if (token.length < 32 || given.length !== expected.length || !timingSafeEqual(given, expected)) {
    return new Response("not found", { status: 404 });
  }

  const steps: Step[] = [];
  const made: string[] = [];
  const flags = globalThis as { __tpfMuteNotify?: boolean };
  flags.__tpfMuteNotify = true;          // the owner must not get e-mails about orders nobody placed
  const order = async (): Promise<CheckOrder> => {
    const o = await createCheckOrder();
    made.push(o.reference);
    if (!Number.isInteger(o.amountTotal) || o.amountTotal <= 0) throw new Error("createCheckOrder: amountTotal must be a positive integer");
    return o;
  };
  const payment = (o: CheckOrder, over: Partial<Payment> = {}): Payment =>
    ({ sessionId: "check_" + o.reference.slice(0, 12), amountTotal: o.amountTotal, currency: o.currency.toLowerCase(), email: "check@example.com", ...over });
  const step = async (name: string, run: () => Promise<string | null>) => {
    try {
      const problem = await run();
      steps.push(problem ? { name, ok: false, detail: problem } : { name, ok: true });
    } catch (e) {
      steps.push({ name, ok: false, detail: (e instanceof Error ? `threw ${e.name}: ${e.message}` : "threw").slice(0, 600) });
    }
  };
  try {
    await step("a payment of the order's exact total and currency marks it paid, and a repeat changes nothing", async () => {
      const o = await order();
      if (await isPaid(o.reference)) return "isPaid is true before any payment";
      await onPaid(o.reference, payment(o));
      if (!(await isPaid(o.reference))) return "isPaid is false after onPaid with the order's exact total and currency";
      await onPaid(o.reference, payment(o));
      return (await isPaid(o.reference)) ? null : "isPaid became false after onPaid was called a second time";
    });
    await step("a payment of a different amount does not mark the order paid", async () => {
      const o = await order();
      await onPaid(o.reference, payment(o, { amountTotal: o.amountTotal + 1 }));
      return (await isPaid(o.reference)) ? "the order was marked paid by a payment of a different amount" : null;
    });
    await step("a payment in a different currency does not mark the order paid", async () => {
      const o = await order();
      await onPaid(o.reference, payment(o, { currency: o.currency.toLowerCase() === "usd" ? "eur" : "usd" }));
      return (await isPaid(o.reference)) ? "the order was marked paid by a payment in a different currency" : null;
    });
    await step("a payment for an unknown order is ignored without an error", async () => {
      await onPaid("no-such-order-" + Date.now(), { sessionId: "check_none", amountTotal: 100, currency: "usd", email: "" });
      return null;
    });
  } finally {
    for (const reference of made) {
      try {
        await removeCheckOrder(reference);
      } catch (e) {
        steps.push({ name: "removeCheckOrder removes the order made for the check", ok: false, detail: (e instanceof Error ? `threw ${e.name}: ${e.message}` : "threw").slice(0, 600) });
      }
    }
    flags.__tpfMuteNotify = false;
  }
  return Response.json({ ok: steps.every((s) => s.ok), steps });
}
'''

SHOP_PAY_CHECK_PATH = "app/api/tpf-check/route.ts"
SHOP_PAY_NOTIFY_PATH = "app/api/tpf-pay/route.ts"
SHOP_PAY_RETURN_PATH = "app/api/tpf-pay-return/route.ts"

# Hosts the shop's own code must never call: payments go through tpf/payments.ts.
PAYMENT_HOSTS = ("api.stripe.com", "api.yookassa.ru", "checkout.bepaid.by", "gateway.bepaid.by")
