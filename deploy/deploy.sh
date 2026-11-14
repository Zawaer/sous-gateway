#!/bin/sh
# Deploy a git revision of Sous to a Docker host over SSH, the way our demo servers are run.
#
#   SOUS_HOST=root@1.2.3.4 SOUS_DOMAIN=1-2-3-4.sslip.io deploy/deploy.sh [git-ref]   (default: origin/main)
#
# Steps: back up MariaDB on the server -> copy the exact revision (git archive) -> build with
# SOUS_COMMIT so /health reports it -> start behind Caddy -> wait for /health -> run the
# conformance kit against the new deployment.
#
# The server keeps its own .env (secrets) and Docker volumes (signing key, MariaDB, certificates);
# this script never copies or prints them. First time on a new server: create /opt/sous/.env from
# .env.example (Valio key, SOUS_APPROVER_CODE, MARIADB_PASSWORD, SOUS_PUBLIC_BASE_URL=https://$SOUS_DOMAIN).
set -eu
: "${SOUS_HOST:?set SOUS_HOST, e.g. root@1.2.3.4}"
: "${SOUS_DOMAIN:?set SOUS_DOMAIN, e.g. 1-2-3-4.sslip.io}"
DIR="${SOUS_DIR:-/opt/sous}"
REF="${1:-origin/main}"
cd "$(dirname "$0")/.."
git fetch -q origin
SHA=$(git rev-parse --short "$REF^{commit}")
echo "deploying $SHA ($(git log -1 --format=%s "$SHA")) to $SOUS_HOST:$DIR"

r() { ssh "$SOUS_HOST" "$@"; }
r "mkdir -p $DIR /root/backups && test -f $DIR/.env" || { echo "create $DIR/.env on the server first"; exit 1; }

# 1. Backup (skipped on the very first deploy, when MariaDB isn't running yet).
r "cd $DIR && if docker compose ps --status running mariadb 2>/dev/null | grep -q mariadb; then
     f=/root/backups/sous-\$(date -u +%Y%m%dT%H%M%SZ).sql.gz
     docker compose exec -T mariadb sh -c 'mariadb-dump -u valtakirja -p\"\$MARIADB_PASSWORD\" --single-transaction valtakirja' | gzip > \$f
     echo backup: \$f; fi"

# 2. Replace the code, keep server-only files (.env) and volumes.
r "cd $DIR && find . -mindepth 1 -maxdepth 1 ! -name .env -exec rm -rf {} +"
git archive "$SHA" | r "tar -x -C $DIR"

# 3. Build and start.
r "cd $DIR && SOUS_COMMIT=$SHA SOUS_DOMAIN=$SOUS_DOMAIN docker compose -f compose.yaml -f deploy/compose.https.yaml up -d --build --remove-orphans 2>&1 | tail -3"

# 4. Health: wait until the new commit answers.
for i in $(seq 1 30); do
  sleep 5
  if curl -sf "https://$SOUS_DOMAIN/health" | grep -q "\"commit\":\"$SHA\""; then echo "health ok ($SHA)"; break; fi
  [ "$i" = 30 ] && { echo "health check failed"; r "cd $DIR && docker compose logs --tail 40 valtakirja"; exit 1; }
done

# 5. Conformance: 17 checks in a fresh sandbox (at most a few live supplier lookups).
uv run --quiet python scripts/conformance.py --base "https://$SOUS_DOMAIN"
