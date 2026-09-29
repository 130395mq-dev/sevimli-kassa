# POS pilot rollout

The server defaults to `KASSA_UPDATE_MODE=hold`: it advertises no update URL
and refuses release downloads. This does not disable receipt or catalogue APIs.

To open one version to selected registers, set all three on **hub**:

```
KASSA_UPDATE_MODE=pilot
KASSA_UPDATE_VERSION=1.18.7
KASSA_UPDATE_REGISTER_IDS=<verified Register primary key>
```

Use database primary keys, never a display name or a client-supplied query
parameter. Empty/mistyped mode, missing version or missing IDs remain closed.
Newer releases are not implicitly included. Existing device and token checks
still apply. The 1.18.6 download without X-Device remains supported during the
transition; its bearer token still identifies the selected register.

Deployment order:

1. Run PostgreSQL and Windows checks on the exact release commits.
2. Deploy the server with `hold`; verify ordinary sales/hello/catalogue work.
3. Verify the pilot register ID and the four trading registers. Set `pilot`
   with one verified ID and the exact version. Verify that version and download
   endpoints refuse the trading registers, including a direct download URL.
4. Only then publish the POS release. The existing release fetcher may cache it
   on hub, but caching does not bypass these endpoint gates.
5. Keep the pilot idle for installation. Check the installed version, original
   receipts/outbox, print/scan/sale/return and one complete trading day.
6. Broader promotion needs a separate decision. `all` is explicit and should
   still pin a version; do not clear the pin casually.

Emergency stop: set `KASSA_UPDATE_MODE=hold`. It refuses future requests; it
cannot cancel a download that already completed or an installer already running.
Never roll the server back to a pre-gate version while a newer POS release is
public: such a server would advertise it to every register. Prepare a rollback
build that retains the gate, or remove/deactivate the release and its fetch path
before any legacy rollback. Database rollback is not an application rollback.

# Outbound work

Immediate receipt delivery waits at most four seconds and uses at most four
background workers per web process, without an unbounded pending task queue.
The saved database receipt is the durable queue. Worker saturation or process
restart does not delete it; sync_sales handles the retry. A response may carry
no MoySklad receipt number until delivery completes, as on existing offline paths.

PostgreSQL session advisory locks serialize each receipt across web, sync_sales
and healer. These locks do not expire after 60 seconds while a slow request is
still running. They are released on session/process exit. Catalogue imports
also serialize per entity. SQLite's local fallback does not provide cross-process
protection and is only suitable for development/testing here.

These protections require every active writer to run the updated code. A rolling
deployment temporarily includes old writers, so existing syncId/idempotency and
retry handling remain necessary. No exactly-once guarantee is claimed across
an external HTTP commit and a subsequent database failure.
