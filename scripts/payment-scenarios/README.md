# Payment scenarios for built shops

Checks the payment code the platform puts into every shop (`tpf/payments.ts` and its routes) against a
stand-in for YooKassa and bePaid that answers the way their documentation describes.

It covers what unit tests cannot: a forged notification, a customer returning before paying, a provider that
reports another amount or is down, a test payment against a live shop, a repeated notification, a return
address pointing to another site, and the platform's own payment check.

To run it you need Node, a local Postgres on `/tmp:5433` and a built sample shop:

1. `python -c "from pathlib import Path; from tpf.tools.template import materialize; import json; root=Path('/tmp/pay-shop'); files=[{'path': str(p.relative_to('scripts/payment-scenarios/shop')), 'content': p.read_text()} for p in Path('scripts/payment-scenarios/shop').rglob('*') if p.is_file()]; materialize(root, files, '', 'shop')"`
2. `cd /tmp/pay-shop && npm install && npm run build`
3. `bash <repo>/scripts/payment-scenarios/run.sh yookassa` (also `bepaid-test`, `bepaid-live`), from `/tmp/pay-shop`

Every line of the output should end the way its description says: `pending` until the provider itself
confirms the exact payment, `paid` after, `/` for a foreign return address.

Stripe is not covered here: it was checked with a real test-mode payment (card 4242…) on a released shop.
