import { paymentsEnabled } from "@/tpf/payments";
export const dynamic = "force-dynamic";
export default async function P(){ return <main>payments: {String(await paymentsEnabled())}</main> }
