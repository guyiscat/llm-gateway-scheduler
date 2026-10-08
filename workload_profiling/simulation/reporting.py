"""Persist configuration, every request, batch/event trace and endpoint counters."""
import json
from pathlib import Path

import pandas as pd

from ..common.io import write_json, write_text, sha256_file


def write_outputs(result, config, directory, *, provenance=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "config.json", config.to_dict())
    recorded_provenance = dict(provenance or {})
    if result.classification_artifacts is not None:
        from ..policies import PercentileReference
        artifacts = result.classification_artifacts
        reference = PercentileReference(artifacts["reference"]["values"], artifacts["reference"]["counts"])
        reference_path = directory / "output_percentile_reference.parquet"
        reference.save(reference_path)
        write_json(directory / "output_reference_metadata.json", {
            "schema_version": 1, "percentile_definition": "empirical_cdf_right",
            "reference_sample_count": reference.sample_count, "artifact_sha256": sha256_file(reference_path),
            "reference_artifact": reference_path.name,
            "original_source": artifacts["metadata"]["reference_source"]})
        write_json(directory / "classification_policy.json", artifacts["policy"])
        metadata = {**artifacts["metadata"],
                    "exported_policy_sha256": sha256_file(directory / "classification_policy.json"),
                    "exported_reference_sha256": sha256_file(reference_path)}
        write_json(directory / "classification_metadata.json", metadata)
        write_text(directory / "threshold_trace.csv",
                    pd.DataFrame(artifacts["trace"], columns=artifacts["trace_columns"]).to_csv(index=False, lineterminator="\n"), "utf-8-sig")
        replay_config = config.to_dict()
        replay_config.update(output_policy_path="classification_policy.json",
                             output_reference_path="output_percentile_reference.parquet",
                             output_reference_metadata_path="output_reference_metadata.json")
        write_json(directory / "replay_config.json", replay_config)
        recorded_provenance["classification"] = metadata
    summary = {**result.summary, "provenance": recorded_provenance}
    write_json(directory / "summary.json", summary)
    write_json(directory / "endpoints.json", result.endpoints)
    write_json(directory / "batches.json", result.batches)
    write_text(directory / "events.jsonl", "".join(json.dumps(e, ensure_ascii=False, allow_nan=False) + "\n" for e in result.events))
    frame = pd.DataFrame(result.requests)
    for column in ("busy_endpoints_at_arrival", "routing_candidate_ids"):
        if column in frame:
            frame[column] = frame[column].map(lambda value: json.dumps(value, ensure_ascii=False))
    if frame.empty:
        frame = pd.DataFrame(columns=["request_id", "source_line", "input_tokens", "output_tokens", "heavy",
                                      "arrival_at_ms", "endpoint_id", "dispatch_at_ms", "finished_at_ms", "status"])
    write_text(directory / "requests.csv", frame.to_csv(index=False, na_rep=""), "utf-8-sig")
    s = result.summary
    arrival_description = (f"逐条到达间隔：{config.arrival_interval_ms} ms。" if config.arrival_mode == "fixed" else
                           f"突发到达：每组最多 {config.burst_size} 条，原始组内跨度 {config.burst_span_ms} ms；全表归一化保持首末到达时刻，允许同毫秒多条请求。标称间隔 {config.arrival_interval_ms} ms 用于确定总跨度及 EOF。")
    scope_description = "所有请求均模拟执行。第4档走即时通道、候选范围为全部端点；其他请求在系统不繁忙时即时调度到当前不繁忙端点，系统繁忙时进入收集窗口、候选范围为全部端点。"
    classification_description = (f"重型 = 输入长度 >= {config.input_threshold_tokens} 或输出长度 >= {config.output_threshold_tokens}。" if config.output_classification == "tokens" else
        f"冻结ECDF，分类模式 {config.output_classification}，初始/固定百分位 {config.output_percentile_threshold}。即时请求在到达时分类，窗口请求在释放时分类；标签随后冻结。门槛变化见 classification_updated 事件。")
    priority_description = f"优先级赋值方式：{config.priority_assignment}；模拟种子：{config.priority_seed}。数字档位为1～4，4最高；显式录制档位保留。"
    queue_description = "容量恢复时先尝试第4档等待请求，再尝试普通即时请求，最后执行已释放窗口队列。即时通道跳过当前不可派发的请求；窗口通道保持批间FIFO和批内计划顺序。即时请求不抢占在途请求，仍受实际RPM、TPM和并发容量约束；单请求超过所有端点TPM上限时拒绝。"
    lines = ["# Request routing simulation", "",
             "离线事件驱动回放；时钟单位为虚拟毫秒。输出长度来自已记录回答（oracle），端点与服务时间为模拟参数。", "",
             f"{arrival_description}收集范围：all；批大小：{config.batch_size}；最老请求等待上限：{config.batch_wait_ms} ms。",
             classification_description,
             priority_description,
             f"批内排序：`{s['batch_order']}`（`{s['batch_order_class']}`）；路由实现：`{s['strategy_class']}`。",
             f"路由策略：`{s['strategy']}`；RPM/TPM 使用 (t-60000,t] 滚动窗口。TPM 在调度时预留 input+output 全量 token。", "",
             scope_description,
             queue_description,
             "batch_wait_ms 约束收集批的等待，容量等待可能更长。输入结束时释放最后不足一批的请求，随后排空所有在途请求。", "",
             f"总请求：{s['total_requests']}；轻型：{s['light_requests']}；重型：{s['heavy_requests']}；完成：{s['completed_requests']}；拒绝：{s['rejected_requests']}。",
             f"Input-heavy：{s['input_heavy_requests']}；Output-heavy：{s['output_heavy_requests']}；Both：{s['both_heavy_requests']}。",
             f"批次数：{s['batch_count']}；触发原因：{s['batch_triggers']}。",
             f"模拟结束：{s['simulation_end_ms']} ms；重型排队时间：{s['heavy_queue_wait_ms']}；重型完成延迟：{s['heavy_latency_ms']}。", "",
             f"端点执行请求数：{s['endpoint_executed_requests']}（轻型 {s['endpoint_executed_light_requests']}）。",
             f"已完成全部请求延迟：{s['all_latency_ms']}；轻型延迟：{s['light_latency_ms']}。",
             f"已完成全部请求排队：{s['all_queue_wait_ms']}；批等待：{s['all_batch_wait_ms']}；容量等待：{s['all_capacity_wait_ms']}；发生容量等待的完成请求数：{s['capacity_wait_requests']}。", "",
             "| Endpoint | Routed requests | Total tokens | Peak concurrency | RPM limit | TPM limit |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for endpoint in result.endpoints:
        lines.append(f"| {endpoint['endpoint_id']} | {endpoint['total_requests']} | {endpoint['total_tokens']} | {endpoint['peak_concurrency']} | {endpoint['rpm_limit']} | {endpoint['tpm_limit']} |")
    lines += ["", f"繁忙条件：RPM比例 >= {config.busy_rpm_threshold} 或 TPM比例 >= {config.busy_tpm_threshold} 或 并发 >= 上限-{config.busy_concurrency_reserve}；所有端点繁忙才判定系统繁忙。",
              f"调度路径计数：{s['scheduling_paths']}。四档优先级的4最高；priority_then_light按档位降序、同档轻型先且稳定FIFO。", "",
              "| 优先级档位 | 请求数 | 完成 | 拒绝 | 平均延迟 ms | P95 ms |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for level, group in s["priority_levels"].items():
        lines.append(f"| {level} | {group['requests']} | {group['completed']} | {group['rejected']} | {group['latency_ms']['mean']:.2f} | {group['latency_ms']['p95']} |")
    lines += ["", "`requests.csv` 含每条请求的分类、到达/批释放/调度/完成时刻、等待时间和调度前端点状态。",
              "`events.jsonl` 保存状态变化；`batches.json` 的 request_ids 保存到达顺序、dispatch_order 保存排序后的计划顺序；CSV 的 batch_position 保存批内位置。拒绝请求也保留计划位置。`endpoints.json` 保存最终状态及峰值；`config.json` 保存本次参数。", ""]
    write_text(directory / "report.md", "\n".join(lines))
    return summary
