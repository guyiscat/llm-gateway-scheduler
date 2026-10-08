"""Persist configuration, every request, batch/event trace and endpoint counters."""
import csv
import io
import json
from pathlib import Path
from uuid import uuid4

from ..common.io import write_json


def atomic_text(path, text, encoding="utf-8"):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding=encoding, newline="")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def requests_csv(rows):
    """Serialize native request values without importing scientific DLLs.

    None becomes an empty cell. Integers remain integers even when another row
    has a missing value in that column. Quoting preserves commas and newlines.
    """
    fields = list(dict.fromkeys(key for row in rows for key in row))
    if not fields:
        fields = ["request_id", "source_line", "input_tokens", "output_tokens", "heavy",
                  "arrival_at_ms", "endpoint_id", "dispatch_at_ms", "finished_at_ms", "status"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def write_outputs(result, config, directory, *, provenance=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "config.json", config.to_dict())
    summary = {**result.summary, "provenance": provenance or {}}
    write_json(directory / "summary.json", summary)
    write_json(directory / "endpoints.json", result.endpoints)
    write_json(directory / "batches.json", result.batches)
    atomic_text(directory / "events.jsonl", "".join(json.dumps(e, ensure_ascii=False, allow_nan=False) + "\n" for e in result.events))
    atomic_text(directory / "requests.csv", requests_csv(result.requests), "utf-8-sig")
    s = result.summary
    lines = ["# Heavy-request routing baseline", "",
             "离线事件驱动回放；时钟单位为虚拟毫秒。输出长度来自已记录回答（oracle），端点与服务时间为模拟参数。", "",
             f"逐条到达间隔：{config.arrival_interval_ms} ms；重型批大小：{config.batch_size}；最老请求等待上限：{config.batch_wait_ms} ms。",
             f"Heavy = input_tokens >= {config.input_threshold_tokens} OR output_tokens >= {config.output_threshold_tokens}。",
             f"批内排序：`{s['batch_order']}`（`{s['batch_order_class']}`）；路由实现：`{s['strategy_class']}`。",
             f"路由策略：`{s['strategy']}`；RPM/TPM 使用 (t-60000,t] 滚动窗口。TPM 在调度时预留 input+output 全量 token。", "",
             "轻型请求在到达时立即完成，不占用端点。重型批释放时先调用批内排序，再逐条选择端点，每次分配立即更新状态。",
             "排序后的批按释放顺序追加到容量等待队列，后续批不越过前批。队首容量不足时等待；完成事件释放并发，窗口过期释放 RPM/TPM。单请求超过所有端点 TPM 上限时明确拒绝。",
             "batch_wait_ms 约束收集批的等待，容量等待可能更长。输入结束时释放最后不足一批的请求，随后排空所有在途请求。", "",
             f"总请求：{s['total_requests']}；轻型：{s['light_requests']}；重型：{s['heavy_requests']}；完成：{s['completed_requests']}；拒绝：{s['rejected_requests']}。",
             f"Input-heavy：{s['input_heavy_requests']}；Output-heavy：{s['output_heavy_requests']}；Both：{s['both_heavy_requests']}。",
             f"批次数：{s['batch_count']}；触发原因：{s['batch_triggers']}。",
             f"模拟结束：{s['simulation_end_ms']} ms；重型排队时间：{s['heavy_queue_wait_ms']}；重型完成延迟：{s['heavy_latency_ms']}。", "",
             "| Endpoint | Routed requests | Total tokens | Peak concurrency | RPM limit | TPM limit |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for endpoint in result.endpoints:
        lines.append(f"| {endpoint['endpoint_id']} | {endpoint['total_requests']} | {endpoint['total_tokens']} | {endpoint['peak_concurrency']} | {endpoint['rpm_limit']} | {endpoint['tpm_limit']} |")
    lines += ["", "`requests.csv` 含每条请求的分类、到达/批释放/调度/完成时刻、等待时间和调度前端点状态。",
              "`events.jsonl` 保存状态变化；`batches.json` 的 request_ids 保存到达顺序、dispatch_order 保存排序后的计划顺序；CSV 的 batch_position 保存批内位置。拒绝请求也保留计划位置。`endpoints.json` 保存最终状态及峰值；`config.json` 保存本次参数。", ""]
    atomic_text(directory / "report.md", "\n".join(lines))
    return summary
