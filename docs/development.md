# 开发接口与扩展

[项目首页](../README.md) · [使用指南](guide.md) · [架构与数据](architecture.md) · [实验说明](experiments.md)

本文件统一 Python API、策略扩展、真实 sender、HTTP 契约与目录迁移。当前新分类通过 simulation.classification 调用 runtime 的冻结 ECDF/输出策略；单条 sender 仍独立于调度引擎。

## 自适应调度模块

| 模块 | 职责 |
| --- | --- |
| `simulation.load_monitor` | `LoadMonitor` 读取端点资源快照，返回端点繁忙原因和 `SystemLoad` |
| `simulation.admission` | `AdmissionPolicy` 决定 priority_immediate / idle_immediate / busy_window 和端点范围 |
| `simulation.priority` | 按 seed/request_id 生成四档，保留录制档位及历史业务权重 |
| `simulation.ordering` | `PriorityThenLightOrderStrategy` 按数字档位降序，再同档轻型先，稳定FIFO |
| `simulation.engine` | 事件时钟、收集窗口、两个即时队列、容量等待及统一派发 |

```python
from workload_profiling.simulation import SimulationRunner, WorkloadRequest, load_config

config = load_config("workload_profiling/config/simulation_adaptive.json")
requests = [
    WorkloadRequest("normal", 100, 2000, priority_level=2, priority_level_source="recorded"),
    WorkloadRequest("urgent", 100, 2000, priority_level=4, priority_level_source="recorded"),
]
result = SimulationRunner(config).run(requests)
```

`AdmissionPolicy` 只决定通道和软繁忙筛选，不计算路由分数。现有 `RoutingStrategy.select()` 接收当前范围内满足实际容量的快照，继续可替换为自定义策略。尚未实现的模型/接口/健康/Cooldown过滤可在将来构建基础候选范围后再交给此流程；本轮基础范围为全部配置端点。

即时请求在到达时分类，窗口请求在释放时分类；分类结果和窗口排序计划随后冻结。最高档不能通过折扣降为其他档。4档等待请求在容量释放时优先尝试，但不会抢占已经派发的请求。等待请求仅在容量状态改变后重试，避免每条新到达都重复输出相同容量等待事件。

## 文件命名与迁移

| 旧位置 | 当前位置 |
| --- | --- |
| `workload_profiling.baseline` | `workload_profiling.simulation` |
| `BaselineConfig` / `BaselineRunner` | `SimulationConfig` / `SimulationRunner` |
| `baseline/run.py` | `simulation/cli.py` |
| `runtime/run_stream.py` | `runtime/cli.py` |
| `web/app.py` | `web/cli.py` 负责入口；`web/profile_console.py` 负责请求长度服务 |
| `web/dashboard.py` | `web/simulation_console.py` |
| `DemoService` / `DashboardService` | `ProfileService` / `SimulationService` |
| `examples/output_first.py` | `examples/output_longest_first.py`，明确输出长度降序 |
| `examples/run_baseline.py` | `examples/run_simulation.py` |
| `experiments/comparison_common.py` | `experiments/validation.py` |
| `experiments/rebuild_all_requests_baseline.py` | `experiments/compare_request_scopes.py` |
| `simulation.reporting.atomic_text` | `common.io.write_text`，统一原子文本写入 |
| 根 `baseline.py` / `demo.py` | 已删除；使用 `python -m workload_profiling replay/web` |
| 包内 `requirements.txt` | 已合并至根 `requirements.txt` |

仓库内导入、插件规格、测试和文档已同步更新。外部脚本和自定义配置应按表更新旧模块名；例如 `examples.output_first:OutputLongestFirstOrder` 改为 `examples.output_longest_first:OutputLongestFirstOrder`。调度网页接口前缀改为 `/api/simulation/`，CSV 下载路径改为 `/simulation/requests.csv`。配置文件改名见[使用指南](guide.md#配置文件命名迁移)。历史数据与科学报告保留原始来源记录；新运行使用新文件名。

命名迁移时通过 146 项测试、两个网页服务的本地 HTTP 检查，以及 8 个场景各 3168 条请求的前后回放对比。模块与路径名称按迁移表归一化后，请求、事件、批次、端点、汇总及分类记录完全一致；42 份历史数据和科学产物的 SHA-256 未改变。加入自适应调度后，测试总数为 171，旧 8 个场景仍保持一致；新自适应配置的 3168 条回放全部完成，并验证了示例导出回放及网页配置提交。

默认从仓库根导入。新调度 API 位于 `workload_profiling.simulation`；当前请求计数/发送 API 位于 `workload_profiling.runtime`。两者职责不同。

## 选择哪个入口

| 需求 | 接口 |
| --- | --- |
| 用原始文件完成回放并导出 | `simulation.cli.execute()` |
| 用自己的 generator/队列提供长度请求 | `SimulationRunner.run()` |
| 修改批大小、频率、端点 | `load_config()`、`dataclasses.replace()`、`EndpointConfig` |
| 更换路由评分 | `RoutingStrategy.select()` 或 `load_strategy()` |
| 更换一批请求的派发顺序 | `BatchOrderStrategy.order_batch()` 或 `load_batch_order()` |
| 接入已有同步模型客户端并计数 | `runtime.RequestProcessor` |
| 在自己的客户端发送前/后插入计数 | `prepare()` / `complete()` |
| 使用固定输出 ECDF 与动态百分位阈值 | `PercentileReference`、`OutputHeavyPolicy` |

## SimulationConfig 与 EndpointConfig

```python
from dataclasses import replace
from workload_profiling.simulation import EndpointConfig, load_config

config = replace(
    load_config(),
    batch_size=8,
    batch_wait_ms=10,
    arrival_interval_ms=1,
    endpoints=(
        EndpointConfig("my_a", rpm_limit=600, tpm_limit=5_000_000, concurrency_limit=8),
        EndpointConfig("my_b", rpm_limit=900, tpm_limit=7_500_000, concurrency_limit=12),
    ),
)
```

`load_config(path=CONFIG_PATH) -> SimulationConfig` 读取 JSON，路径默认指向包内 `config/simulation_default.json`。

`SimulationConfig.from_dict(data) -> SimulationConfig` 将完整字典中的 endpoints 转成 `EndpointConfig`；未提供的普通字段使用默认值，但 endpoints 必须非空。`config.to_dict()` 返回可 JSON 序列化的完整字典。

两个配置对象均为冻结 dataclass。使用 `replace()` 生成新配置；不要直接改属性。裸 `SimulationConfig()` 没有端点，会校验失败。字段、单位、默认值和边界见 [配置表](guide.md)。非法数值、重复端点、空端点报 `ValueError`，未知或格式不符的构造参数可能报 `TypeError`。

## WorkloadRequest

```python
from workload_profiling.simulation import WorkloadRequest

request = WorkloadRequest(
    request_id="req_001",
    input_tokens=50000,
    output_tokens=600,
    source_line=0,
)
assert request.total_tokens == 50600
```

| 字段 | 契约 |
| --- | --- |
| `request_id: str` | 非空，单次回放内唯一 |
| `input_tokens: int` | 非负整数，bool 不接受 |
| `output_tokens: int` | 非负整数，bool 不接受；模拟器要求已知输出/预算 |
| `source_line: int \| None` | 可选，非负原始行号，0-based |
| `base_priority`、`priority_class`、`priority_source` | 基础权重、分组和来源；默认1/normal/default |
| `priority_level`、`priority_level_source` | 数字档位 1～4 与来源 default/recorded/synthetic；默认 1/default。显式录制的 1 档应设置 recorded，避免被模拟赋值覆盖 |
| `total_tokens` | 只读属性，input+output |

对象冻结，不保存 prompt 文本，也没有自定义 arrival 字段。到达支持 fixed/burst 预设；任意原始时间戳需要扩展事件入口。

## SimulationRunner.run

```python
from dataclasses import replace
from workload_profiling.simulation import SimulationRunner, WorkloadRequest, load_config

def incoming():
    yield WorkloadRequest("r0", 100, 20)
    yield WorkloadRequest("r1", 50000, 600)

runner = SimulationRunner(replace(load_config(), batch_size=2, batch_wait_ms=3))
result = runner.run(incoming())
print(result.summary)
print(result.requests[1]["endpoint_id"])
```

签名：`SimulationRunner(config, *, strategy=None, batch_order=None)`；`run(requests: Iterable[WorkloadRequest]) -> RunResult`。

- 提供 strategy 实例时，使用该实例；否则根据 `config.strategy` 加载。
- 提供 batch_order 实例时，使用该实例；否则根据 `config.batch_order` 加载。两个接口必须同步，分别在批释放和逐条派发时调用。
- 按需消费 generator，不预加载原文。结果行、事件和批次仍保存在内存，内存用量会随请求数量增长。
- 处理完 EOF 后释放最后一批，并排空容量等待和在途请求才返回。
- 同一 runner 只能调用一次 `run()`；重复实验要创建新实例。
- 输入必须为 `WorkloadRequest`，重复 ID、无效策略选择会报错；没有 silent retry。
- `run()` 不读取文件、加载 tokenizer 或自动写结果。

`RunResult` 的字段：

| 字段 | 类型 | 内容 |
| --- | --- | --- |
| `requests` | `list[dict]` | 每条请求最终分类、时序、端点、状态 |
| `events` | `list[dict]` | 有序事件记录 |
| `batches` | `list[dict]` | 批次成员与触发原因 |
| `endpoints` | `list[dict]` | 最终状态、累计量、峰值 |
| `summary` | `dict` | 数量、等待/延迟统计和分配计数 |

runner 本身不包含来源 metadata；`execute()` 才补充墙钟耗时并创建导出 provenance。字段解释见 [结果文档](architecture.md)。

## Source 读取接口

```python
from workload_profiling.simulation import SimulationRunner, load_config
from workload_profiling.simulation.source import read_prompt_requests
from workload_profiling.common.tokenizer import load_tokenizer

tokenizer, metadata = load_tokenizer()
requests = read_prompt_requests("examples/prompt_requests.jsonl", tokenizer, limit=2)
result = SimulationRunner(load_config()).run(requests)
```

| 函数 | 参数与返回 |
| --- | --- |
| `read_prompt_requests(source, tokenizer, *, limit=None, progress=None)` | generator，每个原始 prompt/response 行产生一个 `WorkloadRequest` |
| `read_length_requests(source, *, limit=None)` | generator，每个长度 JSONL 行产生一个 `WorkloadRequest`，不加载 tokenizer |

prompt 数据需要 `prompt.messages` 非空列表及可计数的 `response` 文本；完整历史保留，messages 最后一条可以是 assistant。长度数据需要整数 `input_tokens`、`output_tokens`；可选 request_id，省略时按行号生成 `request_000000` 等。

`source_line` 为 0-based。格式/tokenization 错误报告 1-based 文件行号，默认停止；空白行不是有效请求。progress 是 `progress(count)` 同步回调，每处理 100 条调用一次，不保证对不足 100 的最后一段调用。直接使用 reader 时调用方应提供正整数 limit，`execute()` 会验证 limit。

## execute 文件回放接口

```python
from dataclasses import replace
from workload_profiling.simulation import load_config
from workload_profiling.simulation.cli import execute

config = replace(load_config(), batch_size=2, batch_wait_ms=3)
result, exported_summary = execute(
    config,
    source="examples/length_requests.jsonl",
    source_format="lengths",
    output="workload_profiling/results/simulation_example",
)
```

签名：

```text
execute(config, *, source=DEFAULT_SOURCE, source_format="prompt", limit=None,
        output=DEFAULT_OUTPUT, tokenizer=None, tokenizer_metadata=None, progress=None,
        strategy=None, batch_order=None)
```

| 参数 | 说明 |
| --- | --- |
| `config` | 已验证的 `SimulationConfig` |
| `source` | 原始/长度 JSONL 文件路径 |
| `source_format` | `prompt` 或 `lengths` |
| `limit` | None 全部，或正整数 |
| `output` | 输出目录，不是单个文件 |
| `tokenizer` | prompt 模式可注入已加载 tokenizer，便于多次实验复用 |
| `tokenizer_metadata` | 注入 tokenizer 时同时传其来源信息，供 provenance 记录 |
| `progress` | 读取进度回调，与 reader 相同 |
| `strategy` | 可选路由实例，覆盖 config.strategy 的实现 |
| `batch_order` | 可选排序实例，覆盖 config.batch_order 的实现 |

返回 `(RunResult, exported_summary)`。第二项包含 `provenance`，第一项的 `summary` 包含统计和 `wall_time_seconds`，但不包含导出的 provenance。执行前后比较源文件 SHA-256，变化则报错。lengths 模式不加载 tokenizer。

传入外部 tokenizer 但不传 metadata 时，输出中的 tokenizer ID/revision 可能为空；不会推断外部对象的真实来源。`execute()` 与 SimulationRunner 都支持策略实例注入，未注入时按配置规格加载。注入后 summary 的 strategy_class/batch_order_class 记录实际类，配置中的规格字符串保持原值；自定义构造参数需由调用方另行记录。

## 结果持久化

```python
from workload_profiling.simulation.reporting import write_outputs

# result 来自 SimulationRunner.run()，config 为本次配置
summary = write_outputs(result, config, "workload_profiling/results/simulation_example",
                        provenance={"source_kind": "application_generator"})
```

`write_outputs(result, config, directory, *, provenance=None) -> dict` 写基础文件及适用的百分位快照，返回带 provenance 的 summary。单文件采用临时文件替换；它不是跨文件事务，也不会清理目录里其他文件。请为独立实验选择独立目录。

## 批内排序接口

`BatchOrderStrategy.order_batch(requests, endpoints, now_ms) -> Sequence[str]` 在每次非空批释放时调用一次，必须同步实现；无需继承 Protocol。requests 是按到达顺序排列的冻结 WorkloadRequest tuple，endpoints 是所有端点的冻结 EndpointView tuple，包括忙碌端点；now_ms 是批释放的虚拟毫秒时间。

返回值为本批全部 request_id 的完整排列，推荐 list/tuple。字符串、set、generator、None 等非序列返回值报 TypeError；漏项、重复、陌生 ID 或非字符串元素报 ValueError。引擎在加入待调度队列前校验，不会接受部分排序。

`load_batch_order(spec, *, config=None, classification_policy=None)` 支持 fifo/shortest_first/longest_first/effective_priority/light_first_fifo/priority_then_light，以及 `module:attribute` 的无参类、factory 或已有实例。effective_priority/light_first_fifo/priority_then_light 需要 config，百分位模式还传入分类器。总长度升降序类分别为 ShortestFirstOrderStrategy、LongestFirstOrderStrategy，同长保持到达顺序。

```python
from dataclasses import replace
from workload_profiling.simulation import SimulationRunner, load_config
from workload_profiling.simulation.source import read_length_requests

config = replace(load_config(), batch_order="shortest_first", batch_size=2, batch_wait_ms=3)
result = SimulationRunner(config).run(read_length_requests("examples/length_requests.jsonl"))
print(result.batches[0]["request_ids"])
print(result.batches[0]["dispatch_order"])
```

排序后按该顺序逐条进入容量等待队列；只调整当前批，后续批不越过前批，等待期间不重新排序。CSV 保持输入到达顺序，通过 batch_position 关联计划位置。自定义排序、路由同时注入与共享实例的接入方式见 [扩展指南](development.md)。

## 路由接口与端点视图

`RoutingStrategy` 是结构化类型契约，不需要继承它。实现同步 `select(request, endpoints, now_ms) -> endpoint_id`。传入的 endpoints 是非空候选 tuple，每个成员为冻结 `EndpointView`。

| EndpointView 成员 | 含义 |
| --- | --- |
| `endpoint_id` | 唯一 ID |
| `rpm_limit`、`tpm_limit`、`concurrency_limit` | 容量上限 |
| `requests_in_window` | 过去 60 秒调度次数 |
| `tokens_in_window` | 过去 60 秒预留 tokens |
| `concurrency` | 当前在途数 |
| `rpm_utilization`、`tpm_utilization`、`concurrency_utilization` | 当前值 / 对应上限 |
| `can_accept(request)` | 检查三项容量限制 |

`load_strategy(spec)` 接受 `min_rpm` 或 `module:attribute`，支持无参类、factory 或已有实例。它检查 select 存在且同步；引擎验证返回的 ID 属于候选。策略必须只选端点，不修改容量或主动发送网络请求。

## 接入已有模型客户端

真实发送的契约位于 `runtime.RequestProcessor`，独立于离线调度引擎。以下示例可运行，但 sender 只返回演示文本；把它替换为自己的同步客户端即可真实发送。

```python
from examples.sender import send_one
from workload_profiling.common.tokenizer import load_tokenizer
from workload_profiling.runtime import RequestProcessor

tokenizer, _ = load_tokenizer()
processor = RequestProcessor(tokenizer, sender=send_one)
result = processor.process({"messages": [{"role": "user", "content": "解释二分查找"}]})
print(result)
```

构造函数：`RequestProcessor(tokenizer, *, output_policy=None, sender=None, include_response=False)`。

| 方法 | 行为 |
| --- | --- |
| `prepare(record) -> PreparedRequest` | 规范化当前完整上下文，计算 input；不发送 |
| `complete(prepared, response) -> dict` | 根据完整响应计 output/total，可评估输出策略；不发送 |
| `process(record) -> dict` | prepare → 可选调用 sender 一次 → complete；无回复则返回 input-only |
| `process_stream(records, *, on_error="raise") -> Iterator[dict]` | 当前结果被消费后才读取下一条，加 stream_index；yield 模式记录错误并继续 |

record 支持字符串、`{"messages":[...]}` 或 `{"prompt":{"messages":[...], ...}}`；可携带外层 `conversation_id`、`request_index` 和录制的 `response`。此接口要求至少一条 user，当前上下文最后是 user 或 tool；有 tool 时校验其 tool_call_id 与历史调用的关系。这比 baseline 的原始离线计数入口更严格。

sender 接收规范化 prompt 的深拷贝，含完整 messages、tools 及其他 API 参数；返回字符串、text content 列表或标准 assistant message。它必须同步返回完整响应。async 客户端/输出 token 流须在调用方适配为完成的同步响应；当前 stream 指请求流，不是生成 token 流。

配置 sender 时不能同时提供录制 response。每条发送一次，无自动重试、预读、并发或端点选择；发生 send 失败也不重试。默认结果不含回复原文，需显式 `include_response=True`。

也可把计数插入自己的发送过程：

```python
from copy import deepcopy

prepared = processor.prepare({"messages": [{"role": "user", "content": "hello"}]})
response = send_one(deepcopy(prepared.prompt))  # 替换为自己的客户端调用
result = processor.complete(prepared, response)
```

`PreparedRequest` 包含 `prompt`、`lengths`、`identifiers`；发送应使用同一份完整上下文，不要在计数后替换内容。process 会将错误包装成 `RequestProcessingError(stage, code)`；stage 为 input/send/output。直接调用 prepare/complete 时抛出原始结构或计数错误。

没有 response 时 output_tokens、total_tokens 等未知字段为 None；若绑定策略，百分位和 Heavy 也为 None。普通结果还包含角色计数、response_has_tool_calls 和 tokenization_status。

## 独立的百分位策略与拥塞接口

```python
from workload_profiling.runtime import (
    CongestionState, OutputHeavyPolicy, PercentileReference, StaticCongestionProvider,
)

reference = PercentileReference.from_lengths([10, 20, 30, 100])
provider = StaticCongestionProvider(CongestionState.NORMAL)
policy = OutputHeavyPolicy(reference, congestion_provider=provider)
policy.set_threshold(0.85)
decision = policy.evaluate(30)
policy.clear_manual_override()
provider.set_state(CongestionState.BUSY)
```

`PercentileReference` 提供 `from_lengths()`、`percentile()`、`percentiles()`、`minimum_length_at()`、`save()`、`load(artifact_path, metadata_path)`。ECDF 为 `count(reference <= x)/N`，相同长度同百分位。load 需要含定义、artifact SHA-256 与 sample_count 的 metadata；历史 Stage 2.1 负责生成它，save 本身只写 Parquet。

`OutputHeavyPolicy` 提供 `evaluate(output_tokens)`、`classify(output_percentile, *, output_tokens=None)`、`get_threshold()`、`set_threshold()`、`clear_manual_override()`、`update_from_congestion()` 与 threshold_source 属性。百分位阈值必须 0<X<1，优先级 MANUAL > CONGESTION > DEFAULT；有 provider 时每次判断读取一次状态。classify 是低层接口，正常使用 evaluate 保证长度与百分位一致。

外部 controller 可继承 `CongestionProvider`，实现 `get_state() -> CongestionState`，返回 IDLE/NORMAL/BUSY/CRITICAL 之一。默认阈值映射依次为 .98/.95/.90/.80。StaticCongestionProvider 只验证状态映射，不估计真实拥塞。

OutputHeavyPolicy 可绑定 RequestProcessor；baseline 的可选百分位模式通过 OutputPercentileClassifier 使用它和模拟压力。tokens 模式仍使用绝对阈值；同步 sender 未接模拟调度。动态默认映射90/80/70/60%，与历史runtime配置98/95/90/80%不同。

### 自定义策略最小示例

```python
from dataclasses import replace
from workload_profiling.simulation import SimulationRunner, load_config
from workload_profiling.simulation.source import read_length_requests

class OutputOrder:
    def order_batch(self, requests, endpoints, now_ms):
        return [r.request_id for r in sorted(requests, key=lambda r: r.output_tokens)]

class ConcurrencyRoute:
    def select(self, request, endpoints, now_ms):
        return min(endpoints, key=lambda e: e.concurrency_utilization).endpoint_id

config = replace(load_config(), batch_scope="all")
result = SimulationRunner(config, batch_order=OutputOrder(), strategy=ConcurrencyRoute()).run(
    read_length_requests("examples/length_requests.jsonl"))
```

CLI 用 module:attribute 导入无参类/factory/已有实例。有构造参数用factory；两个规格即使写同一类也分别构造，Python 可直接注入同一实例共享状态。策略输入冻结，不能改计数；随机策略自行固定种子并记录参数。


## 扩展边界

来源适配成 Iterable[WorkloadRequest] 即可回放；必须非负整数长度和单轮唯一 ID，未知 usage 不应默认为 0。到达由 fixed/burst 生成，日志原时间戳不会自动生效。

输出预测需要在适配层提供预算，并单独保留实际长度、预测长度、预测器版本；还要明确服务时间/TPM 使用预算还是实际值。仅替换 WorkloadRequest 数字不会自动完成预测器评估。

真实服务执行需另增加完整 prompt 关联、异步提交/完成回调、失败/超时/取消、usage 修正与重试语义。EndpointConfig 没有 URL/key/client；不要在 select 内发送模型请求，它会阻塞虚拟时钟并破坏计数。

| 扩展 | 当前边界 |
| --- | --- |
| 跨批选择、等待期间重排、防饥饿 | 批内接口不覆盖，需扩展 engine 队列和事件 |
| 任意到达时间戳 | 需扩展 arrivals 与事件入口 |
| 模型兼容、动态端点生命周期 | 需扩展请求字段、候选与生命周期事件 |
| 输出在线预测、输入 EVT 在线更新 | 需新算法及来源/审计字段 |
| SLO、TTFT/TPOT | 当前只有整体虚拟完成延迟，无生成 token 事件 |
| 超大流量增量导出 | fixed 来源惰性，但请求和事件仍全保留内存 |

## 本地 HTTP 接口

`python -m workload_profiling web` 启动调度控制台，绑定 127.0.0.1。仅一个正在运行的任务，结果 API 指向最近一轮；每轮文件保存于 results/simulation/web/run_id。

| 方法 | 路径 | 成功 | 用途 |
| --- | --- | --- | --- |
| GET | / | 200 | HTML |
| GET | /api/simulation/config | 200 | 磁盘 simulation_default.json 完整默认值 |
| POST | /api/simulation/run | 202 | 后台回放 |
| GET | /api/simulation/status | 200 | 状态、进度及完成结果 |
| GET | /api/simulation/requests?offset=0&limit=100 | 200 | 完成后分页 |
| GET | /simulation/requests.csv | 200 | 全量 UTF-8 BOM CSV |

POST JSON 为 `{"limit":200}`，null/省略为全部，0、负数、bool 无效。覆盖配置传 `{"config":完整配置,"limit":200}`：它不与磁盘配置做任意 merge，需合法 endpoints。网页只允许 min_rpm 和内置排序，包括 fifo/shortest_first/longest_first、effective_priority、light_first_fifo、priority_then_light；固定默认原始输入，不提供 source、上传或外部 Python 插件。界面可设置 adaptive/window、四档模拟和繁忙阈值，选择 adaptive 或百分位分类时自动使用 batch_scope=all。请求 Content-Length 必须 1..100000 bytes。

status 为 idle/running/completed/failed；profiled 每 100 条更新。completed 包含 summary、endpoints、output_directory，仍需检查 rejected_requests；failed 包含 error。202 只表示启动，停止进程不恢复任务。错误参数或任务未完成为 400，未知路径 404。requests 的 offset>=0，limit 为 1..200，count 是总数，合法越界 offset 返回空数组。

调用顺序：GET config → 修改完整字典 → POST run → 轮询 status → GET requests/CSV。运行中结果不可分页，旧结果也不会作为新任务结果返回。

`web --length-demo` 使用另一种服务器，同端口不能同时运行两模式；依赖历史 Stage 2.1：

| 方法与路径 | 用途 |
| --- | --- |
| GET /api/config | 历史策略默认阈值 |
| POST /api/profile | 单条长度与百分位标签 |
| GET /api/dataset?page=0&size=30&heavy=0 | 历史完整视图分页，page>=0，size 1..100 |
| GET /export.csv | 历史 23 列 CSV |

profile body 形如 `{"prompt":"解释二分查找","threshold":0.9,"response_mode":"typed","response":"折半缩小范围"}`。prompt 可为完整 messages 对象；模式 input_only/typed/example，typed 必须字符串回答，example 为固定演示文本。threshold null/省略用默认，其他必须 (0,1)，只影响本次计算。body 1..2000000 bytes；成功 200，输入/处理失败 400。结果包含 result、active_threshold、active_threshold_source、response_mode、example_response、model_called=false。
