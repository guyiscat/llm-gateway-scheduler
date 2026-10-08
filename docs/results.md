# 结果文件与字段

[返回首页](../README.md) · [架构](architecture.md) · [Python API](api.md)

## 输出目录与文件

| 入口 | 默认目录 |
| --- | --- |
| baseline CLI / execute | `workload_profiling/results/baseline/` |
| baseline 网页 | `workload_profiling/results/baseline/web/<run_id>/` |
| `python -m examples.run_baseline` | `workload_profiling/results/baseline_example/` |

CLI 可用 `--output-dir`，Python 可用 execute 的 output 覆盖。每次持久化写以下七个标准文件：

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
| `source_line` | 原始行号，0-based；自定义 generator 可为空 |
| `input_tokens`、`output_tokens`、`total_tokens` | 已知离线长度，total=input+output |
| `input_heavy`、`output_heavy`、`heavy` | 两轴命中与 OR 结果；边界采用 >= |
| `arrival_at_ms` | 虚拟到达时刻 |
| `batch_id` | 0-based 批编号；轻型为空 |
| `batch_position` | 排序后的 0-based 批内计划位置；轻型为空，拒绝请求也保留位置 |
| `batch_trigger` | batch_size/timeout/end_of_input；轻型为空 |
| `batch_released_at_ms` | 收集批释放时刻 |
| `batch_wait_ms` | 批释放-到达；轻型为 0 |
| `capacity_wait_ms` | 调度-批释放；拒绝时为拒绝处理-批释放 |
| `queue_wait_ms` | 调度-到达；拒绝时为拒绝处理-到达 |
| `endpoint_id` | 实际被选择的模拟端点；轻型/拒绝为空 |
| `dispatch_at_ms` | 模拟调度时刻；轻型/拒绝为空 |
| `finished_at_ms` | 完成时刻；轻型=到达，拒绝=拒绝处理时刻 |
| `service_ms` | 完成-调度；轻型/拒绝为 0 |
| `latency_ms` | 完成/拒绝-到达；轻型为 0 |
| `status` | 正常结束时为 completed 或 rejected；running/queued 为引擎中间状态 |
| `rejection_reason` | 正常为空；超大请求为 tokens_exceed_every_endpoint_tpm_limit |
| `endpoint_rpm_before` | 所选端点调度前窗口请求数，并非 RPM 上限 |
| `endpoint_tpm_before` | 所选端点调度前窗口预留 token 数 |
| `endpoint_concurrency_before` | 所选端点调度前在途数 |
| `rpm_utilization_before`、`tpm_utilization_before`、`concurrency_utilization_before` | 对应调度前利用率 |

重型完成请求满足 `queue_wait_ms = batch_wait_ms + capacity_wait_ms`，`latency_ms = queue_wait_ms + service_ms`。轻型没有占用端点，不应被误当作一次 endpoint 调用。

CSV 空单元格是缺失，不是 0、false 或第一个端点。JSON 中同样的缺失用 null。空输入的 CSV 只提供最小列集；非空输入为完整轨迹列集。

Baseline CSV 使用标准库 csv 写入，保留 UTF-8 BOM、列顺序与必要引号，不需要 pandas。整数直接写成整数，不因同列其他行为空而转换成小数或丢失大整数精度；历史 CSV 导出模式仍使用 pandas。

## batches JSON

每个元素包含 batch_id、trigger、released_at_ms、size、request_ids、dispatch_order。request_ids 为到达顺序，dispatch_order 为排序后的计划顺序，两者包含完全相同的 ID。一个双重型请求只在一个 batch 中出现一次；触发批大小依据重型收集数，轻型不计入。批大小可小于配置值，因为超时或 EOF 提前释放。

dispatch_order 包含最终被拒绝的请求，因此它不等于成功派发/完成顺序。实际派发顺序查看 events 的 dispatched；完成顺序可能受长度、端点速度和并发影响。跨批按释放顺序执行，等待期间不重排。

## events JSONL

每行公共字段为 time_ms 和 event，同一时刻的行顺序体现引擎处理优先级。

| event | 主要附加字段 |
| --- | --- |
| `arrived` | request_id、heavy |
| `light_completed` | request_id |
| `batch_released` | 完整批次字段 |
| `capacity_wait` | 当前无法分配的队首 request_id；可能多次记录 |
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
| `endpoint_dispatch_counts` | 各端点累计调度数，只有重型占用端点 |
| `wall_time_seconds` | execute 的实际耗时；与虚拟时间不是同一指标 |
| `provenance` | 导出摘要的来源与构建信息 |

P95 使用 nearest-rank，即排序后取 `ceil(.95*N)` 位置。没有已完成重型时 mean/p95/max 设为 0。API 的 runner.summary 不含 provenance，execute 返回的第二项和磁盘 summary.json 包含它。

provenance 包含 created_at（Asia/Shanghai）、source 绝对路径、source_sha256、source_format、source_record_semantics、limit、tokenizer_id、tokenizer_revision、network_model_called。注入 tokenizer 未传 metadata 时 ID/revision 可能为空；lengths 模式自然没有 tokenizer 来源。

## 推荐核对顺序

先检查 source/config 是否对应本次实验，再检查 completed+rejected=total、light+heavy=total，以及双轴交集计算是否一致。然后看批触发和 batch_wait/capacity_wait 的分解，最后比较端点调度数、利用率和峰值。

比较不同路由策略时固定 batch_order，比较批内排序时固定 strategy；同时固定输入、阈值、到达间隔、批参数、端点容量和速度。实际墙钟耗时受到 tokenization/磁盘/机器状态影响，不应代替虚拟调度延迟。
