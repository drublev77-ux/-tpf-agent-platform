import { sql } from "@/tpf/db";
import { createCheckout } from "@/tpf/payments";
export const dynamic = "force-dynamic";
export async function POST(request: Request) {
  const token = crypto.randomUUID();
  const origin = new URL(request.url).origin;
  await sql`insert into orders (token, total, currency) values (${token}, 1250, 'rub')`;
  const c = await createCheckout({ reference: token, currency: "rub", successUrl: `${origin}/order/${token}`, cancelUrl: `${origin}/cart`,
    lines: [{ name: "Кружка", unitAmount: 750, quantity: 1 }, { name: "Доставка", unitAmount: 500, quantity: 1 }] });
  return Response.json({ token, checkout: c });
}
