-- Shops can take payments through Stripe, YooKassa (Russia) or bePaid (Belarus). YooKassa and bePaid
-- identify the owner's account by a shop id next to the secret key.
ALTER TABLE payment_connections
    ADD COLUMN IF NOT EXISTS provider   TEXT NOT NULL DEFAULT 'stripe' CHECK (provider IN ('stripe', 'yookassa', 'bepaid')),
    ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT '';
