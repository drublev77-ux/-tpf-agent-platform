-- The currency a client chose to be invoiced in (EUR or USD); empty means the studio's default.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS currency TEXT;
