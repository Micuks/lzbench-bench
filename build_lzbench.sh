#!/usr/bin/env bash
# 在指定 prefix 下 clone & make lzbench。
# 用法: build_lzbench.sh <prefix_dir> [git_ref]
#   prefix_dir: 输出根目录；二进制最终位于 <prefix>/lzbench
#   git_ref:    可选，分支或 tag，默认 master

set -euo pipefail

prefix="${1:-./vendor}"
ref="${2:-master}"
repo="https://github.com/inikep/lzbench.git"

mkdir -p "$prefix"
src="$prefix/lzbench-src"

if [[ ! -d "$src/.git" ]]; then
  git clone --depth=1 --branch "$ref" "$repo" "$src"
else
  git -C "$src" fetch --depth=1 origin "$ref"
  git -C "$src" checkout "$ref"
  git -C "$src" reset --hard "origin/$ref" || true
fi

# aarch64 上 make 应当 OOTB 通过；如有需要可加 CFLAGS。
make -C "$src" -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"

install -m 0755 "$src/lzbench" "$prefix/lzbench"
echo "OK: $prefix/lzbench"
"$prefix/lzbench" -h 2>&1 | head -5 || true
