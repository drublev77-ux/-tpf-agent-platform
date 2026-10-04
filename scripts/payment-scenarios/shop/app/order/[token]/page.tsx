import { sql } from "@/tpf/db";
export const dynamic = "force-dynamic";
export default async function P({ params }: { params: Promise<{ token: string }> }){ const { token } = await params;
  const rows = await sql`select status from orders where token = ${token}`; return <main>status: {rows[0]?.status ?? "none"}</main> }
