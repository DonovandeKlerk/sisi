# Turso cutover plan (SQLite file → Turso Cloud)

## Why
Modal Volumes background-commit every few seconds with file-level
last-writer-wins. Five function types (web, message workers, 2 crons,
media sender) write the same `sisi.db`; divergent views overwrite each
other. On 2026-10-30 this silently dropped a confirmed intake. Turso
(Turso engine, MVCC) gives real transactions across containers with the
same SQL dialect. Backend switches on two env vars; no SQL changes.

## Prereqs (owner, ~10 min)
1. Sign up at the Turso dashboard (or `brew install
   tursodatabase/tap/turso` then `turso auth login`). Confirm current
   pricing/limits on Turso's pricing page; free tier historically
   covers this workload many times over: our DB is < 1 MB.
2. Create database `sisi-prod` in region `iad` (same region as Modal).
3. Create a token for it — keep this private; it goes into Modal
   secrets, never into chat or Git. Copy the database URL too
   (`libsql://...`; the URL is not secret, the token is).

## Maintenance window (owner approves the time; ~15 min)
Inbound WhatsApp traffic cannot be paused, so the cutover is
backup → import → verify → flip → reconcile. Scheduled writers keep
running; their window writes are covered by step 6.

1. **Consistent backup.** Pick a running container id from
   `modal container list`, then run the SQLite backup API inside it
   (safe under concurrent writers; `<id>` and `<ts>` substituted):
   `modal container exec <id> -- python3 -c "import sqlite3;
   s=sqlite3.connect('file:/data/sisi.db?mode=ro', uri=True);
   d=sqlite3.connect('/data/backup-<ts>.db'); s.backup(d)"`
   then `modal volume get sisi-data /backup-<ts>.db /tmp/cutover.db`.
   Record per-table counts (`scripts/turso_verify.py --db /tmp/cutover.db`).
2. **Import (server-side; token never leaves Modal).**
   `sqlite3 /tmp/cutover.db .dump > /tmp/cutover.sql`, then
   `modal volume put sisi-data /tmp/cutover.sql /cutover.sql`, then
   `modal run scripts/turso_import.py --dump /cutover.sql`.
   It refuses non-empty targets, applies statement-by-statement,
   checks foreign keys, and prints per-table counts. (Import logic is
   rehearsed locally against the real backup before every cutover.)
3. **Verify.** `scripts/turso_verify.py --turso` (with the new creds
   exported locally) — every table count + hash must match step 1, and
   the SQL-compat probe must print `ok`. On mismatch: stop, keep serving
   from the file, investigate; nothing has flipped yet.
4. **Live smoke.** `.venv/bin/modal run scripts/turso_smoke.py` — must
   print `wrote: 25 25` and `read back: {'canaries': 50}`. This proves
   cross-container visibility on the real backend.
5. **Flip.** Add `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` to the
   `sisi-env` Modal secret (secret dashboard; existing entries
   untouched), then `modal deploy modal_app.py`, then `/health` 200.
6. **Reconcile the overlap.** Between steps 1 and 5, old containers kept
   writing the file. Diff file vs Turso per-table counts; replay the few
   straggler rows (typically: messages_seen ids, reminder fired flags,
   send pacing) into Turso with plain INSERTs, newest-wins. If a
   customer-facing write landed in the window, confirm it appears in
   Turso before closing the window.
7. **Watch.** Tail logs for 10 min; confirm a live message round-trips
   end to end (its writes must be visible to the next worker).

## Rollback
Set `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` empty in `sisi-env` and
redeploy: the app serves from the volume file again. First re-export
Turso → file so post-cutover writes are not lost:
`turso db dump sisi-prod > /tmp/revert.sql` applied to a fresh local
SQLite file, verified with `scripts/turso_verify.py`, then
`modal volume put` to a NEW path (never overwrite the live file in
place; flip `SISI_DB` to it only after verification).

## Ongoing
- Backups: `turso db dump sisi-prod` nightly (cron) + Turso point-in-time
  restore. Keep a weekly copy off-cloud.
- Never point two backends at live traffic; exactly one of file/Turso
  serves (env switch is atomic per deploy).

## Recovery of the 2026-10-30 intake (separate approval)
Only after cutover is stable: the known values (Albany Superior White
Bread 700g ×1, cost R16.00, sell R22.00) get re-entered by re-sending
the photo (preferred, no privileged writes) or by an approved one-off
script after proving the row absent. Reconciliation report first.
