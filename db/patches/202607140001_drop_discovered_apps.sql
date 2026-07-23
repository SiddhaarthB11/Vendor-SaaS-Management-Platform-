-- Remove the SaaS Discovery (shadow app) feature: the discovered_apps table and
-- its endpoints were removed from the application. Safe on fresh installs where
-- the table was never created.
DROP TABLE IF EXISTS slmct.discovered_apps;
