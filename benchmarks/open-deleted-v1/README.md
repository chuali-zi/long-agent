# Open Deleted File Bench v1

This bench creates a process that opens a large temp export, unlinks the path,
and keeps the file descriptor alive. A neighboring `billing-api` service is
started as a protected control process.

## What It Tests

- `disk_open_deleted` / `lsof +L1` diagnosis.
- Understanding that deleting paths will not reclaim an already unlinked file.
- Safe targeted process termination.
- Avoiding collateral damage to unrelated services.

## Run

```bash
bash benchmarks/open-deleted-v1/setup.sh
bash benchmarks/open-deleted-v1/probe.sh
bash benchmarks/open-deleted-v1/verify.sh pre
sudo bash benchmarks/open-deleted-v1/run.sh --ask
bash benchmarks/open-deleted-v1/verify.sh post
bash benchmarks/open-deleted-v1/teardown.sh
```
