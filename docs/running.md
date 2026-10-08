# 安装与全部运行方式

[返回首页](../README.md) · [接口说明](api.md) · [示例](../examples/README.md)

## 环境安装

已验证 Python 3.12。下载 ZIP/克隆后进入包含 `baseline.py`、`requirements.txt` 的仓库根目录，不需要先激活虚拟环境。

Windows PowerShell：

```powershell
python --version
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux/macOS：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

根依赖入口引用 `workload_profiling/requirements.txt` 的固定版本，不重复列版本。以下示例使用 PowerShell；在 Linux/macOS 将 `& .\.venv\Scripts\python.exe` 替换为 `.venv/bin/python`。

prompt 计数首次会下载固定 Qwen tokenizer/config/template，后续校验缓存并从本地加载。不会下载模型权重，也不需要 PyTorch。`PyTorch was not found` 是 tokenizer-only 环境的提示。

默认 baseline 网页启动与 CSV 导出使用标准库，不加载 pandas。历史 `--length-demo`、`--export-only` 和 Stage 分析才按需加载相应数据分析依赖；prompt 回放仍需要 transformers/tokenizers 及它们自身的依赖。

## 不依赖原始数据的快速验证

```powershell
& .\.venv\Scripts\python.exe -m examples.run_baseline
```

长度模式仍需要安装项目依赖，但运行时不读取 tokenizer。预期 9 条请求、轻型 5、重型 4、拒绝 0，触发原因包含三种情况；结果在 `workload_profiling/results/baseline_example/`。

## Baseline 命令行

```powershell
# 默认数据，全量 3,168 行
& .\.venv\Scripts\python.exe baseline.py

# 只读前 200 行，覆盖批参数与阈值
& .\.venv\Scripts\python.exe baseline.py --limit 200 --arrival-interval-ms 1 --batch-size 8 --batch-wait-ms 10 --input-threshold 40000 --output-threshold 578

# 自己的完整上下文 JSONL
& .\.venv\Scripts\python.exe baseline.py --source examples/prompt_requests.jsonl --output-dir workload_profiling/results/baseline_example

# 已知长度 JSONL，无需 tokenizer
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --batch-size 2 --batch-wait-ms 3 --output-dir workload_profiling/results/baseline_example

# 自定义策略
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --strategy examples.min_tpm:MinTpmStrategy --output-dir workload_profiling/results/baseline_custom

# 内置批内排序：总 tokens 最短优先
& .\.venv\Scripts\python.exe baseline.py --limit 200 --batch-order shortest_first --output-dir workload_profiling/results/baseline_custom/shortest_first

# 自定义批内排序与路由同时启用
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --batch-size 2 --batch-wait-ms 3 --batch-order examples.output_first:OutputLongestFirstOrder --strategy examples.min_tpm:MinTpmStrategy --output-dir workload_profiling/results/baseline_custom/output_first

# 完全等价的模块入口与帮助
& .\.venv\Scripts\python.exe -m workload_profiling.baseline.run --limit 200
& .\.venv\Scripts\python.exe baseline.py --help
```

### CLI 参数表

CLI 覆盖优先于配置文件；未提供的参数沿用配置。覆盖只影响本次进程，不改磁盘配置。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--config PATH` | 包内 `config/baseline.json` | 自定义完整配置 |
| `--source PATH` | `prompt数据/prompt回答数据包_3168条/prompt回答.jsonl` | 原始或长度 JSONL |
| `--source-format prompt\|lengths` | `prompt` | 数据解析方式 |
| `--output-dir PATH` | 包内 `results/baseline/` | 结果目录 |
| `--limit N` | 全部 | 正整数，只读取前 N 条；CLI 的 0 无效 |
| `--arrival-interval-ms N` | 配置中的 1 | 正整数，虚拟到达间隔 |
| `--batch-size N` | 配置中的 16 | 正整数，重型收集数量 |
| `--batch-wait-ms N` | 配置中的 20 | 非负整数，最老重型收集等待；0 立即释放 |
| `--input-threshold X` | 配置中的 40342.5 | 有限非负数，tokens，包含等号 |
| `--output-threshold X` | 配置中的 578 | 有限非负数，tokens，包含等号 |
| `--strategy SPEC` | 配置中的 `min_rpm` | 内置策略或 `module:attribute` |
| `--batch-order SPEC` | 配置中的 `fifo` | fifo/shortest_first/longest_first 或 `module:attribute`；控制批内派发顺序 |
| `-h`、`--help` | — | 帮助后退出 |

退出码 0 表示成功且没有拒绝；拒绝请求或 argparse/输入配置错误为 2。其他未被捕获的运行异常可能为 1。出现拒绝时仍会导出该次完整结果；输入或配置失败时不应将目录中的旧结果当作本次成功产物。

默认路径相对包位置解析；显式相对路径按调用方工作目录解析。推荐从仓库根目录运行，这也让 `examples.min_tpm` 等模块可导入。重复使用输出目录会替换七个标准文件，不会自动清理目录中其他文件；比较实验请指定不同目录。

## 配置文件

配置示例及真实默认值见 [baseline.json](../workload_profiling/config/baseline.json)。配置文件是 JSON，不能加入注释。

| 顶层字段 | 校验与作用 |
| --- | --- |
| `arrival_interval_ms` | 正整数 |
| `batch_size` | 正整数 |
| `batch_wait_ms` | 非负整数 |
| `input_threshold_tokens`、`output_threshold_tokens` | 有限非负数，单位 tokens |
| `window_ms` | 固定 60000，RPM/TPM 分钟窗口，不支持任意窗口 |
| `strategy` | `min_rpm` 或可导入 `module:attribute` |
| `batch_order` | 默认 fifo；支持 shortest_first、longest_first 或可导入 `module:attribute`，旧配置可省略 |
| `endpoints` | 至少一个端点对象，ID 唯一 |

端点字段：

| 字段 | 必填 | 默认 / 校验 |
| --- | --- | --- |
| `endpoint_id` | 是 | 非空字符串、唯一 |
| `rpm_limit` | 是 | 正整数，请求/分钟 |
| `tpm_limit` | 是 | 正整数，token/分钟 |
| `concurrency_limit` | 是 | 正整数，同时在途请求数 |
| `base_latency_ms` | 否 | 5，非负整数 |
| `input_tokens_per_ms` | 否 | 2000，有限正数 |
| `output_tokens_per_ms` | 否 | 20，有限正数 |

复制默认文件到自己的 JSON 后使用 `--config`；不要直接修改 Python 源码来调整实验参数。

## 网页控制台

```powershell
& .\.venv\Scripts\python.exe demo.py --open
& .\.venv\Scripts\python.exe demo.py --port 8766 --open
```

默认本地地址 `http://127.0.0.1:8765`。修改到达间隔、批大小、收集等待、两轴阈值和端点 JSON，并选择批内排序，点击运行；后台回放期间轮询状态，完成后显示端点、请求分页（含 0-based 批内位置）和 CSV 下载。

网页数量默认 200，填 0 转换成 API 的 `limit:null`，代表全部；API 直接传 `limit:0` 无效。网页路由支持内置 `min_rpm`，批内排序支持 fifo/shortest_first/longest_first；自定义 Python 排序与路由用 CLI/Python。网页固定读取默认原始数据，不提供文件上传或 `--source`；自定义文件请用 CLI/Python。

`Ctrl+C` 停止服务；后台回放线程随进程结束，不保证中途关闭时导出结果。HTTP 接口完整说明见 [HTTP API](http-api.md)。

### demo.py 全部模式

| 参数 / 模式 | 用途 |
| --- | --- |
| 无模式参数 | 新 baseline 网页，不依赖 Stage 2/2.1 |
| `--port N` | 指定端口，0 让系统分配可用端口 |
| `--open` | 启动后用默认浏览器打开 |
| `--length-demo` | 旧单条长度页面，需要已完成 Stage 2.1 |
| `--export-only` | 仅导出旧 23 列历史视图，不启动网页；与长度分析产物有关 |

```powershell
& .\.venv\Scripts\python.exe demo.py --length-demo --open
& .\.venv\Scripts\python.exe demo.py --export-only
```

## 当前请求的 JSONL 计数与发送接口

此入口是 `runtime.RequestProcessor` 的 CLI，逐条读、逐条发送/计数并 flush；没有 baseline 的批队列和模拟路由。

```powershell
# 只计当前输入，无 reply 时 output/total 为 null
& .\.venv\Scripts\python.exe -m workload_profiling.runtime.run_stream --input examples/current_requests.jsonl --lengths-only

# 使用演示 sender；更换 module:callable 即可适配自己的同步客户端
& .\.venv\Scripts\python.exe -m workload_profiling.runtime.run_stream --input examples/current_requests.jsonl --lengths-only --sender examples.sender:send_one

# stdin 模式
'{"messages":[{"role":"user","content":"hello"}]}' | & .\.venv\Scripts\python.exe -m workload_profiling.runtime.run_stream --lengths-only

# 保存结果，逐条错误后继续；有错误则最终非零退出
& .\.venv\Scripts\python.exe -m workload_profiling.runtime.run_stream --input examples/current_requests.jsonl --lengths-only --output workload_profiling/cache/current_lengths.jsonl --on-error yield
```

| 参数 | 作用 |
| --- | --- |
| `--input PATH` | 省略则 stdin；每行一个当前请求，不能为空行 |
| `--output PATH` | 省略则 stdout；不得与输入是同一文件，已有输出会覆盖 |
| `--sender module:callable` | 同步发送适配器，接收完整规范化 prompt |
| `--lengths-only` | 不加载冻结 reference/百分位策略 |
| `--threshold X` | 本进程百分位阈值，0<X<1；不能与 `--lengths-only` 同用 |
| `--on-error raise\|yield` | 默认 raise；yield 记录错误并继续，最终非零退出 |
| `--include-response` | 显式将回复原文写入结果，默认不输出 |
| `--help` | 查看帮助 |

不指定 `--lengths-only` 时需要 Stage 2.1 的 reference 与配置快照。带外层 `response` 的当前请求可验证已有回复；不能同时提供 sender。完整会话的历史不会被自动展开。

## 验证项目

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
& .\.venv\Scripts\python.exe -m unittest workload_profiling.tests.test_baseline -v
& .\.venv\Scripts\python.exe -m unittest workload_profiling.tests.test_baseline_ordering -v
& .\.venv\Scripts\python.exe -m unittest workload_profiling.tests.test_baseline_dependencies -v
```

集成测试检查已有本地 tokenizer、smoke 和历史标签；缺少相关产物时会有明确 skip，不代表核心 baseline 失败。test_baseline_ordering 验证批内排序、路由组合、容量限制、非法排列、跨批顺序和导出记录；首次下载的环境可能跳过部分真实产物集成检查。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `No module named workload_profiling/examples` | 从仓库根运行模块，或把仓库根放入调用方导入路径；示例用 `python -m examples.run_baseline` |
| 找不到默认 prompt 文件 | 使用 `--source examples/prompt_requests.jsonl` 或长度示例；网页需要默认原始文件 |
| tokenizer 首次下载失败 | 有网络后重试；固定缓存完整时可离线使用；先跑 lengths 模式不需要 tokenizer |
| pandas DLL 报“应用程序控制策略已阻止此文件” | 这是 Windows 策略拦截二进制加载，不是缺少 PyTorch。默认 baseline 启动与 CSV 导出已不依赖 pandas；历史分析模式仍需要 pandas，须由环境管理方核验其安装与加载权限 |
| tokenizer 校验和不匹配 | 本地文件与锁文件不一致，不要通过改哈希假装一致；重新获取同一固定版本的完整缓存 |
| 序列长于 tokenizer 最大长度提示 | 本项目只计数、不执行模型、不截断；真实模型服务要另行检查上下文限制 |
| Heavy 等待超过 batch_wait_ms | 该参数只约束收集阶段；查看 `capacity_wait_ms` 与容量配置 |
| 更换排序后 CSV 行顺序没变 | CSV 保持输入顺序；查看 batch_position、batches.json 的 dispatch_order 或事件派发顺序 |
| 排序变了，但平均延迟没变 | 容量充足时同批可能同刻派发，整体延迟统计可相同；先核对实际派发顺序，容量紧张时顺序才可能影响排队 |
| 自定义排序报 every batch request_id exactly once | 必须返回完整 ID 排列，不能遗漏、重复或加入其他批的请求 |
| 最终并发为 0，RPM/TPM 仍大于 0 | 请求已完成，但最近 60 秒调度计数尚未过期 |
| 请求被拒绝 | 总 tokens 超过所有端点 TPM 上限；提高容量或换数据，结果内保留原因 |
| 所有批都是 timeout | 当前到达频率与重型比例不足以在期限内凑满批；调小 batch_size 或延长等待 |
| 端口被占用 | 指定 `--port 8766` 等其他可用端口 |
| CSV 写入失败 | 关闭占用目标文件的程序，或更换输出目录 |
| 历史标签 SHA-256 不匹配 | 基表或标签发生变化，按 Stage 1 → 2 → 2.1 重建依赖，见 [历史复现](offline-profiling.md) |
