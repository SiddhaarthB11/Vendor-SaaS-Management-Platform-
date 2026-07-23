#!/usr/bin/env bash
set -euo pipefail

DATABASE_URL="${DATABASE_URL:?Set DATABASE_URL before running, for example postgresql://user:password@localhost:5432/slmct}"
PSQL="${PSQL:-psql}"

for patch in db/patches/*.sql; do
  echo "Applying ${patch}"
  "${PSQL}" "${DATABASE_URL}" -v ON_ERROR_STOP=1 -f "${patch}"
done

echo "Database patches applied successfully."
