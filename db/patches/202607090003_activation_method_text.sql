-- Change activation_method from enum to text — the enum only had 4 values but the
-- API supports vendor_portal, invoice_po, and auto_provisioned as well. Using text
-- keeps the column flexible without needing enum migrations for future methods.
ALTER TABLE slmct.workflow_requests
    ALTER COLUMN activation_method TYPE text USING activation_method::text;
