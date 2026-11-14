# Deploying Sous

How the public demo is run, so anyone can rebuild the same stack. Everything is in this repo except
secrets, which stay in a `.env` file on the server.

## What runs

One Linux VM with Docker. On it, three containers from `compose.yaml` + `deploy/compose.https.yaml`:

| Container | What it does | State it keeps |
|---|---|---|
| `valtakirja` | The gateway (FastAPI: REST, MCP, OAuth, web UIs) | Volume `valtakirja-data`: Valio data files, catalog index, **Ed25519 signing key** |
| `mariadb` | Mandates, quotes, orders, audit log (system-versioned tables) | Volume `mariadb-data` |
| `caddy` | HTTPS with automatic Let's Encrypt certificates | Volume `caddy-data`: certificates |

Optional: `SOUS_VALKEY_URL` (Aiven for Valkey) gives all deployments one shared Valio call budget
and offer cache. Without it each deployment keeps its own limits.

Our demo uses a Verda `CPU.4V.16G` instance (4 vCPU, 16 GB, Ubuntu 24.04 with Docker, about
$0.05/h) in Finland. Prod and staging are two separate VMs with their own database, signing key and
approver code. Hostnames come from [sslip.io](https://sslip.io), which resolves `1-2-3-4.sslip.io`
to `1.2.3.4`, so no domain purchase is needed.

## First deploy on a new server

1. Create the VM with your SSH key; Docker must be installed.
2. On the server, create `/opt/sous/.env` from `.env.example`. At minimum: `VALIO_AIMO_API_KEY`,
   `SOUS_APPROVER_CODE` (random), `MARIADB_PASSWORD` (random),
   `SOUS_PUBLIC_BASE_URL=https://<your-host>`, `SOUS_DEPLOYMENT_ROLE=prod` or `staging`.
3. From your machine:
   ```sh
   SOUS_HOST=root@1.2.3.4 SOUS_DOMAIN=1-2-3-4.sslip.io deploy/deploy.sh
   ```
   The first start downloads Valio's product data (~87 MB) into the data volume, builds the search
   index and seeds the demo mandates.

## Every deploy after that

`deploy/deploy.sh [git-ref]` (default `origin/main`) does, in order:

1. **Back up MariaDB** on the server (`/root/backups/sous-<time>.sql.gz`).
2. Copy the exact revision with `git archive`, keeping `.env` and all volumes.
3. Build with `SOUS_COMMIT=<sha>`, so `GET /health` reports what is running.
4. Wait until `/health` answers with that commit.
5. Run the **conformance kit** (`scripts/conformance.py`, 17 checks: signatures, tampering,
   idempotency, private reads, hard limits, prompt injection, audit chain, ...) against the new
   deployment, in a fresh sandbox.

Our routine: deploy to staging, let QA test it there, then deploy the same commit to prod.

## Never lose these

- The **`valtakirja-data` volume** holds the gateway's Ed25519 signing key. If it's lost, a new key is
  generated and older mandates, quotes and receipts stop verifying against `/.well-known/jwks.json`.
  Back it up once (`/data/gateway_ed25519.pem`), keep it private.
- **Don't rename** the compose service `valtakirja` or the volumes `valtakirja-data` /
  `mariadb-data`: Docker would create new empty volumes (new key, empty database).

## Rollback

Deploy the previous commit with `deploy/deploy.sh <old-sha>`. Schema changes are additive, so an
older build runs on a newer database. If data must go back too, restore the backup taken before the
deploy:

```sh
zcat /root/backups/sous-<time>.sql.gz | docker compose exec -T mariadb sh -c 'mariadb -u valtakirja -p"$MARIADB_PASSWORD" valtakirja'
```

## Settings worth knowing

| Variable | Effect |
|---|---|
| `SOUS_REQUIRE_AGENT_AUTH=1` | Every mandate requires an agent token (prod and staging run with it). |
| `SOUS_SANDBOX_PER_IP_HOUR` | New sandboxes per client IP per hour (default 3). At a venue everyone shares one IP, so we run 60. |
| `SOUS_VALKEY_URL`, `SOUS_DEPLOYMENT_ROLE` | Shared Valio budget across deployments; prod may use the whole window, others half. |
| `SOUS_NTFY_TOPIC` | Push notifications for held and refused orders via ntfy.sh. Treat the topic name as a secret. |
| `SOUS_DEMO_NOW` | Pins "now" for a recorded demo (cut-offs, deadlines). Unset in normal use. |
