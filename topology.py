"""NUMA 拓扑探测：解析 numactl --hardware，把 worker 安排到不同 NUMA 上。

返回结果只关心一件事：每个 worker pin 到哪个 (numa_id, cpu_id)。
默认每个 NUMA 取一个核，worker 数 = NUMA 数。--workers N 时按 NUMA round-robin
取核，保证 worker 平均分布到各 NUMA 上，最大化内存带宽隔离。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Worker:
    idx: int
    numa: int
    cpu: int

    @property
    def label(self) -> str:
        return f"numa{self.numa}.cpu{self.cpu}"


def _run_numactl_hardware() -> str:
    if shutil.which("numactl") is None:
        raise RuntimeError(
            "numactl 未安装。请先 `apt-get install numactl` 或 `yum install numactl`。"
        )
    r = subprocess.run(
        ["numactl", "--hardware"], capture_output=True, text=True, check=True
    )
    return r.stdout


def parse_numactl(output: str) -> dict[int, list[int]]:
    """从 `numactl --hardware` 文本中解析 {numa_id: [cpu_id, ...]}."""
    nodes: dict[int, list[int]] = {}
    for line in output.splitlines():
        m = re.match(r"^node\s+(\d+)\s+cpus:\s*(.*)$", line.strip())
        if not m:
            continue
        nid = int(m.group(1))
        cpus = [int(x) for x in m.group(2).split() if x.strip().isdigit()]
        nodes[nid] = cpus
    return nodes


def detect() -> dict[int, list[int]]:
    return parse_numactl(_run_numactl_hardware())


def fallback_single_node() -> dict[int, list[int]]:
    """没有 numactl 时退化：用 nproc 列出所有 cpu 当 numa0。"""
    try:
        n = int(subprocess.check_output(["nproc"], text=True).strip())
    except Exception:
        n = 1
    return {0: list(range(n))}


def plan_workers(nodes: dict[int, list[int]], n_workers: int | None = None) -> list[Worker]:
    """按 NUMA round-robin 派核，避免两个 worker 落在同一 NUMA。

    n_workers=None ⇒ 每 NUMA 一个 worker。
    n_workers > NUMA 数 ⇒ 同 NUMA 内取多核，但每核仍专属一个 worker。
    """
    numa_ids = sorted(nodes.keys())
    if not numa_ids:
        raise RuntimeError("未探测到任何 NUMA 节点。")

    if n_workers is None:
        n_workers = len(numa_ids)

    # 每 NUMA 内的 cpu 队列（拷贝以便弹出）
    pool = {nid: list(nodes[nid]) for nid in numa_ids}
    workers: list[Worker] = []
    i = 0
    while len(workers) < n_workers:
        nid = numa_ids[i % len(numa_ids)]
        i += 1
        if not pool[nid]:
            # 该 NUMA 已无可用核，跳过；若所有 NUMA 都空，结束
            if all(not pool[n] for n in numa_ids):
                break
            continue
        cpu = pool[nid].pop(0)
        workers.append(Worker(idx=len(workers), numa=nid, cpu=cpu))

    if not workers:
        raise RuntimeError("无可用 CPU 分配 worker。")
    return workers


def describe(nodes: dict[int, list[int]]) -> str:
    parts = [f"NUMA {nid}: {len(cpus)} cpus ({cpus[0]}..{cpus[-1]})"
             for nid, cpus in sorted(nodes.items())]
    return "; ".join(parts)
