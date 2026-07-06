# DAXFS Raw Evaluation Run Summary

Date: 2026-07-05  
Host: `lanxin` (`ubuntu`, riscv64)  
Kernel: `Linux ubuntu 6.7.9+ #19 SMP Wed May 27 14:40:35 CST 2026 riscv64`  
Repo: `/mnt/probe_nvme0n1p4/699f2705259d246f31782962/daxfs`

## What Was Added

- `eval/daxfs_eval.py`: raw-data runner for E1-E5.
- `eval/run_all.sh`: shell wrapper.
- `eval/README.md`: usage and output contract.
- `eval/RUN_SUMMARY_20260705.md`: this run summary.
- `.gitignore`: ignores generated `tests/test_mmap` and Python `__pycache__/`.

The runner now records worker-level failures for concurrent E2/E3 tests instead
of aborting before raw data is written.

## Host And Mount Setup

The target repo could not rebuild the kernel module because
`/lib/modules/6.7.9+/build` is missing.  The run used the matching prebuilt
module from `/home/ubuntu/daxfs/daxfs/daxfs.ko`; key source files matched the
target repo copy for `include/daxfs_format.h`, `include/daxfs_version.h`,
`tools/mkdaxfs.c`, and `daxfs/super.c`.

The real DAXFS mount was created with `lazy_cma`:

```bash
sudo insmod /home/ubuntu/lazy_cma/lazy_cma.ko
sudo insmod /home/ubuntu/daxfs/daxfs/daxfs.ko
sudo /mnt/probe_nvme0n1p4/699f2705259d246f31782962/daxfs/tools/mkdaxfs \
  --empty \
  -D /dev/lazy_cma \
  -m /mnt/probe_nvme0n1p4/daxfs_eval_mount \
  -s 1024M \
  -O 800M \
  -C 4096
```

The mount appeared as:

```text
none on /mnt/probe_nvme0n1p4/daxfs_eval_mount type daxfs (rw,relatime,phys=0x21179200000,size=1073741824,overlay)
```

Note: `stat -f -c %T` reports this filesystem as `UNKNOWN (0x64617835)`, so the
runner also checks `/proc/self/mountinfo` for the `daxfs` type.

Cleanup was completed after the final run:

- `/mnt/probe_nvme0n1p4/daxfs_eval_mount` was unmounted.
- `lazy_cma` allocation `0x21179200000` was freed.
- `daxfs` and `lazy_cma` modules were removed.

## Final Raw Data To Cite

Use these directories as the current raw data set:

| phase | raw directory | status | note |
| --- | --- | --- | --- |
| E1 image footprint | `eval/raw/20260705_205221` | ok | image layout and footprint model |
| E2 concurrent correctness | `eval/raw/20260705_210901` | failed | real concurrent small-file failure, recorded as raw data |
| E3 concurrent write scaling | `eval/raw/20260705_210936` | ok | ProcessPoolExecutor concurrent writers |
| E4 read/cache sweep proxy | `eval/raw/20260705_205221` | ok | read-throughput proxy, not true hit counters |
| E5 single-instance overhead | `eval/raw/20260705_205442` | ok | fresh isolated DAXFS mount plus tmpfs/ext baseline |

Earlier directories are retained for audit but should not be used as final
numbers:

- `eval/raw/20260705_195522`: no DAXFS mount; E2-E4 skipped.
- `eval/raw/20260705_204802`: DAXFS type was misdetected by `stat -f`.
- `eval/raw/20260705_204853`: mount detection fixed, but before final cleanup.
- `eval/raw/20260705_205221`: final for E1/E4, but its E2/E3 rows came from the
  older non-worker-failure-aware runner.
- `eval/raw/20260705_205749`: first concurrent E2 failure; crashed before the
  patch that writes failure rows.

## E1 Image Footprint

Source fixture:

- Apparent source bytes: `33,554,482`
- Regular source files: `10`
- Inode count in static image: `15`

Generated image layouts from `eval/raw/20260705_205221/e1_image_layout.csv`:

| label | total bytes | base bytes | overlay bytes | pcache bytes |
| --- | ---: | ---: | ---: | ---: |
| `static` | `33,587,200` | `33,583,104` | `0` | `0` |
| `static_overlay_requested_file_output` | `33,587,200` | `33,583,104` | `0` | `0` |
| `empty_overlay16m_pcache16` | `17,907,712` | `0` | `17,829,888` | `73,728` |

Finding: `mkdaxfs -d SOURCE -o image -O 16M -B 65536` still produced a static
image with no overlay region in pure file-output mode.  The code path for
regular file output calls `write_static_image()` unless `--empty` is used.  This
is a tool/documentation mismatch and should not be cited as writable-overlay
evidence.

## E2 Concurrent Correctness

Raw files:

- `eval/raw/20260705_210901/e2_correctness.json`
- `eval/raw/20260705_210901/e2_correctness_workers.csv`
- `eval/raw/20260705_210901/events.jsonl`

Result:

- Workers: `4`
- Requested operations: `512`
- Completed operations: `314`
- Mismatches: `0`
- Worker errors: `2`
- Wall time: `0.03939080238342285` seconds
- Event status: `failed`

Worker details:

| worker | completed ops | requested ops | mismatch | error |
| ---: | ---: | ---: | ---: | --- |
| 0 | 128 | 128 | 0 | none |
| 1 | 25 | 128 | 0 | `OSError: [Errno 6] No such device or address` at `worker_01/f0025.dat` |
| 2 | 128 | 128 | 0 | none |
| 3 | 33 | 128 | 0 | `OSError: [Errno 6] No such device or address` at `worker_03/f0033.dat` |

Interpretation: the small-file concurrent path is not correct/stable on this
mount.  The failure occurs while creating/writing files and is not a data
mismatch after successful writes.

## E3 Concurrent Write Scaling

Raw file:

- `eval/raw/20260705_210936/e3_write_scaling.csv`

Result:

| workers | status | total bytes | elapsed seconds | throughput MiB/s |
| ---: | --- | ---: | ---: | ---: |
| 1 | ok | `16,777,216` | `0.029837846755981445` | `536.23` |
| 2 | ok | `33,554,432` | `0.07601141929626465` | `420.99` |
| 4 | ok | `67,108,864` | `0.1714632511138916` | `373.26` |

Interpretation: large concurrent file writes completed without worker errors,
but aggregate throughput dropped as worker count increased.

## E4 Read/Cache Sweep Proxy

Raw file:

- `eval/raw/20260705_205221/e4_cache_read_sweep.csv`

This is a read-throughput proxy.  It is not a true cache-hit-rate measurement,
because the DAXFS module currently does not expose inspectable per-cache hit/miss
counters through this harness.

| working set ratio | pass 1 MiB/s | pass 2 MiB/s |
| ---: | ---: | ---: |
| 0.5 | `733.62` | `935.55` |
| 1.0 | `873.47` | `910.30` |
| 2.0 | `809.37` | `827.12` |
| 4.0 | `739.25` | `749.21` |

## E5 Single-Instance Overhead

Raw file:

- `eval/raw/20260705_205442/e5_single_instance_overhead.csv`

This was rerun on a fresh isolated DAXFS mount, so it is the clean E5 result.

| target | operation | bytes/ops | elapsed seconds | throughput MiB/s |
| --- | --- | ---: | ---: | ---: |
| `daxfs` | `seq_write` | `33,554,432` bytes | `0.013749122619628906` | `2327.42` |
| `daxfs` | `seq_read` | `33,554,432` bytes | `0.02382826805114746` | `1342.94` |
| `daxfs` | `create_stat` | `1000` ops | `0.2201981544494629` | N/A |
| `baseline_0_tmpfs` | `seq_write` | `33,554,432` bytes | `0.02393364906311035` | `1337.03` |
| `baseline_0_tmpfs` | `seq_read` | `33,554,432` bytes | `0.021979093551635742` | `1455.93` |
| `baseline_0_tmpfs` | `create_stat` | `1000` ops | `0.08504581451416016` | N/A |
| `baseline_1_ext2/ext3` | `seq_write` | `33,554,432` bytes | `0.039949655532836914` | `801.01` |
| `baseline_1_ext2/ext3` | `seq_read` | `33,554,432` bytes | `0.017242908477783203` | `1855.84` |
| `baseline_1_ext2/ext3` | `create_stat` | `1000` ops | `0.1401689052581787` | N/A |

## Current Evidence Boundary

What this run now supports:

- E1 has raw image-layout measurements.
- E2 has a real concurrent failure with worker-level raw data.
- E3 has a real concurrent write-scaling run.
- E4 has a repeat-read throughput proxy.
- E5 has an isolated DAXFS-vs-baseline raw CSV.

What still needs more work before paper-quality claims:

- E2 needs kernel-level diagnosis for the concurrent `ENXIO`.
- E4 needs real cache hit/miss counters rather than read-throughput proxy data.
- E1 needs the `mkdaxfs` file-output overlay behavior fixed or documented.
- The host still cannot rebuild `daxfs.ko` locally without the matching kernel
  build tree.
