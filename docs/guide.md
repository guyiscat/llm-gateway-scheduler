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

动态 RPM/TPM/并发、健康、失败及冷却只在运行状态中维护，不写回策略文件。状态及近期性能供路由读取，Python API 可在返回的 RunResult 中检查。

simulation_adaptive.json 中的路径相对自身目录解析。scheduler_config.json 的参考/策略路径相对该文件解析。CLI 路径相对启动目录。包含全部字段的平铺配置仍可读取；分层配置禁止在模拟文件重复覆盖调度字段，以免两处设置互相矛盾。

Python 类预设值是缺省项，文件明确设置的值覆盖它们。独立 SchedulerConfig 默认 tokens 分类，便于核心脱离参考文件运行；项目 scheduler_config.json 明确指定 percentile_dynamic，延续原模拟基线。SimulationConfig 是保留旧 API 的组合配置，scheduler_config() 把其中调度参数投影到核心，不产生第二份动态状态。

## 运行

从项目根目录运行：

```powershell
# 使用本地原始请求试运行前200条
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --limit 200 --output workload_profiling/results/routes.jsonl

# 默认回放全部原始请求；或 --limit 200
& .\.venv\Scripts\python.exe -m workload_profiling replay

# 使用原始请求前200条，设置二档优先级、随机发送、加权排序和固定输出估计
& .\.venv\Scripts\python.exe -m workload_profiling replay --limit 200 `
  --priority-assignment binary --high-priority-ratio 0.1 `
  --arrival-mode random --random-min-interval-ms 1 --random-max-interval-ms 10 `
  --batch-order weighted_length --input-weight 1 --output-weight 2 `
  --prediction-mode fixed --predicted-output-tokens 500 `
  --output workload_profiling/results/binary_random.jsonl
```

默认原始来源是 3168 条完整 prompt/response。prompt 模式使用固定 Qwen/Qwen3-8B tokenizer，只下载 tokenizer，不下载模型权重；lengths 模式不需 tokenizer。百分位分类读取已有冻结参考。

`--config PATH` 选择模拟配置；`--source`、`--source-format prompt|lengths` 更换输入；`--output PATH.jsonl` 指定路由记录文件。省略 --limit 回放全部；CLI/API limit 必须是正整数。`--help` 列出所有参数。

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
| target_model / stream / max_tokens | deepseek-flash / false / null | 请求缺省模型、流式标志和输出上限；显式请求值优先 |
| slo | null | 可选 ttft_ms、tpot_ms、e2e_ms；上游已提供时优先使用 |
| prediction_mode / predicted_output_tokens | oracle / 500 | oracle 使用录制答案长度；fixed 使用固定估计，上游显式预测始终优先 |
| service_time | 5 / 2000 / 20 | 基础 ms、输入 tokens/ms、实际输出 tokens/ms |
| service_jitter_fraction / service_jitter_seed | 0 / 20261005 | 放在 service_time 内；有界、可复现的服务波动 |
| endpoint_service | {} | 按 endpoint_id 覆盖模拟服务参数，不能放健康或配额状态 |

fixed 第 i 条在 i×interval 到达。random 从 0 开始，后续间隔在闭区间内按 arrival_seed 生成。burst 把组内请求压缩，再归一化保持整批首末到达时刻，允许同毫秒多条；标称间隔决定总跨度。新模式不会替换默认 burst 参数。

二档生成按 priority_seed/request_id 固定哈希，与长度和读取顺序无关。旧 four_level 模拟仍按档位排序：4→核心1，其余→核心0；旧档位只留在模拟层。显式 priority=0/1 优先于生成器。uniform 不生成优先级，默认普通请求；保留显式旧档位。Python 显式旧1档应使用 priority_level_source="recorded"。

端点静态文件不能包含模拟速度、当前并发或冷却。supported_models 匹配每条请求的 target_model，api_types 匹配 api_type；context_limit 检查输入加 max_tokens（未设置则加预测输出）。当前默认模型及默认端点支持列表均为 deepseek-flash。价格0、地址/部署映射 null 仍需按实际部署补充；模型字段不再使用 default 占位。价格单位为每百万 token，输入/输出分别配置，所有端点应使用同一币种。

## 请求格式与预测口径

真实核心请求不需要实际输出，格式见开发接口。模拟 lengths 输入每行至少包含 input_tokens、output_tokens，后者代表录制实际输出；可以附加预测、核心优先级、消息、模型、SLO 等：

```json
{"request_id":"urgent_001","input_tokens":100,"predicted_output_tokens":300,"output_tokens":2000,"priority":1,"target_model":"deepseek-flash","messages":[{"role":"user","content":"hi"}],"stream":true,"max_tokens":4096}
```

预测用于分类、排序及派发预留；实际输出只决定模拟服务时间和反馈。默认 oracle 是保留既有基线的离线先验，不是真实输出预测模型。改用 fixed 或提供上游预测后，应单独分析预测误差对容量等待和用量超限的影响。

每条请求可以明确指定自己的 target_model。prompt 格式优先读取顶层 target_model，其次 prompt.target_model、prompt.model；lengths 格式读取 target_model，其次 model。缺失或 null 时才使用模拟配置默认值 deepseek-flash，显式非法值会拒绝。SchedulerRequest 始终有非空 target_model。发送入口无需 --model，按交付记录的 target_model 逐条发送；其他模型需同时配置端点支持列表与远端部署。

高优先级跳过窗口但不绕过硬过滤。无模型/接口/上下文兼容端点时拒绝，健康/冷却/资源暂不可用则等待恢复。普通即时请求在重试时重新计算非繁忙集合；已进入窗口的请求不因负载降低而提前释放。请求不抢占在途执行。

## pressure_rules 与调度繁忙

调度繁忙读取目标模型池的 RPM、TPM、并发，决定路径。pressure_rules 用于输出分类门槛：

- U = 在途并发 / 总并发上限。
- E = max(0，已释放窗口及即时通道等待数 + 本次分类请求数 − 空闲并发名额)。
- E≥总名额×critical_excess_ratio 时为 critical；否则 E>0 或 U 达 busy 边界为 busy，再比较 normal 边界。
- 默认 idle/normal/busy/critical 百分位门槛为 .9/.8/.7/.6。

ECDF 达门槛为输出重型，再与输入重型取 OR。分类在即时到达或窗口释放时冻结，不改变高优先级路径。固定百分位和绝对 tokens 模式保留用于受控验证。

## 本地路由记录

默认路由交付文件为 `workload_profiling/results/routes.jsonl`，UTF-8 JSONL，每行对应一次实际派发，按派发顺序排列。已选择但尚未获准派发、排队和拒绝请求不写入路由文件；完整请求另写入 `results/logs/` 的运行日志。

| 字段 | 含义 |
| --- | --- |
| request_id | 原请求 ID，便于关联上游 |
| target_model | 标准请求携带的目标模型，发送层据此设置 model |
| selected_endpoint_id | 路由最终选择的端点 |
| dispatched_at_ms | 本次运行时间轴上的派发毫秒，模拟中为虚拟时间 |
| litellm_params | LiteLLMAdapter.build_params 的调用参数字典 |

调用参数包括 model、messages、stream、metadata，以及请求提供的 max_tokens 和工具/生成控制项；所选端点配置了 api_base 或 deployment_model 时会转换为对应部署参数。记录不包含录制答案、模拟结果或实验报表。lengths 来源若未提供 messages，记录中的消息列表为空，仅适合检验调度；实际交付请求需提供有效消息。

运行过程中逐条写入同目录临时文件，完成模拟并核对输入哈希后一次替换正式文件；异常会清理临时文件并保留原输出。默认每次覆盖，不追加跨次运行记录；需要保留多次记录时为 --output 指定不同文件名。输出路径禁止与输入相同，已存在的同一文件别名也会拒绝。

读取前两条交付记录：

```powershell
Get-Content -Encoding UTF8 workload_profiling/results/routes.jsonl -TotalCount 2
```

模拟反馈仍用于释放并发、修正 TPM 和维护历史。RunResult 中的 requests/events/batches/summary 等仅供内存分析，CLI 只打印记录数、路径和拒绝数。配置通过原四份 JSON 或 CLI 修改。

需要向部署的 LiteLLM 发送请求时，使用框架之外的 [独立接入工具](litellm_gateway.md)。默认只预览，显式 --send 才通过 SSH 发出 POST，并在终端显示完整响应正文；流式请求实时显示 SSE 事件。加 --session 可在一次登录后交互连续发送，端点组可省略且没有默认值。一次发送限制已移除，默认回放仍不会调用该工具。

需要在一次命令中完成“模拟 N 条 → 发送到 LiteLLM”，使用集成入口：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling replay-send `
  --limit 3 --ssh-target ubuntu@118.195.173.231
```

此命令启动时立即登录，之后才加载配置、数据和 tokenizer。原模拟完整运行后，按本次 routes.jsonl 的实际派发顺序发送，全部请求共用一个 SSH 隧道和 API Key。N 默认 3，可改 --limit；可传原 --source、--source-format、--config、--output，调度规则通过原配置修改。它直接发送真实请求，无须 --send；--dry-run 只模拟及校验，不登录、不发送。--endpoint-group 可省略，输出上限控制同接入工具。失败停止后续发送，无自动重试或续发；真实响应不回写模拟状态。

容量等待可超过 max_wait_ms。RPM/TPM 按派发时刻精确满60秒过期；模拟没有新到达也会推进到恢复事件。真实网关需调用 tick 推进定时器，参见开发接口。超出全部兼容端点 TPM 硬上限的请求明确拒绝，CLI 有拒绝时退出码2。失败反馈另计 failed_requests；未知流式指标为 null。

## 标准请求与真实响应日志

replay 和 replay-send 默认在 results/logs 新建运行目录，终端显示路径。`--log-dir PATH` 可指定尚不存在的目录。每条请求单独保存到 `requests/<请求文件名>.jsonl`，各行只属于同一 request_id；`run.jsonl` 只记录运行开始、结束和运行级错误。scheduler_request 事件包含完整 SchedulerRequest；simulation_outcome 表示本地模拟状态；真实发送时另记录 litellm_send_started 的实际 payload、litellm_response 的 HTTP 状态及完整响应、SSE 片段和发送异常。被拒绝请求也有标准请求文件。默认请求文件名为 request_000000.jsonl 等，自定义 ID 使用安全文件名，原始 ID 保留在正文。

每条日志即时落盘，中断不删除已写记录，凭据脱敏。普通 replay 或 --dry-run 没有真实响应事件；手动 gateway --send 记录发送与响应，不重建完整标准请求。记录格式和 PowerShell 读取方式见 [运行日志](litellm_gateway.md#运行日志)。
