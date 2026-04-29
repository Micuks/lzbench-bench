#!/usr/bin/env bash
# 测试平面 wrapper：把一个 task 翻译成单次 lzbench 调用。
# 控制平面（bench.py）通过环境变量传参，避免长 argv 的转义问题。
#
# 必需 env:
#   TASK_ID        e.g. zstd-L11-b64-3f1c2a8e
#   NUMA_ID        NUMA node 编号；空字符串则不启用 numactl
#   CPU_ID         物理核编号
#   LZBENCH        lzbench 二进制路径
#   OUT_DIR        结果根目录（含 tasks/ 子目录）
#   ALGO           e.g. zstd
#   LEVEL          e.g. 11；空字符串表示该算法无 level
#   BLOCK_KB       4 / 64 / 0（0 表示不分块，不传 -b）
#   INPUT          输入文件绝对路径
#   CTIME_SEC      压缩计时秒数（lzbench -t 第一个值）
#   DTIME_SEC      解压计时秒数（lzbench -t 第二个值）

set -u  # 不开 -e：lzbench 非零退出由调用方判断

: "${TASK_ID:?TASK_ID required}"
: "${LZBENCH:?LZBENCH required}"
: "${OUT_DIR:?OUT_DIR required}"
: "${ALGO:?ALGO required}"
: "${INPUT:?INPUT required}"
: "${CTIME_SEC:=12}"
: "${DTIME_SEC:=3}"
: "${LEVEL:=}"
: "${BLOCK_KB:=0}"
: "${NUMA_ID:=}"
: "${CPU_ID:=}"

mkdir -p "$OUT_DIR/tasks"
csv="$OUT_DIR/tasks/$TASK_ID.csv"
log="$OUT_DIR/tasks/$TASK_ID.log"

# 构造 -e 参数
if [[ -n "$LEVEL" ]]; then
  e_arg="-e${ALGO},${LEVEL}"
else
  e_arg="-e${ALGO}"
fi

# 构造 -b 参数（0 = 不分块即不传 -b）
b_args=()
if [[ "$BLOCK_KB" != "0" ]]; then
  b_args=("-b${BLOCK_KB}")
fi

# 构造 numactl 前缀
prefix=()
if [[ -n "$NUMA_ID" && -n "$CPU_ID" ]] && command -v numactl >/dev/null 2>&1; then
  prefix=(numactl --physcpubind="$CPU_ID" --membind="$NUMA_ID" --)
elif [[ -n "$CPU_ID" ]] && command -v taskset >/dev/null 2>&1; then
  prefix=(taskset -c "$CPU_ID")
fi

{
  echo "# task=$TASK_ID worker=numa${NUMA_ID}.cpu${CPU_ID} t=$(date -u +%FT%TZ)"
  echo "# cmd: ${prefix[*]+${prefix[*]} }$LZBENCH -T1 -x -q -o4 -p2 -t${CTIME_SEC},${DTIME_SEC} $e_arg ${b_args[*]+${b_args[*]} }$INPUT"
} >"$log"

# -T1 单线程；-x 关闭 lzbench 内部的实时优先级（避免多 worker 互抢调度）；
# -q 抑制进度条；-o4 CSV；-p2 多次迭代取均值
# ${arr[@]+"${arr[@]}"} 写法兼容 set -u 时的空数组展开
"${prefix[@]+"${prefix[@]}"}" "$LZBENCH" \
  -T1 -x -q -o4 -p2 \
  -t"${CTIME_SEC}","${DTIME_SEC}" \
  "$e_arg" \
  ${b_args[@]+"${b_args[@]}"} \
  "$INPUT" \
  >"$csv" 2>>"$log"

rc=$?
echo "# rc=$rc finished=$(date -u +%FT%TZ)" >>"$log"
exit $rc
