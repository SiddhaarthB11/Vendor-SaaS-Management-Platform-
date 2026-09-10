-- cancel_job() previously only flipped devtools_jobs.status to 'cancelled' in
-- the DB — it never signalled the actual OS process. Autofix jobs run as a
-- detached subprocess (devtools.autofix_subprocess_runner) specifically so
-- they survive uvicorn --reload restarts, which also means nothing else in
-- the API process tree can find them again to stop them. Confirmed live: a
-- job cancelled through the panel kept running as a real, unsupervised
-- process, and later reverted a rejected task with `git clean -fd`, wiping
-- the untracked db/ directory inside the container and crash-looping the API.
-- Storing the PID lets cancel_job() actually terminate the process.

ALTER TABLE slmct.devtools_jobs ADD COLUMN IF NOT EXISTS pid integer;
