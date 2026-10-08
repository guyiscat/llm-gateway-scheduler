# 架构、数据与结果

[项目首页](../README.md) · [使用指南](guide.md) · [开发接口](development.md) · [实验说明](experiments.md)

## 目录与依赖

```text
CLI replay ─→ simulation.cli.execute ─→ source ─→ SimulationRunner
                                              ├─ classification / ordering
                                              ├─ routing / EndpointState
                                              └─ reporting
CLI web ────→ web.cli ─→ web.simulation_console / web.profile_console
CLI stream ─→ runtime.RequestProcessor ───────→ 可选同步 sender
CLI experiment ─→ experiments ────────────────→ 同一 simulation 引擎
stages ─────→ 历史长度基表 / 标签 / 冻结参考
```

| 目录 | 职责 |
| --- | --- |
| `workload_profiling/simulation/` | 仿真配置、来源、到达、负载监测、调度分流、分类、排序、路由和导出 |
| `workload_profiling/web/` | 两种本地控制台；`templates/` 保存 HTML/CSS/JS |
| `workload_profiling/runtime/` | 当前请求计数、sender、ECDF 与输出策略 |
| `workload_profiling/common/` | 固定 tokenizer、规范化、路径、I/O、数据关联与统一延迟统计 |
| `workload_profiling/stages/` | 历史 Stage 1 / 2 / 2.1，独立于回放入口 |
| `workload_profiling/config/` | 兼容默认、当前主线、历史场景和策略 JSON |
| `examples/` | 小型数据与可导入的排序/路由/sender 示例 |
| `experiments/` | 对照实验和独立诊断，不作为生产策略实现 |
| `workload_profiling/tests/` | 行为、接口、真实缓存与科学产物集成检查 |

根目录只保留统一模块入口的使用说明，baseline.py/demo.py 转发文件已删除。依赖版本只维护于根目录 requirements.txt。统一入口按子命令加载所需模块。

数据及产物仍沿用原位置：

| 路径 | 内容与使用条件 |
| --- | --- |
| `prompt数据/` | 默认 3168 行原始 prompt/response；可用 source 替代 |
| `实验数据2/` | 历史网关流量、价格、容量资料；simulation 不读取 |
| `data/tokenizer/Qwen3-8B/` | 仅 tokenizer/config/template，无权重；prompt 计数需要 |
| `data/processed/` | 唯一长度基表与两份标签 Parquet；历史分析/长度网页使用 |
| `data/artifacts/` | 冻结输出 ECDF；百分位分类需要 |
| `data/exports/` | 可重建历史 CSV 副本 |
| `results/stage*/` | 科学报告、metadata、参考元数据，保留原实验 provenance |
| `results/simulation/` | 默认 CLI 结果；网页位于 web/run_id 子目录 |
| `results/reproduction/` | 本地历史对照结果；当前交付未附全部目录 |
| `cache/` | smoke、测试临时文件、可重建缓存 |

表中 data/results/cache 均位于 workload_profiling 下。Git 忽略虚拟环境、tokenizer、缓存、CSV 副本和默认运行产物；已跟踪文件不会因 gitignore 自动消失，自定义输出目录也不一定被忽略。

## 三种请求口径

| 流程 | 每条数据的含义 | 展开历史 assistant |
| --- | --- | --- |
| replay prompt | 原始一行完整上下文调用；当前 3168 请求 | 否 |
| Stage 1 | 枚举每行合格 assistant 样本；历史 5186 样本 | 是 |
| RequestProcessor | 当前一次待计数/发送的完整请求 | 否 |

物理行号只是定位键，不能证明不同行属于独立业务会话。阈值/参考来自旧展开口径，不是对 3168 行重新估计；两种分母不能混用。

## 完整上下文计数

固定 tokenizer 为 Qwen/Qwen3-8B，revision `b968826d9c46dd6066d109eabc6255188de91218`。只下载白名单文件，缓存存在时核对来源和文件 SHA-256，本地加载不执行远端代码。

input 使用原 messages 角色和顺序，包含 system、历史 user/assistant/tool、最后提问、模板标记和 generation prompt；原文以 assistant 结尾也不自行删除。存在时原样传 tools、tool_choice、parallel_tool_calls，实际渲染由官方模板决定。

外层 response 单独计 output，不含角色标记、EOS、结构化 tool_calls 或独立 reasoning_content；历史 reasoning 按模板规则处理。文本 content 列表按顺序拼接，未知非文本块报错。空字符串输出为 0，缺失/null 不默认为 0。不截断、不填充，超长只计数，不执行模型。

长度是统一离线代理，不保证等于不同生产模型的计费 usage。output_length_mode 为 oracle_recorded_response，发送前真实输出预测未实现。调度对象只保存长度和定位键，不保存原文。

## 虚拟时间与到达

fixed 的第 i 条请求在 i*arrival_interval_ms 到达，第 0 条为 0；source 惰性读取，墙钟 tokenization 耗时不影响虚拟时间。

burst 预读有限请求，按原序每组最多 burst_size 条，将组内挤到 burst_span_ms，再把整张时间表归一化到固定模式的首末时刻；毫秒向下取整允许同时到达，实际组内跨度可能不同。单组跨度 0 时将最后一条保留到原最后时刻。N=0 为空，N=1 为 0ms。此模式不是无限在线流实现。

EOF 在最后一次到达加标称 interval 时发现，随后排空队列。相同时刻先完成、再处理当时全部到达、再处理超时及已释放窗口派发。adaptive 模式在处理每条到达时还会尝试即时通道，因此同毫秒后来的请求看到前面实际派发后的容量。到达凑满批与超时重合时记录 batch_size。

## 负载监测与调度分流

`load_monitor.py` 读取已清理60秒窗口的 `EndpointView`，按 RPM利用率、TPM利用率和并发剩余量判断端点是否繁忙；`admission.py` 将请求分流到三个通道，独立于路由评分和重型分类。端点繁忙条件取OR，系统繁忙条件取所有端点的AND。

```text
到达 → 4档 → 即时通道，全部端点范围
       1～3档 → 系统不繁忙 → 即时通道，不繁忙端点范围
               系统繁忙   → 收集窗口 → 排序 → 全部端点范围
```

所有通道最后由同一容量检查和路由策略派发。当前不做模型/健康/Cooldown硬过滤，基础范围就是全部配置端点。即时通道不会抢占在途请求；等待时的尝试顺序为4档、普通即时、已释放窗口。普通即时每次重试重新检查繁忙状态，窗口范围则允许繁忙端点。窗口批间保持FIFO。负载条件在派发、完成、窗口过期后重新观察，并按端点繁忙原因的变化记录 `load_changed`。

`window` 模式仍遵循原收集与排序行为。adaptive 要求所有请求都模拟执行；轻重标签只用于分类、统计和批内排序，不决定是否执行或是否进入窗口。

## 分类、收集与批内排序

tokens 模式按 `input>=input_threshold OR output>=output_threshold` 分类。默认输入 40342.5、输出 578 是旧 P95 起点，均非自动在线 EVT；阈值 0 会让对应轴全部命中。

百分位模式在窗口批释放前，或即时通道到达时，按冻结输出 ECDF 和门槛分类，与输入判定取 OR，标签随后冻结。分类压力输入只使用已到达需求：U=在途/总并发，E=max(0,已释放窗口+两个即时等待队列+本次分类请求数-空闲名额)。E 达总名额乘 critical_excess_ratio 为 critical，否则 E>0 或 U>=busy 为 busy，否则 U>=normal 为 normal，其余 idle。输出分类压力不读取未来到达或RPM/TPM；决定调度分流的系统繁忙判定则独立读取三项资源。

heavy_only 仅重型收集，轻型立即完成；all 全部收集并执行。以下任一条件释放批：队列达到 batch_size、最老成员等待达到 batch_wait_ms、EOF。新到达不重置最老计时器，EOF 释放不足一批。batch_wait_ms 只约束收集阶段。

每批释放时 order_batch 调用一次，随后按计划顺序追加 ready，再逐条 select 端点。每次分配立即更新容量。内置 shortest_first/longest_first 按总 tokens，light_first_fifo 稳定分组轻先重后，effective_priority 按有效权重降序再输出长度升序，同分稳定。priority_then_light 按数字档位降序，同档轻型先于重型，同类保持 FIFO。

窗口通道中后批不能越过前批；队首容量不足时后续也等待，等待期间不重排。adaptive 的两个即时通道可先于窗口派发，并可跳过暂时无法派发的即时请求。这里的批是调度窗口，不是模型张量 batch。排序扩展必须返回完整 ID 排列，路由只能选择合法候选，接口见开发文档。

## 端点容量与服务时间

端点维护 (t-60000,t] 滚动窗口。派发时 RPM 加 1、TPM 预留完整 input+output、并发加 1；完成只释放并发，窗口到期才释放 RPM/TPM。候选同时满足：

```text
requests_in_window < rpm_limit
tokens_in_window + request.total_tokens <= tpm_limit
concurrency < concurrency_limit
```

min_rpm 选择候选中 requests_in_window/rpm_limit 最小者，同分按配置顺序；TPM 和并发负责容量保护，不作为其评分。请求 tokens 超过所有端点 TPM 上限时明确拒绝，避免永久等待。无候选时推进到完成或窗口过期事件。

```text
nominal_ms = base_latency_ms + input/input_tokens_per_ms + output/output_tokens_per_ms
service_ms = max(1, ceil(nominal_ms * jitter_multiplier))
```

默认基础时延 5ms，输入速度 2000、输出速度 20 tokens/ms。波动关闭倍率为 1；开启时 JSON 键 [seed,request_id,endpoint_id] 做 SHA-256，前 8 字节的高 53 位映射 [0,1)，倍率=1+fraction*(2*u-1)。相同请求/端点不依赖派发顺序和时间；倍率作用于整个服务时间，不表示端点持续变慢。

## 输出目录与文件

| 入口 | 默认目录 |
| --- | --- |
| replay CLI / execute | `workload_profiling/results/simulation/` |
| 调度模拟网页 | `workload_profiling/results/simulation/web/<run_id>/` |
| `python -m examples.run_simulation` | `workload_profiling/results/simulation_example/` |

CLI 可用 `--output-dir`，Python 可用 execute 的 output 覆盖。每次持久化写以下七个基础文件；百分位模式还写后文的资源快照：

| 文件 | 格式与内容 |
| --- | --- |
| `config.json` | 本次生效的参数、策略规格与模拟端点 |
| `summary.json` | 全局数量、延迟与等待统计、端点分配、来源记录 |
| `requests.csv` | 每条请求最终轨迹；UTF-8 BOM |
| `events.jsonl` | 按处理顺序保存的事件，每行一个 JSON 对象 |
| `batches.json` | 批成员、排序后的计划顺序、释放时刻和触发原因 |
| `endpoints.json` | 最终窗口状态、累计量和峰值 |
| `report.md` | 本轮设置、语义与结果的文字说明 |

所有标准日志不保存原始 prompt/response 文本。逐行读取不等于逐行增量写结果：当前结果/events 保存在内存，回放结束后统一导出。

重复目录会替换上述文件，保留其他旧文件。部分本地运行另有 `acceptance_checks.json`，它不是每次自动生成的标准产物；只在其记录的 artifact 哈希与当前文件一致时，才能将验收记录归于当前这轮。

## requests CSV

按输入到达顺序排列；完成时间可以与行顺序不同。

| 字段 | 说明 |
| --- | --- |
| `request_id` | 本轮唯一请求 ID；prompt 模式按原始行生成 |
| `base_priority`、`priority_class`、`priority_source` | 基础业务权重、业务分组、标签来源；默认1/normal/default，合成标签规则见交接说明 |
| `effective_priority`、`priority_discount` | 基础权重乘适用折扣；数值大优先。仅 effective_priority 排序使用，其他排序只记录 |
| `priority_level`、`priority_level_source` | adaptive/four_level 或显式输入时记录1～4档与default/recorded/synthetic来源，4最高 |
| `scheduling_path`、`endpoint_scope` | adaptive：priority_immediate / idle_immediate / busy_window；范围为all或non_busy |
| `system_busy_at_arrival`、`busy_endpoints_at_arrival` | adaptive：到达时系统是否繁忙以及繁忙端点ID的JSON数组 |
| `routing_candidate_ids` | adaptive：实际派发前满足通道范围与容量检查的端点ID，CSV中以JSON数组保存 |
| `output_percentile`、`output_percentile_threshold`、`output_token_cutoff` | 百分位模式新增：冻结参考中的排名、本批门槛及对应最小token长度。tokens模式不新增这些列 |
| `threshold_source`、`pressure_level`、`classified_at_ms` | 百分位模式新增：固定门槛为MANUAL、压力门槛为CONGESTION；内部压力档位及分类时间。窗口请求在批释放时分类，即时请求在到达时分类，最终CSV所有请求均已分类 |
| `source_line` | 原始行号，0-based；自定义 generator 可为空 |
| `input_tokens`、`output_tokens`、`total_tokens` | 已知离线长度，total=input+output |
| `input_heavy`、`output_heavy`、`heavy` | 两轴命中与 OR 结果；边界采用 >= |
| `arrival_at_ms` | 虚拟到达时刻 |
| `batch_id` | 0-based 批编号；heavy_only 中立即完成的轻型为空 |
| `batch_position` | 排序后的 0-based 批内计划位置；heavy_only 中立即完成的轻型为空，拒绝请求也保留位置 |
| `batch_trigger` | batch_size/timeout/end_of_input；heavy_only 中立即完成的轻型为空 |
| `batch_released_at_ms` | 收集批释放时刻；即时请求为空 |
| `batch_wait_ms` | 批释放-到达；即时请求及旧 heavy_only 立即完成的轻型为 0 |
| `capacity_wait_ms` | 窗口请求为调度-批释放，即时请求为调度-到达；拒绝时用拒绝处理时刻替代调度时刻 |
| `queue_wait_ms` | 调度-到达；拒绝时为拒绝处理-到达 |
| `endpoint_id` | 实际被选择的模拟端点；立即完成的轻型/拒绝为空 |
| `dispatch_at_ms` | 模拟调度时刻；立即完成的轻型/拒绝为空 |
| `finished_at_ms` | 完成时刻；立即完成的轻型=到达，拒绝=拒绝处理时刻 |
| `service_ms` | 完成-调度；立即完成的轻型/拒绝为 0 |
| `latency_ms` | 完成/拒绝-到达；立即完成的轻型为 0 |
| `status` | 正常结束时为 completed 或 rejected；running/queued 为引擎中间状态 |
| `rejection_reason` | 正常为空；超大请求为 tokens_exceed_every_endpoint_tpm_limit |
| `endpoint_rpm_before` | 所选端点调度前窗口请求数，并非 RPM 上限 |
| `endpoint_tpm_before` | 所选端点调度前窗口预留 token 数 |
| `endpoint_concurrency_before` | 所选端点调度前在途数 |
| `rpm_utilization_before`、`tpm_utilization_before`、`concurrency_utilization_before` | 对应调度前利用率 |

成功派发并完成的请求满足 `queue_wait_ms = batch_wait_ms + capacity_wait_ms`，`latency_ms = queue_wait_ms + service_ms`。默认 heavy_only 模式下轻型没有占用端点；all 模式下轻重都入窗并执行。具体配置和新增全部/轻型指标见 [交接说明](experiments.md)。

CSV 空单元格是缺失，不是 0、false 或第一个端点。JSON 中同样的缺失用 null。空输入的 CSV 只提供最小列集；非空输入为完整轨迹列集。

## batches JSON

每个元素包含 batch_id、trigger、released_at_ms、size、request_ids、dispatch_order。request_ids 为到达顺序，dispatch_order 为排序后的计划顺序，两者包含完全相同的 ID。一个双重型请求只在一个 batch 中出现一次；触发批大小依据 batch_scope：heavy_only 只计重型，all 计全部成员。批大小可小于配置值，因为超时或 EOF 提前释放。

dispatch_order 包含最终被拒绝的请求，因此它不等于成功派发/完成顺序。实际派发顺序查看 events 的 dispatched；完成顺序可能受长度、端点速度和并发影响。跨批按释放顺序执行，等待期间不重排。

## events JSONL

每行公共字段为 time_ms 和 event，同一时刻的行顺序体现引擎处理优先级。

| event | 主要附加字段 |
| --- | --- |
| `arrived` | request_id、heavy |
| `light_completed` | request_id |
| `batch_released` | 完整批次字段 |
| `capacity_wait` | 当前无法分配的等待请求 request_id；窗口通道检查队首。adaptive 仅在容量变化后重试，避免重复记录相同状态 |
| `classification_updated` | 百分位模式每次窗口释放或即时到达分类时的压力、前后门槛、token界限与分类数量；arrived时heavy=null表示尚未分类 |
| `load_changed` | adaptive：系统/端点繁忙状态、触发原因及当前三项资源状态 |
| `admission_decided` | adaptive：本请求档位、调度路径、端点范围及到达时负载快照 |
| `dispatched` | request_id、endpoint_id、batch_id、batch_position、finished_at_ms、endpoint_state_after |
| `completed` | request_id、endpoint_id、concurrency_after |
| `rejected` | request_id、reason |

dispatched 的 endpoint_state_after 包含上限、窗口请求数/token 数和并发，是更新后的值；requests CSV 保存的是同次调度前状态。窗口过期在事件时钟推进时维护，没有独立 window_expired 日志行。

## endpoints JSON

每个端点包含：

- endpoint_id 与 rpm_limit、tpm_limit、concurrency_limit。
- requests_in_window、tokens_in_window、concurrency 及三项 utilization。
- total_requests、total_tokens 为整轮累计量。
- peak_concurrency、peak_requests_in_window、peak_tokens_in_window 为整轮峰值。

正常排空后所有 concurrency 应为 0。最终 RPM/TPM 可非 0，因为模拟结束时最近 60 秒记录还有效；累计量不会随过期减少。利用率是 0 到 1 的比值，不是百分数显示值。

## summary JSON

| 字段组 | 说明 |
| --- | --- |
| `clock`、`output_length_mode` | virtual_ms、oracle_recorded_response |
| `strategy`、`strategy_class` | 配置规格与实际策略类；直接注入实例时可能不同 |
| `batch_order`、`batch_order_class` | 批内排序规格与实际排序类；旧配置默认 fifo |
| `total_requests`、`light_requests`、`heavy_requests` | 总量、轻型、两轴 OR 后的重型 |
| `input_heavy_requests`、`output_heavy_requests`、`both_heavy_requests` | 重型交叉统计，不能简单把两轴相加 |
| `completed_requests`、`rejected_requests` | 最终状态计数，包含轻型完成 |
| `batch_count`、`batch_triggers` | 批次数与触发原因计数 |
| `last_arrival_ms`、`simulation_end_ms` | 最后到达、模拟排空时刻；空输入 last_arrival 为 null |
| `heavy_queue_wait_ms`、`heavy_latency_ms` | 已完成重型请求的 mean/p95/max；不包含被拒绝者 |
| `endpoint_dispatch_counts` | 各端点累计调度数，heavy_only 中只有重型执行，all 中轻重都执行 |
| `wall_time_seconds` | execute 的实际耗时；与虚拟时间不是同一指标 |
| `provenance` | 导出摘要的来源与构建信息 |

P95 使用 nearest-rank，即排序后取 `ceil(.95*N)` 位置。没有已完成重型时 mean/p95/max 设为 0。API 的 runner.summary 不含 provenance，execute 返回的第二项和磁盘 summary.json 包含它。

provenance 包含 created_at（Asia/Shanghai）、source 绝对路径、source_sha256、source_format、source_record_semantics、limit、tokenizer_id、tokenizer_revision、network_model_called。注入 tokenizer 未传 metadata 时 ID/revision 可能为空；lengths 模式自然没有 tokenizer 来源。

## 推荐核对顺序

先检查 source/config 是否对应本次实验，再检查 completed+rejected=total、light+heavy=total，以及双轴交集计算是否一致。然后看批触发和 batch_wait/capacity_wait 的分解，最后比较端点调度数、利用率和峰值。

比较不同路由策略时固定 batch_order，比较批内排序时固定 strategy；同时固定输入、阈值、到达间隔、批参数、端点容量和速度。实际墙钟耗时受到 tokenization/磁盘/机器状态影响，不应代替虚拟调度延迟。

### 百分位模式附加文件

| 文件 | 内容 |
| --- | --- |
| threshold_trace.csv | 每次分类的压力、前后门槛、长度界限和分类数量；空输入也有表头 |
| classification_policy.json | 实际策略映射与压力规则快照 |
| classification_metadata.json | 模式、初始门槛、来源和导出资源哈希 |
| output_percentile_reference.parquet | 本轮冻结参考副本 |
| output_reference_metadata.json | 副本ECDF定义、样本数及哈希 |
| replay_config.json | 指向同目录副本的相对资源路径 |

summary 还包括 all/light 延迟、全部收集/容量等待、发生容量等待的请求数、端点实际执行数、业务组指标；百分位模式记录 threshold_update_count、threshold_values_used、output_percentile_reference_samples。adaptive 还记录 scheduling_paths、busy_thresholds、四档 priority_levels 统计。threshold_trace.csv 的 classification_context 区分 immediate/window；即时分类的 batch_id 为空，不创建虚假批次。延迟统计只含已完成请求，拒绝数单独查看；缺少成员时统计为0不表示真实延迟为0。
