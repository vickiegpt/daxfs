# DAXFS Evaluation Scripts

This directory contains reproducible raw-data runners for the paper's E1-E5
evaluation plan.

The runner is intentionally split into:

- always-available artifact checks: environment capture, userspace tool build,
  module-build preflight, and image-layout footprint data.
- live filesystem checks: correctness, write scaling, cache/read sweep, and
  single-instance overhead. These require a mounted DAXFS instance.

The default command writes raw data under `eval/raw/<timestamp>/`:

```bash
sudo ./eval/run_all.sh
```

To run the live mount-dependent parts, pass a DAXFS mount point:

```bash
sudo ./eval/run_all.sh --mountpoint /mnt/daxfs
```

Useful options:

```bash
sudo ./eval/run_all.sh \
  --mountpoint /mnt/daxfs \
  --baseline-dir /dev/shm/daxfs_baseline_tmpfs \
  --max-workers 8 \
  --max-instances 4
```

## Output

Each run creates:

- `manifest.json`: run id, repo path, commit, and command line.
- `environment.json`: host/kernel/filesystem/tooling snapshot.
- `events.jsonl`: machine-readable pass/fail/skip event stream.
- `logs/`: stdout/stderr from build and helper commands.
- `e1_image_layout.csv`: raw `mkdaxfs` image layout measurements.
- `e1_footprint_model.csv`: per-instance footprint model derived from raw image
  layout.
- `e2_correctness.json` and `e2_correctness_workers.csv`, when a mount is
  available.
- `e3_write_scaling.csv`, when a mount is available.
- `e4_cache_read_sweep.csv`, when a mount is available.
- `e5_single_instance_overhead.csv`, when a mount or baseline directory is
  available.

When the current host is missing a requirement, the runner writes an explicit
`skipped` event with the reason instead of silently omitting the experiment.

## Current Lanxin Notes

On the inspected `lanxin` environment, `/lib/modules/$(uname -r)/build` and
`/dev/dma_heap/system` were absent. That means the kernel module cannot be
rebuilt on the host and the DMA-heap mount path is unavailable.

For the 2026-07-05 run, the live tests were executed with the matching prebuilt
DAXFS module from `/home/ubuntu/daxfs/daxfs/daxfs.ko` and a `lazy_cma` backed
mount:

```bash
sudo insmod /home/ubuntu/lazy_cma/lazy_cma.ko
sudo insmod /home/ubuntu/daxfs/daxfs/daxfs.ko
sudo ./tools/mkdaxfs --empty \
  -D /dev/lazy_cma \
  -m /mnt/probe_nvme0n1p4/daxfs_eval_mount \
  -s 1024M \
  -O 800M \
  -C 4096
```

See `RUN_SUMMARY_20260705.md` for the final raw directories and the cleanup
state. In short, use:

- `raw/20260705_205221` for E1 and E4.
- `raw/20260705_210901` for E2 concurrent correctness; this is a failed result
  with worker-level `ENXIO` rows.
- `raw/20260705_210936` for E3 concurrent write scaling.
- `raw/20260705_205442` for isolated E5 single-instance overhead.
