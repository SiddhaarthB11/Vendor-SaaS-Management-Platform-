-- Remove seat-tracking columns: seats_total from subscriptions, seat_reference from licences.
ALTER TABLE slmct.subscriptions DROP COLUMN IF EXISTS seats_total;
ALTER TABLE slmct.licences DROP COLUMN IF EXISTS seat_reference;
