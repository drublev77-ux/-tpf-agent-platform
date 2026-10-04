import { sql } from "@/tpf/db";
import { notifyOwner } from "@/tpf/notify";
import type { Payment } from "@/tpf/payments";
export async function onPaid(reference: string, payment: Payment): Promise<void> {
  const rows = await sql`update orders set status = 'paid' where token = ${reference} and status = 'pending'
    and total = ${payment.amountTotal} and currency = ${payment.currency.toLowerCase()} returning id`;
  if (rows.length) await notifyOwner("Оплачен заказ", `Заказ ${rows[0].id}`);
}
export async function createCheckOrder(): Promise<{ reference: string; amountTotal: number; currency: string }> {
  const reference = crypto.randomUUID();
  await sql`insert into orders (token, total, currency) values (${reference}, 1250, 'rub')`;
  return { reference, amountTotal: 1250, currency: "rub" };
}
export async function isPaid(reference: string): Promise<boolean> {
  return (await sql`select 1 from orders where token = ${reference} and status = 'paid'`).length > 0;
}
export async function removeCheckOrder(reference: string): Promise<void> {
  await sql`delete from orders where token = ${reference}`;
}
