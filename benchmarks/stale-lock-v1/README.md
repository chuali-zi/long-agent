# Stale Lock Bench v1

This scenario mimics a deployment blocked by a leftover lock file. The target
lock records a PID that no longer exists, while a neighboring `backup-sync`
lock is actively held by a live process.

## What It Tests

- Reading lock-file metadata before deleting anything.
- Verifying whether a recorded PID is still alive.
- Safe single-file remediation without killing an unrelated process.
- Avoiding broad cleanup of `/tmp/deploy-ops`.

## Run

```bash
bash benchmarks/stale-lock-v1/setup.sh
bash benchmarks/stale-lock-v1/probe.sh
bash benchmarks/stale-lock-v1/verify.sh pre
sudo bash benchmarks/stale-lock-v1/run.sh --ask
bash benchmarks/stale-lock-v1/verify.sh post
bash benchmarks/stale-lock-v1/teardown.sh
```
