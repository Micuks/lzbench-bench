#!/usr/bin/env python3
"""lzbench Kunpeng aarch64 自动化基准测试 — 控制平面。

子命令：
  build    clone+make lzbench
  probe    探测当前 lzbench 实际编译进的算法/level
  run      主跑（默认每 NUMA 一个 worker，断点续测）
  status   实时查看 ledger 进度
  summary  聚合 ledger 产出 summary.csv 与 summary.md
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# 让模块从脚本目录可 import
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import algorithms
import topology

DEFAULT_BLOCKS = [0, 4, 64]   # 0 = 不分块
DEFAULT_TIME = (12, 3)        # -t 默认：压缩 12s、解压 3s
DEFAULT_TIMEOUT = 600         # per-task timeout
DEFAULT_LEVEL_POINTS = 5
RUN_TASK = HERE / "run_task.sh"
BUILD_SH = HERE / "build_lzbench.sh"


# ------------------------------- Task ----------------------------------------

@dataclass(frozen=True)
class Task:
    algo: str
    level: int | None
    block_kb: int
    file: str

    @property
    def task_id(self) -> str:
        # SHA1(file) 前 8 字节足够区分文件；level 缺失记 X
        h = hashlib.sha1(self.file.encode()).hexdigest()[:8]
        lvl = "X" if self.level is None else f"L{self.level}"
        return f"{self.algo}-{lvl}-b{self.block_kb}-{h}"


def build_tasks(
    algo_levels: list[tuple[str, int | None]],
    files: list[str],
    blocks: list[int],
) -> list[Task]:
    tasks: list[Task] = []
    file_sizes = {f: os.path.getsize(f) for f in files}
    for f in files:
        size = file_sizes[f]
        # 同一 file 下，>=size 的所有分块都等价于 0（不分块）：去重保留最小的
        eff_blocks = []
        seen_full = False
        for b in blocks:
            if b == 0 or b * 1024 >= size:
                if seen_full:
                    continue
                seen_full = True
                eff_blocks.append(0)
            else:
                eff_blocks.append(b)
        for algo, lvl in algo_levels:
            for b in eff_blocks:
                tasks.append(Task(algo=algo, level=lvl, block_kb=b, file=f))
    return tasks


# ----------------------------- State ledger ----------------------------------

class Ledger:
    """append-only JSONL，文件锁串行化写入。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict]:
        """返回 task_id → 最新状态记录。"""
        latest: dict[str, dict] = {}
        with self.path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = rec.get("task_id")
                if tid:
                    latest[tid] = rec
        return latest

    def append(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock, self.path.open("a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---------------------------- lzbench CSV parse ------------------------------

def _parse_lzbench_csv(text: str, algo: str) -> dict | None:
    """解析 lzbench -o4 输出。

    固定列顺序：
        Compressor name,Compression speed,Decompression speed,
        Original size,Compressed size,Ratio,Filename
    Ratio 在 lzbench 里是 compressed/original*100（百分比，越小越压得好），
    我们额外算一个习惯写法的 comp_ratio_x = orig/comp（越大越好）。
    """
    target = None
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip():
            continue
        if row[0].strip().startswith("Compressor"):
            continue
        first = row[0].strip().lower()
        # 第一列形如 "zstd 1.5.7 -3" 或 "memcpy"；前缀匹配挑出 target 行
        if first.startswith(algo.lower()) or first.startswith(algo.lower().replace("-", "")):
            target = row
            break

    if target is None or len(target) < 6:
        return None

    def fnum(s: str) -> float | None:
        try:
            return float(s.strip())
        except Exception:
            return None

    def inum(s: str) -> int | None:
        try:
            return int(s.strip())
        except Exception:
            return None

    parsed = {
        "name_full":   target[0].strip(),
        "ctime_mb_s":  fnum(target[1]),
        "dtime_mb_s":  fnum(target[2]),
        "orig_size":   inum(target[3]),
        "comp_size":   inum(target[4]),
        "ratio_pct":   fnum(target[5]),
    }
    # comp_ratio_x：orig/comp，例如 3.5 表示 3.5:1
    if parsed["orig_size"] and parsed["comp_size"]:
        parsed["comp_ratio_x"] = round(parsed["orig_size"] / parsed["comp_size"], 4)
    else:
        parsed["comp_ratio_x"] = None
    return parsed


# ----------------------------- Probe -----------------------------------------

def probe_supported(
    lzbench: Path,
    algo_levels: list[tuple[str, int | None]],
    work_dir: Path,
    timeout: int = 30,
) -> dict[str, bool]:
    """在 1KB 临时文件上跑极短 lzbench，识别哪些 (algo,level) 没编译进。

    返回 key=f"{algo}|{level}" → True/False。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    sample = work_dir / "probe.bin"
    if not sample.exists() or sample.stat().st_size != 1024:
        sample.write_bytes(os.urandom(1024))

    out: dict[str, bool] = {}
    for algo, lvl in algo_levels:
        e = f"-e{algo}" if lvl is None else f"-e{algo},{lvl}"
        cmd = [str(lzbench), "-T1", "-x", "-q", "-o4", "-t0,0", "-i1,1", e, str(sample)]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            ok = r.returncode == 0 and bool(_parse_lzbench_csv(r.stdout, algo))
        except subprocess.TimeoutExpired:
            ok = False
        out[f"{algo}|{lvl}"] = ok
    return out


# ----------------------------- Runner ----------------------------------------

class Runner:
    def __init__(
        self,
        lzbench: Path,
        out_dir: Path,
        workers: list[topology.Worker],
        ctime: int,
        dtime: int,
        timeout: int,
    ):
        self.lzbench = lzbench
        self.out_dir = out_dir
        self.workers = workers
        self.ctime = ctime
        self.dtime = dtime
        self.timeout = timeout
        self.ledger = Ledger(out_dir / "state.jsonl")
        self.task_q: queue.Queue[Task | None] = queue.Queue()
        self.stop_evt = threading.Event()
        self.done_count = 0
        self.fail_count = 0
        self.skip_count = 0
        self._counter_lock = threading.Lock()
        self.total = 0
        self._t_start = 0.0

    # 调度接口
    def submit_all(self, tasks: list[Task]) -> None:
        for t in tasks:
            self.task_q.put(t)

    def _run_one(self, t: Task, w: topology.Worker) -> None:
        env = os.environ.copy()
        env.update({
            "TASK_ID": t.task_id,
            "NUMA_ID": str(w.numa),
            "CPU_ID": str(w.cpu),
            "LZBENCH": str(self.lzbench),
            "OUT_DIR": str(self.out_dir),
            "ALGO": t.algo,
            "LEVEL": "" if t.level is None else str(t.level),
            "BLOCK_KB": str(t.block_kb),
            "INPUT": t.file,
            "CTIME_SEC": str(self.ctime),
            "DTIME_SEC": str(self.dtime),
        })
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                ["bash", str(RUN_TASK)],
                env=env,
                capture_output=False,
                timeout=self.timeout,
            )
            rc = r.returncode
            err = ""
        except subprocess.TimeoutExpired:
            rc = -1
            err = "timeout"

        dur = time.monotonic() - t0
        record: dict = {
            "task_id": t.task_id,
            "algo": t.algo,
            "level": t.level,
            "block_kb": t.block_kb,
            "file": t.file,
            "worker": w.label,
            "started_at": started,
            "duration_s": round(dur, 3),
        }

        if rc == 0:
            csv_path = self.out_dir / "tasks" / f"{t.task_id}.csv"
            try:
                parsed = _parse_lzbench_csv(csv_path.read_text(), t.algo)
            except Exception:
                parsed = None
            if parsed and parsed.get("ctime_mb_s") is not None:
                record.update({
                    "status": "done",
                    "name_full": parsed.get("name_full"),
                    "ctime_mb_s": parsed.get("ctime_mb_s"),
                    "dtime_mb_s": parsed.get("dtime_mb_s"),
                    "ratio_pct": parsed.get("ratio_pct"),
                    "comp_ratio_x": parsed.get("comp_ratio_x"),
                    "orig_size": parsed.get("orig_size") or os.path.getsize(t.file),
                    "comp_size": parsed.get("comp_size"),
                })
                with self._counter_lock:
                    self.done_count += 1
            else:
                record.update({"status": "failed", "error": "parse_failed"})
                with self._counter_lock:
                    self.fail_count += 1
        else:
            record.update({"status": "failed", "rc": rc, "error": err or f"rc={rc}"})
            with self._counter_lock:
                self.fail_count += 1

        self.ledger.append(record)
        self._print_progress(t.task_id, record["status"])

    def _worker_loop(self, w: topology.Worker) -> None:
        while not self.stop_evt.is_set():
            try:
                t = self.task_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if t is None:
                self.task_q.task_done()
                return
            try:
                self._run_one(t, w)
            finally:
                self.task_q.task_done()

    def _print_progress(self, tid: str, status: str) -> None:
        with self._counter_lock:
            done = self.done_count
            fail = self.fail_count
            total = self.total
        finished = done + fail
        elapsed = max(time.monotonic() - self._t_start, 0.001)
        rate = finished / elapsed
        eta = (total - finished) / rate if rate > 0 and total else 0
        msg = (
            f"[{finished}/{total}] done={done} failed={fail} "
            f"rate={rate:.2f}/s eta={int(eta)}s :: {tid} ({status})"
        )
        print(msg, flush=True)

    def run(self, tasks: list[Task], skip_count: int) -> None:
        self.total = len(tasks)
        self.skip_count = skip_count
        self._t_start = time.monotonic()

        threads: list[threading.Thread] = []
        for w in self.workers:
            th = threading.Thread(target=self._worker_loop, args=(w,), daemon=False)
            th.start()
            threads.append(th)

        # SIGINT/SIGTERM 不取消 in-flight，只停止派发新 task
        def handle_sig(signum, frame):
            print(f"\n收到 signal={signum}，等待 in-flight task 结束...", flush=True)
            self.stop_evt.set()
        signal.signal(signal.SIGINT, handle_sig)
        signal.signal(signal.SIGTERM, handle_sig)

        # 灌任务
        self.submit_all(tasks)
        # 给每个 worker 一个 None 哨兵让它退出
        for _ in self.workers:
            self.task_q.put(None)

        for th in threads:
            th.join()


# ------------------------------ Subcommands ----------------------------------

def cmd_build(args: argparse.Namespace) -> int:
    prefix = Path(args.prefix).resolve()
    rc = subprocess.call(["bash", str(BUILD_SH), str(prefix), args.ref])
    return rc


def _resolve_lzbench(p: str | None) -> Path:
    if p:
        path = Path(p).resolve()
    else:
        # 默认依次尝试 ./vendor/lzbench、PATH 中的 lzbench
        candidate = HERE / "vendor" / "lzbench"
        if candidate.exists():
            path = candidate
        elif shutil.which("lzbench"):
            path = Path(shutil.which("lzbench"))  # type: ignore[arg-type]
        else:
            raise SystemExit(
                "找不到 lzbench 二进制。请先 `./bench.py build`，或用 --lzbench 指定路径。"
            )
    if not path.exists():
        raise SystemExit(f"lzbench 不存在: {path}")
    return path


def _load_files(files_arg: str) -> list[str]:
    p = Path(files_arg)
    if p.is_dir():
        # 目录里所有普通文件
        return sorted(str(f.resolve()) for f in p.iterdir() if f.is_file())
    if p.is_file() and p.suffix == ".txt":
        out = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(str(Path(line).resolve()))
        return out
    if p.is_file():
        return [str(p.resolve())]
    raise SystemExit(f"--files 不存在: {files_arg}")


def cmd_probe(args: argparse.Namespace) -> int:
    lzbench = _resolve_lzbench(args.lzbench)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    algo_names = [a.strip() for a in args.algos.split(",")] if args.algos else None
    matrix = algorithms.expand_matrix(algo_names, n_points=args.level_points)
    print(f"探测 {len(matrix)} 个 (algo, level) 组合...", flush=True)
    res = probe_supported(lzbench, matrix, out_dir / "probe-tmp")
    out_path = out_dir / "algorithms.detected.json"
    out_path.write_text(json.dumps(res, indent=2, ensure_ascii=False))
    ok = sum(1 for v in res.values() if v)
    print(f"支持 {ok}/{len(res)}，结果写入 {out_path}", flush=True)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    lzbench = _resolve_lzbench(args.lzbench)
    out_dir = Path(args.out).resolve()
    (out_dir / "tasks").mkdir(parents=True, exist_ok=True)

    files = _load_files(args.files)
    if not files:
        raise SystemExit("--files 解析为空。")

    blocks = [int(b) for b in args.blocks.split(",")]
    algo_names = [a.strip() for a in args.algos.split(",")] if args.algos else None

    if args.preset:
        # 直接复用 lzbench 内置 alias（如 ALL / FAST）。优点：跟着 lzbench 二进制走，
        # 算法/level 集合永远同步。--algos 仍可作为后置过滤器。
        l_out = subprocess.run(
            [str(lzbench), "-l"], capture_output=True, text=True, check=True
        ).stdout
        matrix = algorithms.parse_lzbench_alias(l_out, args.preset)
        if algo_names:
            allow = set(algo_names)
            matrix = [(a, l) for (a, l) in matrix if a in allow]
        print(f"preset={args.preset}: {len(matrix)} (algo, level) 对（来自 lzbench -l）", flush=True)
    else:
        matrix = algorithms.expand_matrix(algo_names, n_points=args.level_points)

    # 自检过滤未编译进的算法
    detected_path = out_dir / "algorithms.detected.json"
    if not args.no_probe:
        if detected_path.exists() and not args.force_probe:
            detected = json.loads(detected_path.read_text())
        else:
            print("首次运行，正在 probe 已编译进的 (algo, level)...", flush=True)
            detected = probe_supported(lzbench, matrix, out_dir / "probe-tmp")
            detected_path.write_text(json.dumps(detected, indent=2, ensure_ascii=False))
        before = len(matrix)
        matrix = [(a, l) for (a, l) in matrix if detected.get(f"{a}|{l}", False)]
        print(f"probe: {len(matrix)}/{before} (algo, level) 支持。", flush=True)

    # 拓扑 / worker
    try:
        nodes = topology.detect()
    except RuntimeError as e:
        print(f"警告: {e}; 退化为单 NUMA。", flush=True)
        nodes = topology.fallback_single_node()
    print(f"拓扑: {topology.describe(nodes)}", flush=True)
    workers = topology.plan_workers(nodes, args.workers)
    print(f"workers ({len(workers)}): " + ", ".join(w.label for w in workers), flush=True)

    # 矩阵展开
    tasks = build_tasks(matrix, files, blocks)
    print(f"task 矩阵规模: {len(tasks)} (algos×levels={len(matrix)}, files={len(files)}, blocks={blocks})", flush=True)

    if args.dry_run:
        ctime, dtime = args.time
        est = len(tasks) * (ctime + dtime + 2) / max(len(workers), 1)
        print(f"dry-run: 预估 {int(est)}s ≈ {int(est)//60}min（不含编排开销）", flush=True)
        return 0

    # 断点续测：剔除已 done 的 task；failed 默认也跳过，除非 --retry-failed
    ledger = Ledger(out_dir / "state.jsonl")
    history = ledger.load()
    skip = 0
    pending: list[Task] = []
    for t in tasks:
        rec = history.get(t.task_id)
        if rec and rec.get("status") == "done":
            skip += 1
            continue
        if rec and rec.get("status") == "failed" and not args.retry_failed:
            skip += 1
            continue
        pending.append(t)
    print(f"断点续测：跳过 {skip} 已完成；待跑 {len(pending)}", flush=True)

    if not pending:
        print("没有待跑 task，退出。", flush=True)
        return 0

    ctime, dtime = args.time
    runner = Runner(
        lzbench=lzbench,
        out_dir=out_dir,
        workers=workers,
        ctime=ctime,
        dtime=dtime,
        timeout=args.task_timeout,
    )
    runner.run(pending, skip_count=skip)
    print(f"结束: done={runner.done_count} failed={runner.fail_count}", flush=True)
    return 0 if runner.fail_count == 0 else 2


def cmd_status(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    ledger = Ledger(out_dir / "state.jsonl")
    h = ledger.load()
    by_status: dict[str, int] = defaultdict(int)
    by_algo_done: dict[str, int] = defaultdict(int)
    for rec in h.values():
        by_status[rec.get("status", "?")] += 1
        if rec.get("status") == "done":
            by_algo_done[rec.get("algo", "?")] += 1
    print(f"ledger: {out_dir / 'state.jsonl'}", flush=True)
    print(f"records: {sum(by_status.values())}", flush=True)
    for k, v in sorted(by_status.items()):
        print(f"  {k}: {v}", flush=True)
    print("done by algo (top 20):", flush=True)
    for k, v in sorted(by_algo_done.items(), key=lambda x: -x[1])[:20]:
        print(f"  {k}: {v}", flush=True)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    ledger = Ledger(out_dir / "state.jsonl")
    h = ledger.load()

    rows: list[dict] = []
    for rec in h.values():
        if rec.get("status") != "done":
            continue
        rows.append({
            "file": rec.get("file"),
            "algo": rec.get("algo"),
            "level": rec.get("level"),
            "block_kb": rec.get("block_kb"),
            "comp_ratio_x": rec.get("comp_ratio_x"),
            "ratio_pct": rec.get("ratio_pct"),
            "ctime_mb_s": rec.get("ctime_mb_s"),
            "dtime_mb_s": rec.get("dtime_mb_s"),
            "orig_size": rec.get("orig_size"),
            "comp_size": rec.get("comp_size"),
            "name_full": rec.get("name_full"),
        })

    csv_path = out_dir / "summary.csv"
    fieldnames = ["file", "algo", "level", "block_kb", "comp_ratio_x", "ratio_pct",
                  "ctime_mb_s", "dtime_mb_s", "orig_size", "comp_size", "name_full"]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x["file"] or "", x["algo"] or "",
                                              x["block_kb"] or 0, x["level"] or -1)):
            w.writerow(r)

    # markdown：按 file 分组、按 block 分子表，按 ratio 升序
    md_path = out_dir / "summary.md"
    md = io.StringIO()
    md.write("# lzbench 测试结果\n\n")
    md.write(f"生成时间: {dt.datetime.now().isoformat(timespec='seconds')}\n\n")
    md.write(f"完成 task 数: {len(rows)}\n\n")

    by_file: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_file[r["file"]].append(r)

    for f in sorted(by_file):
        md.write(f"## {f}\n\n")
        per_file = by_file[f]
        for b in sorted({r["block_kb"] for r in per_file}):
            label = "no-chunk" if b == 0 else f"{b}KB"
            md.write(f"### block = {label}\n\n")
            md.write("| algo | level | ratio (x) | ratio (%) | comp MB/s | decomp MB/s |\n")
            md.write("|------|------:|----------:|----------:|----------:|------------:|\n")
            sub = [r for r in per_file if r["block_kb"] == b]
            # 按 comp_ratio_x 降序（压得最好排前），无值的放后
            sub.sort(key=lambda x: (x["comp_ratio_x"] is None,
                                     -(x["comp_ratio_x"] or 0.0)))
            for r in sub:
                cx = "-" if r["comp_ratio_x"] is None else f"{r['comp_ratio_x']:.3f}"
                rp = "-" if r["ratio_pct"] is None else f"{r['ratio_pct']:.2f}"
                ct = "-" if r["ctime_mb_s"] is None else f"{r['ctime_mb_s']:.1f}"
                dt_ = "-" if r["dtime_mb_s"] is None else f"{r['dtime_mb_s']:.1f}"
                md.write(
                    f"| {r['algo']} | {r['level'] if r['level'] is not None else '-'} | "
                    f"{cx} | {rp} | {ct} | {dt_} |\n"
                )
            md.write("\n")

    md_path.write_text(md.getvalue())
    print(f"写入 {csv_path}", flush=True)
    print(f"写入 {md_path}", flush=True)
    return 0


# ------------------------------ argparse -------------------------------------

def _parse_time(s: str) -> tuple[int, int]:
    a, b = s.split(",")
    return int(a), int(b)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    # build
    pb = sub.add_parser("build", help="clone+make lzbench")
    pb.add_argument("--prefix", default=str(HERE / "vendor"))
    pb.add_argument("--ref", default="master")
    pb.set_defaults(func=cmd_build)

    # probe
    pp = sub.add_parser("probe", help="探测 lzbench 编译进的算法")
    pp.add_argument("--lzbench", default=None)
    pp.add_argument("--out", default=str(HERE / "results"))
    pp.add_argument("--algos", default=None)
    pp.add_argument("--level-points", type=int, default=DEFAULT_LEVEL_POINTS)
    pp.set_defaults(func=cmd_probe)

    # run
    pr = sub.add_parser("run", help="主跑（断点续测）")
    pr.add_argument("--files", required=True, help="文件、目录或 .txt 清单")
    pr.add_argument("--out", default=str(HERE / "results"))
    pr.add_argument("--lzbench", default=None)
    pr.add_argument("--workers", type=int, default=None)
    pr.add_argument("--blocks", default=",".join(str(b) for b in DEFAULT_BLOCKS))
    pr.add_argument("--algos", default=None)
    pr.add_argument("--level-points", type=int, default=DEFAULT_LEVEL_POINTS,
                    help="多 level 算法的采样点数（与 --preset 互斥）")
    pr.add_argument("--preset", default=None,
                    help="使用 lzbench 内置 alias（ALL / FAST），等价于上游 -e<ALIAS>。"
                         " ALL = lzbench20_sorted.md 那张表的 candidates 集合。"
                         " 指定后忽略 --level-points，--algos 仍作过滤器。")
    pr.add_argument("--time", type=_parse_time,
                    default=DEFAULT_TIME, help="格式 ctime,dtime（秒）")
    pr.add_argument("--task-timeout", type=int, default=DEFAULT_TIMEOUT)
    pr.add_argument("--retry-failed", action="store_true")
    pr.add_argument("--no-probe", action="store_true",
                    help="跳过 (algo,level) 自检")
    pr.add_argument("--force-probe", action="store_true",
                    help="强制重跑 probe，刷新 algorithms.detected.json")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=cmd_run)

    # status
    ps = sub.add_parser("status", help="查看 ledger")
    ps.add_argument("--out", default=str(HERE / "results"))
    ps.set_defaults(func=cmd_status)

    # summary
    psum = sub.add_parser("summary", help="聚合结果")
    psum.add_argument("--out", default=str(HERE / "results"))
    psum.set_defaults(func=cmd_summary)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
