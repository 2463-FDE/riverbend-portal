# Riverbend Patient Portal — Operations Runbook

Practical "how do I run / fix this" notes for whoever is on call. Stack is Docker
Compose; one stack per clinic region.

## Start / stop

```bash
make up        # docker compose up -d (Postgres seeds on first boot via initdb)
make down      # stop the stack
make logs      # tail all logs
make ps        # service status (docker compose ps)
```

Endpoints once up:
- Portal: http://localhost:3070
- Gateway + OpenAPI docs: http://localhost:8070/docs
- Per-service health: `GET http://localhost:807N/healthz`

## First-boot data

On a fresh volume Postgres runs `db/schema.sql` then `db/seed/seed.sql`
automatically (mounted into `/docker-entrypoint-initdb.d`). To reload demo data
into an already-running DB:

```bash
make seed
```

To regenerate the seed file (deterministic):

```bash
python3 db/seed/generate_seed.py > db/seed/seed.sql
```

## Demo accounts

All seeded users share password `portal123`, role `staff`. Examples:
`frontdesk`, `rdelgado`, `drnguyen`, `roiclerk`, `mokonkwo`.
(Full list: `db/seed/generate_seed.py`.)

## Health checks

```bash
curl -s localhost:8070/healthz        # gateway
for p in 8071 8072 8073 8074 8075 8076; do curl -s localhost:$p/healthz; echo; done
```

A service that won't become healthy is almost always (a) Postgres not ready yet
or (b) bad DB creds in `.env`. Check `make logs`.

## Common incidents

### "Registration spins for 4–5 seconds" (RIV-088)
Expected with the current build: intake verifies eligibility **inline** with a
synchronous, no-timeout payer call. Not a fix target for ops — it's an
architectural issue (see ARCHITECTURE §7).

### "Whole intake screen froze ~20 min" (RIV-141)
The payer/clearinghouse was degraded. Because the eligibility call has no
timeout/circuit breaker and sits on the intake request path, a payer outage
stalls intake. Mitigation today: wait for the payer to recover. Real fix:
make eligibility async + add timeout/breaker.

### "Two confirmations / two people for one slot" (RIV-175)
Double-booking from the check-then-insert race (no UNIQUE on `appointments.slot_id`,
no idempotency). To find duplicates:

```sql
SELECT slot_id, count(*) FROM appointments
WHERE status='confirmed' GROUP BY slot_id HAVING count(*) > 1;
```

Resolve manually (cancel the later row) until the booking path is fixed.

### "Allergy list differs between charts for the same patient" (RIV-160)
Duplicate-patient problem: self-service intake created multiple charts for one
person (no match key), and inbound HL7 AL1/RXA segments are dropped by the
parser. Reconcile charts manually; do not assume one chart is complete.

### DB connection errors after a restart
Postgres healthcheck gates the app services, but if you `down -v` you wipe the
volume and lose data; next `up` re-seeds from scratch.

## Backups (current state)

There is **no automated backup/restore job** configured. For ad-hoc:

```bash
docker compose exec -T postgres pg_dump -U riverbend_app riverbend > backup.sql
```

This is a known gap (HIPAA contingency / data-backup plan) — flagged for the
next team.

## Logs & PHI warning

`logs/intake-service.log` currently contains full request bodies **including
PHI** (name/DOB/SSN). Treat the logs directory as sensitive; do not copy it off
the host. Removing PHI from logs is an open remediation item.

## CI

`.github/workflows/ci.yml`: frontend build, per-service import smoke, unit tests
(`pytest -m "not integration"`), then `docker compose build`. There is no
secret-scan, dependency-vuln-scan, or image-scan step — another known gap.

## Chroma (knowledge index) — backup, restore, rebuild

The Chroma volume holds **derived data**. Every chunk in it is reproducible from
`db/seed/*.csv` and the clinic knowledge documents in
`services/ai-orchestrator/corpus.py`. That is the property that makes the
following procedures cheap, and it is a gate on the W2 PR (adr/0006): a store we
cannot rebuild is a store we cannot lose.

### Rebuild from source (the normal recovery path)

```bash
curl -sX POST localhost:8070/ai/knowledge/seed \
     -H "Authorization: Bearer $TOKEN"     # requires the knowledge_admin capability
```

Takes seconds on the sampled corpus. Prefer this to restoring a backup — it
cannot restore stale or corrupted vectors.

### Back up the volume

```bash
docker compose stop chroma
docker run --rm -v riverbend-portal_chroma-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/chroma-$(date +%F).tar.gz -C /data .
docker compose start chroma
```

### Restore

```bash
docker compose stop chroma
docker run --rm -v riverbend-portal_chroma-data:/data -v "$PWD":/backup \
  alpine sh -c "rm -rf /data/* && tar xzf /backup/chroma-YYYY-MM-DD.tar.gz -C /data"
docker compose start chroma
```

### ⚠ The `riverbend_records` collection holds PHI

It is a PHI store and is governed as one. A Chroma backup tarball is therefore a
PHI artifact: it needs the same handling, storage location, and retention
decision as a database dump. Do **not** leave one in the repo directory, which is
where the command above writes it — move it to wherever database backups already
go.

Queries against that collection require an authorized patient scope; the adapter
raises rather than serving an unscoped similarity search (`index_port.ScopeRequired`).
