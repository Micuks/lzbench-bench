# lzbench-bench

[lzbench](https://github.com/inikep/lzbench) 的自动化基准测试编排器，专为
**Kunpeng aarch64 多核 NUMA 服务器**设计。一行命令在一组语料上把 50+ 压缩算法 ×
多个 level × 三种分块（4KB / 64KB / 不分块）全跑一遍，自动并行、自动 NUMA 隔离、
自动断点续测、自动出报表。

---

## 它解决什么问题

直接用 `lzbench` 跑测试会遇到三个痛点：

1. **结果会互相污染**——压缩带宽是 memory-bound，并行跑两个算法会在内存通道上打架，
   你以为测的是 lz4 的带宽，实际是 lz4 + zstd 抢通道之后的下界。
2. **手工拼笛卡尔积烦死人**——`算法 × level × 文件 × 分块` 几百上千个 task，
   shell for 循环写死人不说，跑到一半挂了得从头开始。
3. **lzbench 只接受单一 `-b` 与单一 `-e`**——必须由外层调度器生成所有组合，
   每次只调一次 lzbench。

本工具：
- 把 worker 钉在不同 NUMA 上跑（`numactl --physcpubind --membind`），并行加速、
  内存通道隔离，结果可比可信。
- 把所有 task 写进 append-only 的 `state.jsonl`，`Ctrl+C` 后再跑自动跳过已完成。
- 内置算法-level 表 + 智能采样：levels 少则全测，levels 多则取 5 个等距代表点。
- 启动时自检 lzbench 实际编译进了哪些算法（不同 Makefile flag 会裁剪），
  避免在不支持的算法上浪费时间。
- 跑完产出 `summary.csv`（机器友好）+ `summary.md`（按文件分组、按分块分子表、
  按压缩率倒序、可直接贴报告）。

---

## 准备工作

测试机（Linux / aarch64 或 x86_64 都可以，本工具不挑架构）需要：

| 依赖 | 最低要求 | 用途 |
|---|---|---|
| `python3` | 3.8+ | 控制平面（不依赖任何第三方库） |
| `bash` | 4.0+ | 测试平面 wrapper |
| `gcc` / `g++` / `make` | 任意近代版本 | 构建 lzbench（如选择本工具自动构建） |
| `git` | 任意 | 同上 |
| `numactl` | 任意 | NUMA pin（强烈建议；缺失时退化为单 NUMA、并行隔离失效） |
| `taskset` (util-linux) | 任意 | numactl 缺失时的二线 fallback |

安装命令：
```bash
# Debian/Ubuntu
sudo apt-get install -y python3 build-essential git numactl

# RHEL/CentOS/openEuler
sudo yum install -y python3 gcc gcc-c++ make git numactl
```

---

## 30 秒上手

```bash
# 1) 拉到测试机上
cd /path/to/repo/lzbench-bench

# 2) 构建 lzbench（首次约 1-3 分钟）
./bench.py build

# 3) 写一份测试文件清单（绝对路径，每行一个）
cat > files.txt <<'EOF'
/data/corpus/glove.6B.300d.txt
/data/corpus/silesia/dickens
/data/corpus/silesia/mozilla
EOF

# 4) 看一眼会跑多少 task
./bench.py run --files files.txt --dry-run

# 5) 真跑（默认每 NUMA 一个 worker，自动断点续测）
./bench.py run --files files.txt

# 6) 跑完出报表
./bench.py summary
cat results/summary.md
```

就这些。中途要看进度就 `./bench.py status`，`Ctrl+C` 中断后再次执行第 5 步会自动续跑。

---

## 测试文件清单（`--files`）的三种写法

`--files` 接受三种输入：

### 1. 一个 `.txt` 清单文件（推荐）
每行一个**绝对路径**，`#` 开头为注释、空行忽略。
```
# Silesia 标准语料
/data/silesia/dickens
/data/silesia/mozilla
/data/silesia/webster

# 嵌入向量
/data/glove/glove.6B.300d.txt

# 自定义业务数据
/data/our-app/oltp.dump
```

### 2. 一个目录
```bash
./bench.py run --files /data/silesia/
```
会把目录下**所有普通文件**当作测试输入（不递归子目录）。

### 3. 单个文件
```bash
./bench.py run --files /data/glove.txt
```

> 💡 lzbench 是把整个文件**整体读到内存**再压缩的，所以测试文件别太大——一个文件
> 通常控制在几百 MB 到 1-2 GB 之间。如果你想测的是 50 GB 的大文件，更合理的做法
> 是切一段代表性数据出来。

---

## 跑测试的常见姿势

### 全量跑（推荐 / 默认）
```bash
./bench.py run --files files.txt
```
默认配置：
- worker 数 = NUMA 节点数（每 NUMA 钉 1 个核）
- 算法 = `--preset ALL`，即上游
  [`doc/lzbench20_sorted.md`](https://github.com/inikep/lzbench/blob/master/doc/lzbench20_sorted.md)
  那张全压缩器对照表用的 candidate 集合（148 个 (algo, level) 对，含 `zstd_fast` 的负 level、
  `bsc1/4/5` 子 alias、`lzham 0/1`、`lzo1b,1,3,6,9,99,999` 等手工策划的 level，
  来自 `lzbench -l` 输出的 `ALL = ...` 行——跟着二进制走永不失同步）。
- 分块：`0 (不分块), 4 KB, 64 KB`
- 计时：压缩 12s + 解压 3s
- per-task 超时：86400s（24 小时）。慢算法 + 大文件可能跑过夜，超时阈值放得很宽。

跟上游 `lzbench -eALL` 的差别只在编排：本工具会把 148 pairs × 3 分块 × N 文件并行
铺到所有 NUMA 上、可断点续测、自动出 summary。

`--algos` 仍可作为后置过滤器（如 `--algos zstd,brotli,kanzi` 只保留这三类）。

### 切换 preset

```bash
# lzbench 的"高速档"子集（>100 MB/s 的算法，35 对）
./bench.py run --files files.txt --preset FAST

# 关闭 preset，改用我们朴素的"levels 多则等距 5 点"采样（自由度更高，
# 但对 lzo1b/lzo1c 这类"看着 [1-999] 实际只有几个有效 level"的算法会浪费许多 probe）
./bench.py run --files files.txt --preset OFF --algos zstd --level-points 22
```

### 只测几个感兴趣的算法
```bash
./bench.py run --files files.txt --algos zstd,lz4,lz4hc,brotli
```

### 改 level 采样密度（对每个算法用 8 个点而不是 5 个）
```bash
./bench.py run --files files.txt --level-points 8
```

### 改分块组合
```bash
# 加测 1MB 分块；0 表示"不分块"
./bench.py run --files files.txt --blocks 0,4,16,64,1024
```

### 跑得更快（牺牲精度做快速摸底）
```bash
./bench.py run --files files.txt --time 3,1
```

### 跑得更准（每 task 多花时间稳态）
```bash
./bench.py run --files files.txt --time 30,10
```

### 串行跑（最干净的隔离，但慢 N 倍）
```bash
./bench.py run --files files.txt --workers 1
```

### 加大并行度（每 NUMA 内多核并行；会牺牲一些带宽测量纯净度）
```bash
# 假设 4 NUMA，每个 NUMA 取 2 个核 → 总 8 worker
./bench.py run --files files.txt --workers 8
```

### 干跑只看矩阵规模与预估耗时
```bash
./bench.py run --files files.txt --dry-run
```

### 失败的 task 重跑一遍
```bash
./bench.py run --files files.txt --retry-failed
```

### 跳过自检（如果你确信所有算法都支持，可省 30s 启动时间）
```bash
./bench.py run --files files.txt --no-probe
```

---

## 断点续测怎么用

完全无感。任何时候 `Ctrl+C` 中断、机器重启、跑挂了，再次执行**完全相同的 `run` 命令**，
工具会：

1. 读 `results/state.jsonl`，找出已 `done` 的 task → 跳过
2. 找出标 `failed` 的 task → 默认也跳过（避免反复跑必失败的算法）
3. 剩下的灌进队列继续跑

`failed` 的 task 想重试加 `--retry-failed`。

> ⚠️ 想重新跑一份完全干净的结果，删 `results/` 整个目录即可。**不要**只删 `state.jsonl`
> 而保留 `tasks/` 子目录——会有歧义。

---

## 实时查看进度

主跑命令本身会打印每个 task 的 `[done/total] eta=Ns` 进度行。如果想从另一个终端看：

```bash
./bench.py status
```
输出例：
```
ledger: /home/me/lzbench-bench/results/state.jsonl
records: 386
  done: 380
  failed: 6
done by algo (top 20):
  zstd: 75
  brotli: 60
  lz4hc: 36
  ...
```

或者直接 `tail -f results/state.jsonl`，每完成一条就追加一行 JSON。

---

## 输出文件结构

```
results/
├── state.jsonl                  ← 单一可信源；append-only；每 task 一行
├── algorithms.detected.json     ← 启动 probe 缓存（哪些 algo,level 被支持）
├── tasks/
│   ├── zstd-L11-b64-3f1c2a8e.csv   ← lzbench 原始 -o4 输出
│   └── zstd-L11-b64-3f1c2a8e.log   ← 实际命令行 + 退出码 + stderr
├── summary.csv                  ← `bench.py summary` 生成；机器友好
└── summary.md                   ← `bench.py summary` 生成；按文件/分块分组的对比表
```

`summary.csv` 列：
```
file, algo, level, block_kb, comp_ratio_x, ratio_pct,
ctime_mb_s, dtime_mb_s, orig_size, comp_size, name_full
```

- `comp_ratio_x` = `orig / comp`，常用习惯（3.5 表示 3.5:1，越大越好）
- `ratio_pct` = `comp / orig * 100`，lzbench 原生指标（100 表示无压缩，越小越好）
- `ctime_mb_s` / `dtime_mb_s` = 压缩 / 解压带宽（MB/s）
- `name_full` = lzbench 报的算法全名，含版本号（如 `zstd 1.5.7 -3`）

`summary.md` 长这样（按文件分组，每组三张子表，按压缩率倒序）：
```markdown
## /data/glove.6B.300d.txt

### block = no-chunk

| algo | level | ratio (x) | ratio (%) | comp MB/s | decomp MB/s |
|------|------:|----------:|----------:|----------:|------------:|
| zstd | 22 | 3.412 | 29.31 | 27.5 | 887.2 |
| brotli | 11 | 3.398 | 29.43 | 0.6 | 412.5 |
| zstd | 17 | 3.250 | 30.77 | 53.0 | 884.1 |
| ...
```

---

## 让测量更准的几个开关（强烈建议）

CPU 频率抖动是带宽测量噪声的主要来源。跑前在测试机上：

```bash
# 1) 锁定 performance governor，避免休眠/降频
sudo cpupower frequency-set -g performance

# 2) 关闭 turbo（按需，turbo 会让 ctime/dtime 在算法间不可比）
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost 2>/dev/null || true

# 3) 临时关闭透明大页（lzma 等大窗口算法对 THP 敏感）
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled

# 4) 关闭 ASLR（可选；让 ratio 更稳定）
echo 0 | sudo tee /proc/sys/kernel/randomize_va_space
```

跑完想恢复 turbo 就 `echo 1 > .../boost`。

---

## CLI 速查

```text
bench.py build     [--prefix DIR] [--ref BRANCH]
bench.py probe     [--lzbench PATH] [--out DIR] [--algos LIST] [--level-points N]
bench.py run       --files FILES_OR_DIR_OR_TXT
                   [--out DIR]              输出目录，默认 ./results
                   [--lzbench PATH]         lzbench 二进制，默认 ./vendor/lzbench
                   [--workers N]            worker 数，默认 = NUMA 节点数
                   [--blocks 0,4,64]        分块组合（KB；0 = 不分块）
                   [--algos zstd,lz4,...]   仅测这些算法（默认全部）
                   [--preset ALL|FAST|OFF]  默认 ALL，复现 lzbench20_sorted.md 的 candidate 集合
                   [--level-points 5]       朴素采样点数（仅 --preset OFF 时生效）
                   [--time 12,3]            压缩,解压计时（秒）
                   [--task-timeout 86400]   单 task 上限（秒），默认 24h
                   [--retry-failed]         也重跑 failed 状态的 task
                   [--no-probe]             跳过 (algo,level) 自检
                   [--force-probe]          强制重新 probe
                   [--dry-run]              只打印矩阵规模与预估耗时
bench.py status    [--out DIR]
bench.py summary   [--out DIR]
```

---

## FAQ / 故障排查

**Q: 跑了一半发现某个算法测试结果异常想剔除，怎么办？**

A: 编辑 `results/state.jsonl`，把那些行删掉（或用 `grep -v 'algo":"xxx"'` 过滤后回写），
然后再 `bench.py run` 续跑。或者更安全的做法：保留 ledger 不动，把那个算法从 `--algos`
里去掉，然后 `bench.py summary` 出报表时它就不会出现。

**Q: 报"numactl 未安装"然后退化为单 NUMA。**

A: 装 `numactl`。**没装 numactl 的话，并行跑就会互相干扰内存带宽**，结果只能当
"相对趋势"看，绝对数值会偏低。强烈建议装上。

**Q: 某个算法 100% 失败。**

A: 先看 `results/tasks/<failed-task-id>.log`，多半是该算法没编译进 lzbench
（开发者用 `DONT_BUILD_*` 关掉了），或者文件太小/太大触发了算法的边界条件。
启动时的 probe 应该已经过滤掉前者；如果是后者，给 `--algos` 排除它即可。

**Q: 跑完想再多测几个文件，能加进来吗？**

A: 直接把新文件加进 `files.txt`，再 `bench.py run --files files.txt` 就行。
旧文件的 task_id 不变（含文件路径的 SHA1），自动跳过；只有新文件的 task 会跑。

**Q: 跑完想再多测一个算法，能加进来吗？**

A: 同理，`--algos zstd,brotli,kanzi`（或不指定 `--algos` 即全量）继续跑就行，
已完成的会跳过。

**Q: 报告里 `comp_ratio_x` 接近 1 的算法是怎么回事？**

A: 在测试数据上没压出东西。常见情况：
- 数据本身已经压缩过（如 `.gz` / 视频 / 加密文件）
- 用了高速低压缩比的算法（lz4 在随机数据上就是 1:1）

这是真实信号，不是 bug。

**Q: lzbench 的 ratio 列怎么看？**

A: lzbench 用的是百分比 `compressed/original*100`：100 表示无压缩，50 表示压到一半，
**越小越好**。本工具的 `summary.csv` 里用 `ratio_pct` 表示这个，并额外算了
`comp_ratio_x = original/compressed`（习惯写法，越大越好）方便阅读。

**Q: 单线程 `-T1` 是不是低估了带宽？多核场景呢？**

A: `-T1` 是为了让"单 worker = 单核"的测量纯净。多核加速本质上是 lzbench 把数据切成
多块每块独立压一遍——你完全可以用本工具用 4KB / 64KB 分块跑出"单核分块带宽"，再乘以
核数得到多核理论上限。如果你想直接测 lzbench 自己的多线程实现，把 `run_task.sh` 里
`-T1` 改成 `-T4` 即可，但记得把 worker 数也调成 1（避免 N×4 个核互抢）。

---

## 它的工作流程（5 行说清楚）

1. `bench.py run` 解析文件清单 + 算法表 + 分块列表 → 笛卡尔积出 task 列表。
2. 读 `state.jsonl` 把已完成的 task 剔掉。
3. 起 N 个 worker 线程，每个线程绑死一个 `(numa, cpu)`。
4. 每个 worker 不断 `queue.get()`，调 `run_task.sh`（里面是 `numactl ... lzbench ...`），
   解析 CSV 输出，append 一行 JSON 到 `state.jsonl`。
5. 全跑完或被中断 → 退出。`bench.py summary` 把 ledger 折成 CSV + Markdown 报告。

源码不到 700 行，没有第三方依赖，欢迎按需修改。

---

## License

本编排器代码（`bench.py` / `*.py` / `*.sh` / `README.md`）以
**GPL-2.0-or-3.0** 发布——与上游 [lzbench](https://github.com/inikep/lzbench)
的 LICENSE 同款（GPL v2，或 at your option v3）。详见 [`LICENSE`](./LICENSE)。

`build_lzbench.sh` 产出的 `vendor/lzbench` 二进制内打包了 50+ 个独立压缩器，
每个有各自的 license（MIT / BSD / Apache-2.0 / zlib / GPL 等）；如果你分发该
二进制，务必同时遵守 lzbench 仓库根 `LICENSE` 与 `lz/<compressor>/` 子目录下
各自的声明。
