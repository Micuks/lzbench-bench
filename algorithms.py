"""算法清单与 level 采样策略。

ALGOS 列表来源于 lzbench 源码中的 compressors[]（README 标称 50+ 压缩器）。
不同发行版/构建可能裁掉一部分（Makefile DONT_BUILD_*），bench.py 启动时会做一次
自检并把不支持的过滤掉，所以这里宁可多列。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlgoSpec:
    name: str
    lo: int | None
    hi: int | None

    @property
    def has_levels(self) -> bool:
        return self.lo is not None and self.hi is not None


ALGOS: list[AlgoSpec] = [
    AlgoSpec("blosclz", 1, 9),
    AlgoSpec("brieflz", 1, 8),
    AlgoSpec("brotli", 0, 11),
    AlgoSpec("bsc", None, None),
    AlgoSpec("bzip2", 1, 9),
    AlgoSpec("bzip3", None, None),
    AlgoSpec("crush", 0, 2),
    AlgoSpec("csc", 1, 5),
    AlgoSpec("density", 1, 3),
    AlgoSpec("fastlz", 1, 2),
    AlgoSpec("fastlzma2", 1, 10),
    AlgoSpec("glza", None, None),
    AlgoSpec("kanzi", 1, 9),
    AlgoSpec("libdeflate", 1, 12),
    AlgoSpec("lizard", 10, 49),
    AlgoSpec("lz4", None, None),
    AlgoSpec("lz4fast", 1, 99),
    AlgoSpec("lz4hc", 1, 12),
    AlgoSpec("lzav", 1, 2),
    AlgoSpec("lzf", 0, 1),
    AlgoSpec("lzfse", None, None),
    AlgoSpec("lzg", 1, 9),
    AlgoSpec("lzham", 0, 4),
    AlgoSpec("lzjb", None, None),
    AlgoSpec("lzlib", 0, 9),
    AlgoSpec("lzma", 0, 9),
    AlgoSpec("lzo1", 1, 1),
    AlgoSpec("lzo1a", 1, 1),
    AlgoSpec("lzo1b", 1, 999),
    AlgoSpec("lzo1c", 1, 999),
    AlgoSpec("lzo1f", 1, 1),
    AlgoSpec("lzo1x", 1, 999),
    AlgoSpec("lzo1y", 1, 1),
    AlgoSpec("lzo1z", 999, 999),
    AlgoSpec("lzo2a", 999, 999),
    AlgoSpec("lzrw", 1, 5),
    AlgoSpec("lzsse2", 1, 17),
    AlgoSpec("lzsse4", 1, 17),
    AlgoSpec("lzsse8", 1, 17),
    AlgoSpec("lzvn", None, None),
    AlgoSpec("memcpy", None, None),
    AlgoSpec("memlz", None, None),
    AlgoSpec("ppmd8", 2, 12),
    AlgoSpec("quicklz", 1, 3),
    AlgoSpec("slz_zlib", 1, 3),
    AlgoSpec("snappy", None, None),
    AlgoSpec("tamp", None, None),
    AlgoSpec("tornado", 1, 16),
    AlgoSpec("ucl_nrv2b", 1, 9),
    AlgoSpec("ucl_nrv2d", 1, 9),
    AlgoSpec("ucl_nrv2e", 1, 9),
    AlgoSpec("xpack", 1, 9),
    AlgoSpec("xz", 0, 9),
    AlgoSpec("yalz77", 1, 12),
    AlgoSpec("yappy", 1, 99),
    AlgoSpec("zlib", 1, 9),
    AlgoSpec("zlib-ng", 1, 9),
    AlgoSpec("zling", 0, 4),
    AlgoSpec("zpaq", 1, 5),
    AlgoSpec("zstd", 1, 22),
]

ALGO_BY_NAME: dict[str, AlgoSpec] = {a.name: a for a in ALGOS}


def sample_levels(spec: AlgoSpec, n_points: int = 5) -> list[int | None]:
    """levels 少则全测，levels 多则等距插值（含两端点）。

    返回 [None] 表示该算法不接受 -level（lzbench 调用时不带 ,N 后缀）。
    """
    if not spec.has_levels:
        return [None]
    lo, hi = spec.lo, spec.hi  # type: ignore[assignment]
    width = hi - lo + 1
    if width <= n_points:
        return list(range(lo, hi + 1))
    # 等距 n_points 点，含两端，去重保持升序。
    if n_points <= 1:
        return [lo]
    step = (hi - lo) / (n_points - 1)
    pts = sorted({round(lo + i * step) for i in range(n_points)})
    # 边界保险（避免浮点导致的端点丢失）
    if pts[0] != lo:
        pts.insert(0, lo)
    if pts[-1] != hi:
        pts.append(hi)
    return pts


def expand_matrix(
    algo_names: list[str] | None,
    n_points: int = 5,
) -> list[tuple[str, int | None]]:
    """展开 (algo, level) 列表。algo_names=None 表示全部算法。"""
    selected = ALGOS if algo_names is None else [ALGO_BY_NAME[n] for n in algo_names]
    out: list[tuple[str, int | None]] = []
    for spec in selected:
        for lvl in sample_levels(spec, n_points):
            out.append((spec.name, lvl))
    return out
