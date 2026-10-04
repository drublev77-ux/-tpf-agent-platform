
// Stands in for YooKassa and bePaid: answers the way their documentation says, with payment states read from a file.
const fs = require("fs");
const real = globalThis.fetch;
const STATE = "/tmp/paymock.json", LOG = "/tmp/paymock.log";
const state = () => { try { return JSON.parse(fs.readFileSync(STATE, "utf8")); } catch { return {}; } };
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
let n = 0;
globalThis.fetch = async (input, init = {}) => {
  const url = String(typeof input === "string" ? input : input.url ?? input);
  if (!/api\.yookassa\.ru|checkout\.bepaid\.by/.test(url)) return real(input, init);
  const method = (init.method ?? "GET").toUpperCase();
  const body = init.body ? JSON.parse(String(init.body)) : null;
  const auth = String(new Headers(init.headers).get("authorization") ?? "");
  fs.appendFileSync(LOG, JSON.stringify({ method, url, auth: Buffer.from(auth.replace("Basic ", ""), "base64").toString(), body, idem: new Headers(init.headers).get("idempotence-key") ? "yes" : "no" }) + "\n");
  const s = state();
  if (s.down) return json(503, {});
  if (url.includes("api.yookassa.ru")) {
    if (method === "POST") { const id = "yk-" + Date.now() + "-" + (++n); fs.writeFileSync(STATE, JSON.stringify({ ...s, [id]: { create: body } }));
      return json(200, { id, status: "pending", paid: false, amount: body.amount, confirmation: { type: "redirect", confirmation_url: "https://yoomoney.test/pay?orderId=" + id }, metadata: body.metadata, test: true }); }
    const id = decodeURIComponent(url.split("/payments/")[1]); const p = s[id]; if (!p) return json(404, { type: "error", code: "not_found" });
    return json(200, { id, status: p.status ?? "pending", paid: p.status === "succeeded", amount: p.amount ?? p.create.amount, metadata: p.metadata ?? p.create.metadata, test: true });
  }
  if (method === "POST") { const token = "bp" + Date.now() + (++n); fs.writeFileSync(STATE, JSON.stringify({ ...s, [token]: { create: body.checkout } }));
    return json(201, { checkout: { token, redirect_url: "https://checkout.bepaid.test/v2/checkout?token=" + token } }); }
  const token = decodeURIComponent(url.split("/checkouts/")[1]); const p = s[token]; if (!p) return json(404, { message: "not found" });
  return json(200, { checkout: { token, shop_id: 1, transaction_type: "payment", order: p.order ?? { currency: p.create.order.currency, amount: p.create.order.amount, description: p.create.order.description },
    finished: p.status === "successful", expired: false, test: p.test ?? p.create.test, status: p.status ?? "incomplete", message: "x" } });
};
