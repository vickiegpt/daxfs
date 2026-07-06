#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import time


SUPER_STRUCT = struct.Struct("<IIIIQ QQQIIQ QQII QQII")
DAXFS_MAGIC = 0x64617835


def now_id():
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def repo_root():
    return Path(__file__).resolve().parents[1]


def mkdir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def append_event(outdir, phase, status, **fields):
    event = {
        "ts_unix": time.time(),
        "phase": phase,
        "status": status,
    }
    event.update(fields)
    with (outdir / "events.jsonl").open("a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def run_cmd(outdir, name, cmd, cwd=None, timeout=120, check=False):
    logs = mkdir(outdir / "logs")
    start = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    elapsed = time.time() - start
    (logs / f"{name}.stdout").write_text(proc.stdout)
    (logs / f"{name}.stderr").write_text(proc.stderr)
    result = {
        "cmd": cmd,
        "cwd": str(cwd) if cwd else None,
        "returncode": proc.returncode,
        "elapsed_s": elapsed,
        "stdout_log": str(logs / f"{name}.stdout"),
        "stderr_log": str(logs / f"{name}.stderr"),
    }
    if check and proc.returncode != 0:
        raise RuntimeError(f"{name} failed with rc={proc.returncode}")
    return result


def command_text(cmd):
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=20, check=False
        ).stdout
    except Exception as exc:
        return f"ERROR: {exc}\n"


def git_info(repo):
    env = {}
    for key, cmd in {
        "commit": ["git", "-c", f"safe.directory={repo}", "-C", str(repo), "rev-parse", "HEAD"],
        "short_commit": ["git", "-c", f"safe.directory={repo}", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        "status_short": ["git", "-c", f"safe.directory={repo}", "-C", str(repo), "status", "--short"],
    }.items():
        env[key] = command_text(cmd).strip()
    return env


def capture_environment(repo, outdir):
    tools = {}
    for tool in ["make", "gcc", "cc", "python3", "fio", "perf", "numactl", "taskset"]:
        tools[tool] = shutil.which(tool)
    uname_r = command_text(["uname", "-r"]).strip()
    env = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(repo),
        "git": git_info(repo),
        "id": command_text(["id"]).strip(),
        "uname": command_text(["uname", "-a"]).strip(),
        "lscpu": command_text(["lscpu"]),
        "free_h": command_text(["free", "-h"]),
        "df_repo": command_text(["df", "-h", str(repo)]),
        "mounts_daxfs": command_text(["sh", "-c", "mount | grep daxfs || true"]),
        "kernel_build_dir": f"/lib/modules/{uname_r}/build",
        "kernel_build_dir_exists": Path(f"/lib/modules/{uname_r}/build").exists(),
        "dma_heap_system_exists": Path("/dev/dma_heap/system").exists(),
        "tools": tools,
    }
    write_json(outdir / "environment.json", env)
    append_event(outdir, "environment", "ok",
                 kernel_build_dir_exists=env["kernel_build_dir_exists"],
                 dma_heap_system_exists=env["dma_heap_system_exists"])
    return env


def ensure_userspace_tools(repo, outdir, skip_build=False):
    if skip_build:
        append_event(outdir, "build_userspace", "skipped", reason="--skip-build")
        return
    res = run_cmd(outdir, "build_userspace", ["make", "tools", "tests"], cwd=repo, timeout=180)
    append_event(outdir, "build_userspace",
                 "ok" if res["returncode"] == 0 else "failed", **res)


def module_build_preflight(repo, outdir, skip_build=False):
    if skip_build:
        append_event(outdir, "build_module", "skipped", reason="--skip-build")
        return
    uname_r = command_text(["uname", "-r"]).strip()
    kdir = Path(f"/lib/modules/{uname_r}/build")
    if not kdir.exists():
        append_event(outdir, "build_module", "skipped",
                     reason="missing_kernel_build_dir", kdir=str(kdir))
        return
    res = run_cmd(outdir, "build_module", ["make", f"KDIR={kdir}"], cwd=repo / "daxfs", timeout=240)
    append_event(outdir, "build_module",
                 "ok" if res["returncode"] == 0 else "failed", **res)


def write_pattern_file(path, size, seed):
    block = hashlib.sha256(seed.encode()).digest()
    with path.open("wb") as f:
        remaining = size
        while remaining > 0:
            chunk = (block * min(4096, (remaining + len(block) - 1) // len(block)))[: min(4096, remaining)]
            f.write(chunk)
            remaining -= len(chunk)


def make_source_tree(src, file_count, file_size):
    if src.exists():
        shutil.rmtree(src)
    mkdir(src)
    mkdir(src / "bin")
    mkdir(src / "lib")
    mkdir(src / "etc")
    (src / "etc" / "config.txt").write_text("daxfs evaluation fixture\n")
    for i in range(file_count):
        sub = "bin" if i % 2 == 0 else "lib"
        write_pattern_file(src / sub / f"payload_{i:03d}.bin", file_size, f"daxfs-{i}")
    try:
        os.symlink("../etc/config.txt", src / "bin" / "config.link")
    except FileExistsError:
        pass
    try:
        os.link(src / "etc" / "config.txt", src / "etc" / "config.hardlink")
    except OSError:
        pass


def apparent_tree_bytes(src):
    total = 0
    files = 0
    for path in src.rglob("*"):
        try:
            st = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
            files += 1
    return total, files


def parse_super(path):
    data = path.read_bytes()[:SUPER_STRUCT.size]
    vals = SUPER_STRUCT.unpack_from(data)
    keys = [
        "magic", "version", "block_size", "reserved0", "total_size",
        "base_offset", "base_size", "inode_offset", "inode_count",
        "root_inode", "data_offset", "overlay_offset", "overlay_size",
        "overlay_bucket_count", "overlay_bucket_shift", "pcache_offset",
        "pcache_size", "pcache_slot_count", "pcache_hash_shift",
    ]
    obj = dict(zip(keys, vals))
    obj["magic_ok"] = obj["magic"] == DAXFS_MAGIC
    obj["file_size"] = path.stat().st_size
    return obj


def run_mkdaxfs(repo, outdir, name, args, timeout=180):
    tool = repo / "tools" / "mkdaxfs"
    cmd = [str(tool)] + args
    return run_cmd(outdir, name, cmd, cwd=repo, timeout=timeout)


def e1_image_footprint(repo, outdir, args):
    phase = "e1_image_footprint"
    work = mkdir(outdir / "work" / phase)
    src = work / "source"
    make_source_tree(src, args.file_count, args.file_size_mib * 1024 * 1024)
    source_bytes, source_files = apparent_tree_bytes(src)

    specs = [
        ("static", ["-d", str(src), "-o", str(work / "static.daxfs")], work / "static.daxfs"),
        ("static_overlay_requested_file_output",
         ["-d", str(src), "-o", str(work / "static_overlay_requested_file_output.daxfs"),
          "-O", "16M", "-B", "65536"],
         work / "static_overlay_requested_file_output.daxfs"),
        ("empty_overlay16m_pcache16", ["--empty", "-o", str(work / "empty_overlay16m_pcache16.daxfs"),
                                       "-s", "32M", "-O", "16M", "-B", "65536", "-C", "16"],
         work / "empty_overlay16m_pcache16.daxfs"),
    ]

    rows = []
    for label, mkargs, image in specs:
        res = run_mkdaxfs(repo, outdir, f"mkdaxfs_{label}", mkargs)
        row = {
            "label": label,
            "returncode": res["returncode"],
            "source_apparent_bytes": source_bytes,
            "source_regular_files": source_files,
            "image_path": str(image),
        }
        if res["returncode"] == 0 and image.exists():
            row.update(parse_super(image))
        rows.append(row)

    layout_csv = outdir / "e1_image_layout.csv"
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with layout_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    model_rows = []
    for r in rows:
        if r.get("returncode") != 0 or not r.get("magic_ok"):
            continue
        for n in range(1, args.max_instances + 1):
            model_rows.append({
                "instances": n,
                "image_label": r["label"],
                "metric_kind": "model_from_image_layout",
                "per_instance_copy_bytes": n * source_bytes,
                "daxfs_shared_region_bytes": r.get("total_size", 0),
                "daxfs_base_bytes": r.get("base_size", 0),
                "daxfs_overlay_bytes": r.get("overlay_size", 0),
                "daxfs_pcache_bytes": r.get("pcache_size", 0),
                "source_apparent_bytes": source_bytes,
            })
    model_csv = outdir / "e1_footprint_model.csv"
    if model_rows:
        with model_csv.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(model_rows[0].keys()))
            writer.writeheader()
            writer.writerows(model_rows)

    append_event(outdir, phase, "ok", layout_csv=str(layout_csv),
                 model_csv=str(model_csv), source_bytes=source_bytes, rows=len(rows))


def is_mountpoint(path):
    return path and subprocess.run(["mountpoint", "-q", str(path)]).returncode == 0


def fs_type(path):
    if not path:
        return ""
    return command_text(["stat", "-f", "-c", "%T", str(path)]).strip()


def mountinfo_fs_type(path):
    if not path:
        return ""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return ""
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        if parts[4] != str(resolved):
            continue
        try:
            dash = parts.index("-")
        except ValueError:
            continue
        if dash + 1 < len(parts):
            return parts[dash + 1]
    return ""


def require_mount(outdir, phase, mountpoint):
    if not mountpoint:
        append_event(outdir, phase, "skipped", reason="no_mountpoint")
        return False
    mp = Path(mountpoint)
    if not is_mountpoint(mp):
        append_event(outdir, phase, "skipped", reason="not_a_mountpoint", mountpoint=str(mp))
        return False
    stat_type = fs_type(mp)
    mi_type = mountinfo_fs_type(mp)
    if stat_type != "daxfs" and mi_type != "daxfs":
        append_event(outdir, phase, "skipped", reason="mountpoint_is_not_daxfs",
                     mountpoint=str(mp), fs_type=stat_type,
                     mountinfo_fs_type=mi_type)
        return False
    return True


def e2_worker(base_str, worker, files_per_worker):
    base = Path(base_str)
    worker_dir = base / f"worker_{worker:02d}"
    mismatches = 0
    total_bytes = 0
    completed_ops = 0
    failed_index = -1
    failed_path = ""
    start = time.time()
    try:
        worker_dir.mkdir(parents=True, exist_ok=True)
        for i in range(files_per_worker):
            path = worker_dir / f"f{i:04d}.dat"
            failed_index = i
            failed_path = str(path)
            body = f"worker={worker} file={i} token={worker * 1000000 + i}\n"
            path.write_text(body)
            got = path.read_text()
            total_bytes += len(body)
            completed_ops += 1
            if got != body:
                mismatches += 1
    except Exception as exc:
        elapsed = time.time() - start
        return {
            "worker": worker,
            "ops": completed_ops,
            "requested_ops": files_per_worker,
            "mismatches": mismatches,
            "bytes": total_bytes,
            "worker_elapsed_s": elapsed,
            "error": f"{type(exc).__name__}: {exc}",
            "failed_index": failed_index,
            "failed_path": failed_path,
        }
    elapsed = time.time() - start
    return {
        "worker": worker,
        "ops": completed_ops,
        "requested_ops": files_per_worker,
        "mismatches": mismatches,
        "bytes": total_bytes,
        "worker_elapsed_s": elapsed,
        "error": "",
        "failed_index": "",
        "failed_path": "",
    }


def e2_correctness(outdir, args):
    phase = "e2_correctness"
    if not require_mount(outdir, phase, args.mountpoint):
        return
    base = mkdir(Path(args.mountpoint) / f".daxfs_eval_e2_{now_id()}")
    start = time.time()
    worker_rows = []
    with ProcessPoolExecutor(max_workers=args.max_workers) as executor:
        futures = [
            executor.submit(e2_worker, str(base), worker, args.files_per_worker)
            for worker in range(args.max_workers)
        ]
        for future in as_completed(futures):
            worker_rows.append(future.result())
    elapsed = time.time() - start
    worker_rows.sort(key=lambda r: r["worker"])
    mismatches = sum(int(r["mismatches"]) for r in worker_rows)
    operations = sum(int(r["ops"]) for r in worker_rows)
    requested_operations = sum(int(r["requested_ops"]) for r in worker_rows)
    errors = sum(1 for r in worker_rows if r.get("error"))
    csv_path = outdir / "e2_correctness_workers.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["worker", "ops", "requested_ops",
                                               "mismatches", "bytes",
                                               "worker_elapsed_s", "error",
                                               "failed_index", "failed_path"])
        writer.writeheader()
        writer.writerows(worker_rows)
    result = {
        "mountpoint": args.mountpoint,
        "workers": args.max_workers,
        "files_per_worker": args.files_per_worker,
        "operations": operations,
        "requested_operations": requested_operations,
        "mismatches": mismatches,
        "errors": errors,
        "wall_elapsed_s": elapsed,
        "csv": str(csv_path),
    }
    write_json(outdir / "e2_correctness.json", result)
    append_event(outdir, phase, "ok" if mismatches == 0 and errors == 0 else "failed", **result)
    shutil.rmtree(base, ignore_errors=True)


def write_bytes(path, total_bytes, chunk_bytes=1024 * 1024):
    chunk = b"DAXFS-EVAL-WRITE\n" * 4096
    chunk = chunk[:chunk_bytes]
    written = 0
    with path.open("wb") as f:
        while written < total_bytes:
            n = min(len(chunk), total_bytes - written)
            f.write(chunk[:n])
            written += n


def e3_write_worker(base_str, workers, worker, mib_per_worker):
    base = Path(base_str)
    path = base / f"writer_{workers}_{worker}.bin"
    total_bytes = mib_per_worker * 1024 * 1024
    start = time.time()
    try:
        write_bytes(path, total_bytes)
    except Exception as exc:
        elapsed = time.time() - start
        return {
            "worker": worker,
            "bytes": 0,
            "requested_bytes": total_bytes,
            "worker_elapsed_s": elapsed,
            "error": f"{type(exc).__name__}: {exc}",
        }
    elapsed = time.time() - start
    return {
        "worker": worker,
        "bytes": total_bytes,
        "requested_bytes": total_bytes,
        "worker_elapsed_s": elapsed,
        "error": "",
    }


def e3_write_scaling(outdir, args):
    phase = "e3_write_scaling"
    if not require_mount(outdir, phase, args.mountpoint):
        return
    rows = []
    base = mkdir(Path(args.mountpoint) / f".daxfs_eval_e3_{now_id()}")
    for workers in [1, 2, 4, args.max_workers]:
        workers = min(workers, args.max_workers)
        if any(r.get("workers") == workers for r in rows):
            continue
        start = time.time()
        worker_rows = []
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(e3_write_worker, str(base), workers, w,
                                args.write_mib_per_worker)
                for w in range(workers)
            ]
            for future in as_completed(futures):
                worker_rows.append(future.result())
        elapsed = time.time() - start
        requested_total = workers * args.write_mib_per_worker * 1024 * 1024
        completed_total = sum(int(r["bytes"]) for r in worker_rows)
        errors = sum(1 for r in worker_rows if r.get("error"))
        rows.append({
            "workers": workers,
            "status": "ok" if errors == 0 else "failed",
            "total_bytes": completed_total,
            "requested_bytes": requested_total,
            "errors": errors,
            "wall_elapsed_s": elapsed,
            "max_worker_elapsed_s": max(r["worker_elapsed_s"] for r in worker_rows),
            "throughput_mib_s": (completed_total / (1024 * 1024)) / elapsed if elapsed > 0 else 0,
        })
    csv_path = outdir / "e3_write_scaling.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    append_event(outdir, phase, "ok" if all(r["status"] == "ok" for r in rows) else "failed",
                 csv=str(csv_path), rows=len(rows))
    shutil.rmtree(base, ignore_errors=True)


def read_sweep_once(base, total_files, file_size):
    total = 0
    start = time.time()
    for i in range(total_files):
        data = (base / f"read_{i:04d}.bin").read_bytes()
        total += len(data)
    elapsed = time.time() - start
    return total, elapsed


def e4_cache_read_sweep(outdir, args):
    phase = "e4_cache_read_sweep"
    if not require_mount(outdir, phase, args.mountpoint):
        return
    base = mkdir(Path(args.mountpoint) / f".daxfs_eval_e4_{now_id()}")
    rows = []
    file_size = max(4096, args.cache_file_kib * 1024)
    for ratio in [0.5, 1.0, 2.0, 4.0]:
        total_files = max(1, int(args.cache_base_files * ratio))
        for i in range(total_files):
            write_pattern_file(base / f"read_{i:04d}.bin", file_size, f"cache-{ratio}-{i}")
        for pass_id in [1, 2]:
            total, elapsed = read_sweep_once(base, total_files, file_size)
            rows.append({
                "working_set_ratio": ratio,
                "pass": pass_id,
                "files": total_files,
                "total_bytes": total,
                "elapsed_s": elapsed,
                "throughput_mib_s": (total / (1024 * 1024)) / elapsed if elapsed > 0 else 0,
                "metric_note": "read-throughput proxy; inspect counters required for true hit rate",
            })
    csv_path = outdir / "e4_cache_read_sweep.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    append_event(outdir, phase, "ok", csv=str(csv_path), rows=len(rows))
    shutil.rmtree(base, ignore_errors=True)


def e5_single_instance(outdir, args):
    phase = "e5_single_instance_overhead"
    targets = []
    if args.mountpoint and is_mountpoint(Path(args.mountpoint)):
        targets.append(("daxfs", Path(args.mountpoint)))
    for i, b in enumerate(args.baseline_dir or []):
        p = Path(b)
        mkdir(p)
        targets.append((f"baseline_{i}_{fs_type(p) or 'dir'}", p))
    if not targets:
        append_event(outdir, phase, "skipped", reason="no_mountpoint_or_baseline_dir")
        return
    rows = []
    for label, target in targets:
        base = mkdir(target / f".daxfs_eval_e5_{now_id()}")
        path = base / "seq.bin"
        start = time.time()
        write_bytes(path, args.e5_file_mib * 1024 * 1024)
        write_elapsed = time.time() - start
        start = time.time()
        data = path.read_bytes()
        read_elapsed = time.time() - start
        meta_start = time.time()
        for i in range(args.e5_meta_files):
            p = base / f"meta_{i:04d}.txt"
            p.write_text("x\n")
            p.stat()
        meta_elapsed = time.time() - meta_start
        rows.extend([
            {"target": label, "operation": "seq_write", "bytes": len(data), "ops": 1,
             "elapsed_s": write_elapsed, "throughput_mib_s": args.e5_file_mib / write_elapsed if write_elapsed > 0 else 0},
            {"target": label, "operation": "seq_read", "bytes": len(data), "ops": 1,
             "elapsed_s": read_elapsed, "throughput_mib_s": args.e5_file_mib / read_elapsed if read_elapsed > 0 else 0},
            {"target": label, "operation": "create_stat", "bytes": 0, "ops": args.e5_meta_files,
             "elapsed_s": meta_elapsed, "throughput_mib_s": 0},
        ])
        shutil.rmtree(base, ignore_errors=True)
    csv_path = outdir / "e5_single_instance_overhead.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    append_event(outdir, phase, "ok", csv=str(csv_path), rows=len(rows))


def parse_args():
    parser = argparse.ArgumentParser(description="Run DAXFS E1-E5 raw-data evaluation.")
    parser.add_argument("--outdir", default=None, help="Output directory; default eval/raw/<timestamp>.")
    parser.add_argument("--mountpoint", default=None, help="Mounted DAXFS path for E2-E5.")
    parser.add_argument("--baseline-dir", action="append", help="Baseline directory for E5; may repeat.")
    parser.add_argument("--only", action="append",
                        choices=["e1", "e2", "e3", "e4", "e5"],
                        help="Run only selected phase(s). May be repeated. Default runs E1-E5.")
    parser.add_argument("--skip-build", action="store_true", help="Skip make tools/tests and module preflight.")
    parser.add_argument("--max-instances", type=int, default=4)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--file-count", type=int, default=8)
    parser.add_argument("--file-size-mib", type=int, default=4)
    parser.add_argument("--files-per-worker", type=int, default=64)
    parser.add_argument("--write-mib-per-worker", type=int, default=16)
    parser.add_argument("--cache-base-files", type=int, default=8)
    parser.add_argument("--cache-file-kib", type=int, default=512)
    parser.add_argument("--e5-file-mib", type=int, default=32)
    parser.add_argument("--e5-meta-files", type=int, default=1000)
    return parser.parse_args()


def main():
    args = parse_args()
    repo = repo_root()
    outdir = Path(args.outdir) if args.outdir else repo / "eval" / "raw" / now_id()
    mkdir(outdir)
    write_json(outdir / "manifest.json", {
        "run_id": outdir.name,
        "repo": str(repo),
        "argv": sys.argv,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    capture_environment(repo, outdir)
    ensure_userspace_tools(repo, outdir, args.skip_build)
    module_build_preflight(repo, outdir, args.skip_build)
    phases = set(args.only or ["e1", "e2", "e3", "e4", "e5"])
    if "e1" in phases:
        e1_image_footprint(repo, outdir, args)
    if "e2" in phases:
        e2_correctness(outdir, args)
    if "e3" in phases:
        e3_write_scaling(outdir, args)
    if "e4" in phases:
        e4_cache_read_sweep(outdir, args)
    if "e5" in phases:
        e5_single_instance(outdir, args)
    append_event(outdir, "run", "complete", output_dir=str(outdir))
    print(outdir)


if __name__ == "__main__":
    main()
