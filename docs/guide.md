# 使用指南

[项目首页](../README.md) · [架构与数据](architecture.md) · [开发接口](development.md) · [实验说明](experiments.md)

本文件统一安装、接手运行、配置、动态功能和示例说明。命令从项目根目录执行；Linux/macOS 将 `& .\.venv\Scripts\python.exe` 替换为 `.venv/bin/python`。

## 环境安装

已验证 Python 3.12。新机器重新创建虚拟环境，不复制其他机器的 `.venv`。

```powershell
python --version
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖版本只维护于根目录 `requirements.txt`。长度模式不需要 tokenizer；prompt 模式首次下载固定 Qwen tokenizer/config/template，后续校验本地缓存，不下载权重，也不需要 PyTorch。

## 统一命令入口

| 命令 | 用途 |
| --- | --- |
| `python -m workload_profiling replay ...` | 单次回放与导出 |
| `python -m workload_profiling web ...` | 调度网页、旧长度页面或历史 CSV 导出 |
| `python -m workload_profiling stream ...` | 逐条计数或同步 sender |
| `python -m workload_profiling experiment NAME ...` | 对照实验；名称与依赖见实验说明 |

每个命令支持 `--help`。根目录 baseline.py/demo.py 转发入口已删除，请使用 replay/web 子命令。也可直接运行 `workload_profiling.simulation.cli`、`workload_profiling.runtime.cli`、`workload_profiling.web.cli` 和 `stages.*` 模块入口。

## 配置选择

| 配置（位于 `workload_profiling/config/`） | 行为 | 用途 |
| --- | --- | --- |
| `simulation_adaptive.json` | 四档优先级 + 负载分流 + 动态百分位 | 流程图对应的新入口；4档/非繁忙即时、繁忙窗口 |
| `simulation_dynamic_percentile.json` | all + burst + 动态百分位 + light_first_fifo | 当前平级动态入口，RPM/TPM 额度充足 |
| `simulation_all_requests.json` | all + fixed + tokens + 输出最短 | 全请求原额度基线 |
| `simulation_default.json` | heavy_only + fixed + tokens + fifo | 未指定配置时的兼容默认 |
| `simulation_burst.json` | heavy_only + burst | 历史突发场景 |
| `simulation_endpoint_speeds.json` | heavy_only，速度 30/20/10 | 历史异构速度场景 |
| `simulation_priority*.json` | all，合成业务优先级 | 历史优先级对照 |

heavy_only 的轻型零耗时、不占端点；all 的轻重都执行，两种模式的整体延迟不可直接比较。百分位模式要求 all；绝对输出阈值 578 只参与 tokens 模式。

## 自适应调度

`simulation_adaptive.json` 设置 `scheduling_mode=adaptive`、`batch_scope=all`、`priority_assignment=four_level`。只选择端点，路由继续使用 min_rpm；当前所有配置端点都进入基础候选范围。模型支持、接口、健康、Cooldown 硬过滤和 Pareto 尚未实现。模拟仍保留 RPM、TPM、并发的实际容量约束。

| 新参数 | 默认值 | 含义 |
| --- | --- | --- |
| `scheduling_mode` / `--scheduling-mode` | `window` | `window` 保留旧收集模式；`adaptive` 按负载分流 |
| `busy_rpm_threshold` / `--busy-rpm-threshold` | 0.95 | 当前60秒请求数 / RPM上限达到该比例即繁忙 |
| `busy_tpm_threshold` / `--busy-tpm-threshold` | 0.95 | 当前60秒预留Token数 / TPM上限达到该比例即繁忙 |
| `busy_concurrency_reserve` / `--busy-concurrency-reserve` | 3 | 当前并发达到并发上限减此值即繁忙 |
| `priority_assignment` / `--priority-assignment` | `uniform` | 新增 `four_level`，按固定种子生成1～4档，4最高 |
| `batch_order` / `--batch-order` | `fifo` | 新增 `priority_then_light`：档位降序，同档轻型先、同类FIFO |

三项繁忙条件取 OR；所有端点繁忙才判定系统繁忙。比例必须在 `(0,1]`；并发预留数是非负整数，adaptive 模式下必须小于每个端点的并发上限，避免出现零或负的繁忙边界。阈值比较包含等号。

新请求的处理规则：

1. `priority_level=4`：即时通道，候选范围为全部端点，绕过收集窗口和繁忙筛选。
2. 1～3档、系统不繁忙：即时通道，仅向当前不繁忙的端点派发。
3. 1～3档、系统繁忙：进入收集窗口，释放后使用全部端点范围，沿用批内排序和批间FIFO。

即时表示不等待收集窗口；实际容量不足时仍等待容量释放，不抢占在途请求。每次容量恢复先尝试4档等待请求，再尝试普通即时请求，最后处理已释放窗口队列。即时队列允许跳过当前无法派发的请求；普通即时请求重试时仍仅选择不繁忙端点。窗口请求按到达时确定的路径等待原截止时间，繁忙状态恢复不会提前冲刷已有窗口。

四档优先级独立于历史 `base_priority` / `priority_class` 权重和重型折扣。原始 prompt JSONL 与长度 JSONL 都可提供 `"priority_level": 1` 到 `4`；显式值保留，不被模拟覆盖。Python 直接构造时可设置 `priority_level_source="recorded"`，包括显式保留1档。未提供档位且启用 four_level 时，用 `priority_seed + request_id` 的固定哈希均匀分为四档，不受长度或输入读取顺序影响。

```json
{"request_id":"urgent_001","input_tokens":100,"output_tokens":2000,"priority_level":4}
```

48条样例涵盖三条调度路径，不需要 tokenizer，百分位模式需要现有冻结参考：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --config workload_profiling/config/simulation_adaptive.json `
  --source examples/adaptive_requests.jsonl --source-format lengths `
  --busy-rpm-threshold 0.95 --busy-tpm-threshold 0.95 `
  --busy-concurrency-reserve 3 `
  --output-dir workload_profiling/results/simulation/adaptive_example
```

网页中选择“自适应”后可编辑这些阈值和优先级，逐条轨迹展示档位及调度路径。`pressure_threshold_policy.json` 的 `pressure_rules` 仍只控制输出分类的 idle/normal/busy/critical 等级，不决定是否进入窗口；进入窗口由上述三项新阈值和所有端点的繁忙状态决定。即时路径在到达时分类，窗口路径在批释放时分类，标签随后冻结。

## 配置文件命名迁移

场景配置统一使用 `simulation_` 前缀。文件内容中的实验数值保持原样；旧文件名已移除，调用脚本需使用下表的新路径。

| 旧文件名 | 新文件名 |
| --- | --- |
| `baseline.json` | `simulation_default.json` |
| `baseline_all_requests.json` | `simulation_all_requests.json` |
| `baseline_burst.json` | `simulation_burst.json` |
| `baseline_endpoint_speed.json` | `simulation_endpoint_speeds.json` |
| `baseline_priority.json` | `simulation_priority.json` |
| `baseline_priority_ample_quota.json` | `simulation_priority_high_quota.json` |
| `baseline_priority_burst_321.json` | `simulation_priority_burst_321.json` |
| `baseline_dynamic_output.json` | `simulation_dynamic_percentile.json` |
| `runtime_policy.json` | `output_percentile_policy.json` |
| `dynamic_output_policy.json` | `pressure_threshold_policy.json` |

`output_percentile_policy.json` 是通用输出重型阈值配置；`pressure_threshold_policy.json` 是模拟分类器的压力等级规则与阈值映射。stream 和请求长度网页继续默认读取历史 Stage 2.1 的 `runtime_policy_config.json` 快照，修改源配置不会自动修改快照。该快照及历史报告中的原始文件名保留用于追溯。

最终回放参数的优先级是显式 CLI 覆盖 > 所选场景 JSON > `SimulationConfig` / `EndpointConfig` 中的字段缺省值。未指定 `--config` 时加载 `simulation_default.json`；至少一个端点及其 ID、RPM、TPM、并发上限必须提供。最终生效参数保存在本轮输出目录的 `config.json`。

## 最小样例与当前主线

```powershell
# 预期完成 9、拒绝 0、端点执行数 9
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --config workload_profiling/config/simulation_all_requests.json `
  --source examples/length_requests.jsonl --source-format lengths `
  --output-dir workload_profiling/results/simulation_example/all_requests

# 全量原始 3168 行；可先加 --limit 200
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --config workload_profiling/config/simulation_dynamic_percentile.json `
  --output-dir workload_profiling/results/simulation/dynamic

# 相同环境固定 80% 对照
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --config workload_profiling/config/simulation_dynamic_percentile.json `
  --output-classification percentile_fixed --output-percentile-threshold 0.8 `
  --output-dir workload_profiling/results/simulation/fixed_80
```

原始 JSONL 每行提供 `prompt.messages` 和外层 `response`，完整历史进入 input，每行只产生一次请求。长度 JSONL 每行提供 `input_tokens`、`output_tokens`，可选 `request_id` 和优先级字段。

默认路径相对代码位置，显式相对路径按调用方工作目录。每轮选择独立结果目录：普通 replay 替换同名标准产物，不清理其他文件；历史对照脚本通常要求输出根目录不存在。

## 回放参数

CLI 覆盖仅对本轮生效。参数错误退出 2；有拒绝仍导出结果并退出 2；成功无拒绝退出 0，其他运行异常可能退出 1。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--config PATH` | 包内 `config/simulation_default.json` | 自定义完整配置 |
| `--source PATH` | `prompt数据/prompt回答数据包_3168条/prompt回答.jsonl` | 原始或长度 JSONL |
| `--source-format prompt\|lengths` | `prompt` | 数据解析方式 |
| `--output-dir PATH` | 包内 `results/simulation/` | 结果目录 |
| `--limit N` | 全部 | 正整数，只读取前 N 条；CLI 的 0 无效 |
| `--arrival-interval-ms N` | 配置中的 1 | 正整数；fixed 为实际间隔，burst 为确定总跨度与 EOF 的标称间隔 |
| `--arrival-mode fixed\|burst` | fixed | 固定或确定性突发到达，见 [突发到达](experiments.md) |
| `--burst-size N` | 512 | 每组最多请求数，至少 2 |
| `--burst-span-ms N` | 20 | 组内原始跨度；全表归一化后实际跨度可能不同 |
| `--batch-size N` | 配置中的 16 | 正整数，当前 batch_scope 的收集数量 |
| `--batch-wait-ms N` | 配置中的 20 | 非负整数，最老成员收集等待；0 立即释放 |
| `--batch-scope heavy_only\|all` | heavy_only | 旧模式仅重型入窗、轻型零耗时；all 为所有请求同窗并执行，见 [交接说明](experiments.md) |
| `--priority-assignment uniform\|synthetic\|four_level` | uniform | uniform不生成；synthetic生成历史业务权重；four_level生成1～4档；显式值保留 |
| `--priority-seed N` | 20261006 | 非负整数，合成优先级标签种子 |
| `--priority-high-weight X`、`--priority-normal-weight X`、`--priority-low-weight X` | 10/3/1 | 合成业务标签权重，高>普通>低>0；显式源字段不覆盖 |
| `--heavy-priority-discount X` | 1 | 0<X<=1；有效优先级=基础优先级乘重型系数；仅 effective_priority 排序使用 |
| `--output-classification tokens\|percentile_fixed\|percentile_dynamic` | tokens | 输出判定模式；百分位模式要求所有请求入窗，参考文件固定，见交接说明 |
| `--output-percentile-threshold X` | 0.8 | (0,1)内固定门槛或动态初始状态；动态首次采样立即按映射更新 |
| `--output-policy PATH` | 包内pressure_threshold_policy.json | 百分位模式的门槛/压力规则JSON；见 [动态功能](guide.md) |
| `--output-reference PATH`、`--output-reference-metadata PATH` | 包内冻结参考与metadata | 成对指定参考Parquet和metadata，校验哈希与样本数 |
| `--input-threshold X` | 配置中的 40342.5 | 有限非负数，tokens，包含等号 |
| `--output-threshold X` | 配置中的 578 | 有限非负数，tokens，包含等号 |
| `--strategy SPEC` | 配置中的 `min_rpm` | 内置策略或 `module:attribute` |
| `--batch-order SPEC` | 配置中的 `fifo` | fifo/shortest_first/longest_first/effective_priority/light_first_fifo/priority_then_light 或 `module:attribute`；控制批内派发顺序 |
| `-h`、`--help` | — | 帮助后退出 |

## 配置文件

复制场景 JSON 后通过 `--config` 指定，JSON 不含注释。`endpoints` 非空且 ID 唯一；RPM/TPM/并发为正整数，窗口固定 60000ms。端点字段如下：

| 字段 | 必填 | 默认 / 校验 |
| --- | --- | --- |
| `endpoint_id` | 是 | 非空字符串、唯一 |
| `rpm_limit` | 是 | 正整数，请求/分钟 |
| `tpm_limit` | 是 | 正整数，token/分钟 |
| `concurrency_limit` | 是 | 正整数，同时在途请求数 |
| `base_latency_ms` | 否 | 5，非负整数 |
| `input_tokens_per_ms` | 否 | 2000，有限正数 |
| `output_tokens_per_ms` | 否 | 20，有限正数 |

`service_jitter_fraction` 默认 0，必须 0<=fraction<1；`service_jitter_seed` 默认 20261005，为非负整数。波动使用请求/端点/种子的稳定哈希，见架构说明。

## 动态分类与资源复现

默认冻结 ECDF 包含 5,186 个历史展开样本，来自当前底层语料，不能视作独立测试集。必需资源：

```text
workload_profiling/data/artifacts/output_percentile_reference.parquet
workload_profiling/results/stage2_1/percentile_reference_metadata.json
workload_profiling/config/pressure_threshold_policy.json
```

窗口释放或即时请求到达时，读取当前并发、已释放窗口与两个即时通道中的等待数，以及本次分类的已到达需求，映射到 idle/normal/busy/critical，默认门槛为 90%/80%/70%/60%。输出 ECDF>=门槛即输出重型，与固定输入判定取 OR；标签随后冻结。初始 80% 可在首次采样立即改变；当前无平滑或滞回。这里的分类压力等级与决定调度通道的系统繁忙判定相互独立。

`--output-policy PATH` 指定映射和压力规则。门槛在 (0,1)，压力越高门槛不能增大；并发规则 0<=normal<busy<=1，critical_excess_ratio 为有限正数：

```json
{
  "default_threshold": 0.8,
  "congestion_threshold_mapping": {"idle":0.9,"normal":0.8,"busy":0.7,"critical":0.6},
  "percentile_definition": "empirical_cdf_right",
  "reference_sample_count": null,
  "pressure_rules": {"normal_concurrency_threshold":0.6,"busy_concurrency_threshold":0.85,"critical_excess_ratio":1.0}
}
```

自定义参考成对传入 `--output-reference` 和 `--output-reference-metadata`。配置内相对资源路径以配置所在目录为基准；CLI 资源路径以调用方目录为基准。

百分位模式自动导出规则、参考副本、逐批轨迹与 `replay_config.json`。使用相同源输入及导出配置可复现；目录不复制原始请求或 Python 依赖：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --config workload_profiling/results/simulation/dynamic/replay_config.json `
  --output-dir workload_profiling/results/simulation/dynamic_replay
```

原运行若有 `--source`、`--source-format` 或 `--limit`，重放指定相同值。结果字段见架构说明。

## 网页与逐条接口

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling web --open
& .\.venv\Scripts\python.exe -m workload_profiling web --port 8766 --open
& .\.venv\Scripts\python.exe -m workload_profiling web --length-demo --open
& .\.venv\Scripts\python.exe -m workload_profiling web --export-only

& .\.venv\Scripts\python.exe -m workload_profiling stream `
  --input examples/current_requests.jsonl --lengths-only
& .\.venv\Scripts\python.exe -m workload_profiling stream `
  --input examples/current_requests.jsonl --lengths-only --sender examples.sender:send_one
```

网页绑定 127.0.0.1:8765，默认原始文件、window、heavy_only、前 200 条；可切换 adaptive、四档优先级、百分位分类，并修改繁忙阈值。路由使用 min_rpm，排序支持内置策略（包括 priority_then_light）；网页不加载外部 Python 插件。选择 adaptive 或百分位分类时自动使用 batch_scope=all。界面数量 0 转为 API null，直接 API 不接受 0。端口 0 由系统分配；Ctrl+C 停止服务，任务不跨进程恢复。

length-demo/export-only 依赖 Stage 2.1；export-only 导出历史 23 列视图。HTTP 契约见开发接口。

stream 的 input/output 省略为 stdin/stdout，每条结果 flush，输入输出不得同文件。无回复时 output/total=null。`--on-error yield` 记录错误后继续，最终非零退出；`--include-response` 显式输出回答。`--threshold X` 要求 0<X<1，不能与 lengths-only 同用。未选 lengths-only 需历史百分位参考。stream 独立同步计数/发送，不使用模拟批队列。

## 示例与策略

examples 包含四份小型 JSONL、run_simulation、min_tpm、输出升/降序策略和 sender。`adaptive_requests.jsonl` 是48条显式四档样例；`python -m examples.run_simulation` 展示旧模式：9 条逻辑完成，4 条消耗端点。

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --source examples/length_requests.jsonl --source-format lengths `
  --batch-size 2 --batch-wait-ms 3 `
  --batch-order examples.output_longest_first:OutputLongestFirstOrder `
  --strategy examples.min_tpm:MinTpmStrategy `
  --output-dir workload_profiling/results/simulation_custom/output_longest_first
```

shortest_first 按 input+output 排序；`examples.output_shortest_first:OutputShortestFirstOrder` 仅按 output。Python 注入见开发接口；对照脚本统一位于 experiments。

## 验证与排错

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
```

缺少 tokenizer 或历史产物时，相关集成测试明确 skip。长度回放可以独立验证。

| 现象 | 处理 |
| --- | --- |
| 模块找不到 | 从项目根运行 python -m，或把根目录加入导入路径 |
| 默认数据不存在 | 指定长度示例及 source-format lengths |
| tokenizer 下载失败 | 先长度模式，网络恢复后获取固定缓存 |
| tokenizer/标签哈希不匹配 | 恢复原资源或按阶段依赖重建，不改哈希冒充一致 |
| PyTorch/超长上下文提示 | 仅计数可以运行；真实模型需另检查上下文 |
| 等待超过 batch_wait_ms | 它只约束收集阶段，检查 capacity_wait_ms |
| CSV 顺序没随排序改变 | 查看 dispatch_order、batch_position 与派发事件 |
| 所有批都超时 | 到达率/重型比例不足，调整批大小和等待 |
| 请求拒绝 | 总 tokens 超过所有端点 TPM 上限，结果记录原因 |
| 并发归零但额度非零 | 最近 60 秒调度记录尚未过期 |
| 文件/端口占用 | 关闭占用程序或更换输出目录/端口 |
| 对照实验缺 reference | 未附全部历史结果，先 replay，再按实验依赖重建 |
