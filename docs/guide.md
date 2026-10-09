# 使用指南

[项目首页](../README.md) · [架构](architecture.md) · [开发接口](development.md)

## 配置分层

配置位于 `workload_profiling/config/`：

| 文件 | 管什么 |
| --- | --- |
| endpoint_config.json | 静态端点：模型、接口、地址/部署映射、RPM/TPM/并发硬上限、价格、上下文 |
| scheduler_config.json | 繁忙阈值、窗口、排序、路由、分类、冷却和历史窗口 |
| simulation_adaptive.json | 引用上述两文件；发送时刻、优先级生成、输出预测、SLO、模拟服务时间 |
| pressure_threshold_policy.json | 输出分类的 pressure_rules 和百分位映射 |

动态 RPM/TPM/并发、健康、失败及冷却只在运行状态中维护，不写回策略文件。结果 `endpoints.json` 展示动态状态，`endpoint_history.json` 展示近期性能。它们与端点静态配置用途不同。

simulation_adaptive.json 中的路径相对自身目录解析。scheduler_config.json 的参考/策略路径相对该文件解析。CLI 路径相对启动目录。旧版包含全部字段的平铺配置及导出的 config.json/replay_config.json 仍可读取；分层配置禁止在模拟文件重复覆盖调度字段，以免两处设置互相矛盾。

Python 类预设值是缺省项，文件明确设置的值覆盖它们。独立 SchedulerConfig 默认 tokens 分类，便于核心脱离参考文件运行；项目 scheduler_config.json 明确指定 percentile_dynamic，延续原模拟基线。SimulationConfig 是保留旧 API 的组合配置，scheduler_config() 把其中调度参数投影到核心，不产生第二份动态状态。

## 运行

从项目根目录运行：

```powershell
# 不需要 tokenizer 的已有长度样例
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --source examples/adaptive_requests.jsonl --source-format lengths `
  --output-dir workload_profiling/results/simulation/adaptive_example

# 默认回放全部原始请求；或 --limit 200
& .\.venv\Scripts\python.exe -m workload_profiling replay
& .\.venv\Scripts\python.exe -m workload_profiling web --open

# 新的二档优先级、随机发送、加权排序和固定输出估计
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --source examples/adaptive_requests.jsonl --source-format lengths `
  --priority-assignment binary --high-priority-ratio 0.1 `
  --arrival-mode random --random-min-interval-ms 1 --random-max-interval-ms 10 `
  --batch-order weighted_length --input-weight 1 --output-weight 2 `
  --prediction-mode fixed --predicted-output-tokens 500 `
  --output-dir workload_profiling/results/simulation/binary_random
```

默认原始来源是 3168 条完整 prompt/response。prompt 模式使用固定 Qwen/Qwen3-8B tokenizer，只下载 tokenizer，不下载模型权重；lengths 模式不需 tokenizer。百分位分类读取已有冻结参考。

`--config PATH` 选择模拟配置；`--source`、`--source-format prompt|lengths`、`--output-dir` 更换输入和结果目录。省略 --limit 回放全部；CLI/API limit 必须是正整数，网页的 0 表示全部。`--help` 列出所有参数。

## 调度参数

下表字段写在 scheduler_config.json。括号中是旧组合配置及 CLI 名称：

| 字段 | 项目默认值 | 含义 / CLI |
| --- | --- | --- |
| max_batch_size | 16 | 窗口请求数量上限（batch_size / --batch-size） |
| max_wait_ms | 20 | 最长收集等待，不包含容量等待（batch_wait_ms / --batch-wait-ms） |
| busy_rpm_threshold / busy_tpm_threshold | .95 / .95 | 利用率繁忙边界，范围 (0,1] |
| busy_concurrency_reserve | 3 | 繁忙并发数 = 上限减预留数 |
| busy_concurrency_threshold | null | 可选并发比例 (0,1]；设定后替代 reserve 判据 |
| cooldown_ms | 1000 | 端点/限流失败后的冷却时间 |
| ranking_policy | priority_then_light | 保留默认排序；可选 fifo、weighted_length、module:attribute（batch_order / --batch-order） |
| input_weight / output_weight | 1 / 1 | weighted_length 的输入和预测输出权重，非负且不能同时为零 |
| ranking_direction | ascending | 加权排序 ascending 或 descending，同分保持到达顺序 |
| routing_policy | min_rpm | 候选中 RPM 利用率最低；支持 module:attribute（strategy / --strategy） |
| history_window_ms / history_max_samples | 60000 / 10000 | 每端点历史时间窗与最大样本数 |
| input_threshold_tokens | 40342.5 | 固定输入重型阈值；--input-threshold |
| output_threshold_tokens | 578 | tokens 模式输出重型阈值；--output-threshold |
| output_classification | percentile_dynamic | tokens / percentile_fixed / percentile_dynamic |
| output_percentile_threshold | .8 | 初始或固定百分位，范围 (0,1) |
| output_policy_path | pressure_threshold_policy.json | 输出分类策略；--output-policy |
| output_reference_path / output_reference_metadata_path | null / null | 默认参考或成对指定文件；--output-reference / --output-reference-metadata |
| window_ms | 60000 | RPM/TPM 固定 60 秒滚动窗，不能随意改成其他周期 |

除表中别名外，CLI 通常把字段下划线换为连字符，例如 --cooldown-ms、--history-window-ms、--input-weight。reserve 必须小于每端点并发上限；设定并发比例时不使用此限制。布尔值不接受为整数。模型池内每端点的三个繁忙条件取 OR，整个健康候选池取 AND。

## 模拟参数与端点

| simulation_adaptive.json 字段 | 默认值 | 含义 |
| --- | --- | --- |
| arrival_mode / arrival_interval_ms | burst / 1 | fixed、random、burst；标称间隔 |
| burst_size / burst_span_ms | 512 / 20 | 组大小与压缩跨度 |
| arrival_seed / random_min_interval_ms / random_max_interval_ms | 20261008 / 1 / 10 | 独立发送种子与随机整数间隔范围 |
| priority_assignment / priority_seed | four_level / 20261008 | 保留旧分布；可选 binary 或 uniform |
| high_priority_ratio | .1 | binary 模式中高优先级比例，范围 [0,1] |
| target_model / stream / max_tokens | default / false / null | 请求缺省模型、流式标志和输出上限 |
| slo | null | 可选 ttft_ms、tpot_ms、e2e_ms；上游已提供时优先使用 |
| prediction_mode / predicted_output_tokens | oracle / 500 | oracle 使用录制答案长度；fixed 使用固定估计，上游显式预测始终优先 |
| service_time | 5 / 2000 / 20 | 基础 ms、输入 tokens/ms、实际输出 tokens/ms |
| service_jitter_fraction / service_jitter_seed | 0 / 20261005 | 放在 service_time 内；有界、可复现的服务波动 |
| endpoint_service | {} | 按 endpoint_id 覆盖模拟服务参数，不能放健康或配额状态 |

fixed 第 i 条在 i×interval 到达。random 从 0 开始，后续间隔在闭区间内按 arrival_seed 生成。burst 把组内请求压缩，再归一化保持整批首末到达时刻，允许同毫秒多条；标称间隔决定总跨度。新模式不会替换默认 burst 参数。

二档生成按 priority_seed/request_id 固定哈希，与长度和读取顺序无关。旧 four_level 模拟仍按档位排序：4→核心1，其余→核心0；旧档位只留在模拟层。显式 priority=0/1 优先于生成器。uniform 不生成优先级，默认普通请求；保留显式旧档位。Python 显式旧1档应使用 priority_level_source="recorded"。

端点静态文件不能包含模拟速度、当前并发或冷却。supported_models 匹配 target_model，api_types 匹配 api_type；context_limit 检查输入加 max_tokens（未设置则加预测输出）。默认模型名 default、价格0、地址/部署映射 null 都是沿用基线的模拟占位，需要真实接入时填写实际模型及部署。价格单位为每百万 token，输入/输出分别配置，所有端点应使用同一币种。

## 请求格式与预测口径

真实核心请求不需要实际输出，格式见开发接口。模拟 lengths 输入每行至少包含 input_tokens、output_tokens，后者代表录制实际输出；可以附加预测、核心优先级、消息、模型、SLO 等：

```json
{"request_id":"urgent_001","input_tokens":100,"predicted_output_tokens":300,"output_tokens":2000,"priority":1,"target_model":"default","messages":[{"role":"user","content":"hi"}],"stream":true,"max_tokens":4096}
```

预测用于分类、排序及派发预留；实际输出只决定模拟服务时间和反馈。默认 oracle 是保留既有基线的离线先验，不是真实输出预测模型。改用 fixed 或提供上游预测后，应单独分析预测误差对容量等待和用量超限的影响。

高优先级跳过窗口但不绕过硬过滤。无模型/接口/上下文兼容端点时拒绝，健康/冷却/资源暂不可用则等待恢复。普通即时请求在重试时重新计算非繁忙集合；已进入窗口的请求不因负载降低而提前释放。请求不抢占在途执行。

## pressure_rules 与调度繁忙

调度繁忙读取目标模型池的 RPM、TPM、并发，决定路径。pressure_rules 用于输出分类门槛：

- U = 在途并发 / 总并发上限。
- E = max(0，已释放窗口及即时通道等待数 + 本次分类请求数 − 空闲并发名额)。
- E≥总名额×critical_excess_ratio 时为 critical；否则 E>0 或 U 达 busy 边界为 busy，再比较 normal 边界。
- 默认 idle/normal/busy/critical 百分位门槛为 .9/.8/.7/.6。

ECDF 达门槛为输出重型，再与输入重型取 OR。分类在即时到达或窗口释放时冻结，不改变高优先级路径。固定百分位和绝对 tokens 模式保留用于受控验证。

## 网页与结果

网页保持独立本地回放，支持三种发送、两种新旧优先级分布、内置排序、预测、分类、繁忙和冷却参数；端点组合 JSON 可编辑。未展示的参数仍可通过配置/CLI/Python 修改。网页不导入外部 Python 策略；默认 127.0.0.1:8765，--port 可修改，0 自动分配。

输出含 config.json、requests.csv、events.jsonl、batches.json、endpoints.json、endpoint_history.json、summary.json、report.md。百分位模式另外输出轨迹、策略/参考快照和独立 replay_config.json。导出完整组合配置便于单文件回放。

CSV 中 priority 是核心0/1，priority_level 是模拟旧档位；predicted_output_tokens 与 actual_output_tokens 分列。scheduling_path、system_busy_at_arrival、busy_endpoints_at_arrival、routing_candidate_ids 可核对路径与候选。即时请求批字段为空。queue_wait_ms = batch_wait_ms + capacity_wait_ms；成功请求 latency_ms = queue_wait_ms + service_ms。端点 ID 数组列使用 JSON。

容量等待可超过 max_wait_ms。RPM/TPM 按派发时刻精确满60秒过期；模拟没有新到达也会推进到恢复事件。真实网关需调用 tick 推进定时器，参见开发接口。超出全部兼容端点 TPM 硬上限的请求明确拒绝，CLI 有拒绝时退出码2。失败反馈另计 failed_requests；未知流式指标为 null。
