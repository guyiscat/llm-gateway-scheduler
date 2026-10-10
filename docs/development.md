# 开发接口

[项目首页](../README.md) · [使用指南](guide.md) · [架构](architecture.md)

## 核心入口与统一结构

```python
from workload_profiling.core import (
    Scheduler, SchedulerRequest, ExecutionResult,
    load_endpoint_configs, load_scheduler_config,
)

endpoints = load_endpoint_configs("workload_profiling/config/endpoint_config.json")
settings = load_scheduler_config("workload_profiling/config/scheduler_config.json")
scheduler = Scheduler(endpoints, settings)
record = scheduler.submit_request(SchedulerRequest(
    request_id="req_001", target_model="deepseek-flash",
    messages=({"role": "user", "content": "hi"},),
    input_tokens=1200, predicted_output_tokens=500,
    max_tokens=1024, priority=1, stream=True, arrival_time=1000,
    slo={"ttft_ms": 2000, "tpot_ms": 80}, metadata={"user_id": "u1"},
))
decisions = scheduler.take_decisions()
# 将 decisions 交给网关/执行器。执行完成后：
decision = decisions[0]
scheduler.accept_feedback(ExecutionResult(
    request_id=decision.request.request_id,
    endpoint_id=decision.selected_endpoint_id, success=True,
    started_at_ms=decision.dispatched_at_ms, finished_at_ms=1500,
    actual_input_tokens=1200, actual_output_tokens=450,
    ttft_ms=100, tpot_ms=0.8,
))
```

时间统一为同一运行时间轴上的整数毫秒，不混用秒、Unix 秒与虚拟毫秒。arrival_time 按接入顺序非递减；反馈可以迟到，其完成时间不倒退内部处理时钟。独立 SchedulerConfig 默认 tokens；加载项目调度文件会使用现有百分位分类，可注入 classifier 替换。

SchedulerRequest 必需 ID、模型、输入及预测输出；priority 只允许0/1，不含实际输出。消息和 metadata 接入时深拷贝。SLO 可省略。对象字段冻结，但嵌套消息/metadata 按只读约定使用，插件不要修改。RouteDecision 保存请求、端点、派发时刻、候选 ID 与 metadata。RequestBatch 保存完整窗口成员、释放时间和触发原因。

submit_request 也接受对应字段的 dict，返回 SchedulingRecord。即时路径可能已产生 decision；等待路径 status=queued，之后由反馈或 tick 派发。使用 take_decisions 消费待执行决定，或构造时注入 on_dispatch(decision) 投递到自己的执行队列。真实调用应放在工作线程/任务中，避免阻塞核心时钟；完成后回到同一个串行调度循环调用 accept_feedback。

窗口/额度/冷却需要外部定时器：读取 next_wakeup_ms，在该时刻调用 tick(now_ms)，每次变化后重新注册。外部健康观测用 states.update_health(endpoint_id, healthy, now_ms)，再 tick。核心方法要求单一串行调用者；多线程应在调用方同步，多进程不能各自共享这份内存状态。

ExecutionResult 的实际长度未知时用 None，不能填写预测冒充实际。未知项保留预留估计。success=False 时 failure_kind 可为 endpoint、rate_limit、request、cancelled 或 None；仅前两者自动冷却。反馈须匹配实际预留的 ID、端点、开始时间，重复和未知反馈会拒绝。默认 E2E 为完成减到达，包含排队；TTFT/TPOT 未测量时留空。

## 可替换排序与路由

```python
class MyRoute:
    def select_endpoint(self, request, endpoints, context):
        # endpoints 已满足模型、健康、通道软筛选及硬容量条件。
        # context.endpoint_configs 提供价格/能力；performance 提供近期缓存。
        return min(endpoints, key=lambda e: e.rpm_utilization).endpoint_id

class MyRank:
    def rank_requests(self, requests, context):
        # 返回原请求对象，每个恰好一次；此处给出 FIFO。
        return tuple(requests)

scheduler = Scheduler(endpoints, settings,
    routing_policy=MyRoute(), ranking_policy=MyRank())
```

RoutingContext 包含 now_ms、候选 endpoint_configs、performance。EndpointPerformance 提供样本数、成功/失败数、mean_e2e_ms、mean_ttft_ms、mean_tpot_ms。SLO/stream/预测位于请求本身，未来 Pareto 可直接使用这些信息。默认 min_rpm 保持原逻辑，不会自动执行 Pareto 或约束 SLO。

新排序上下文是 dict，包含 now_ms、endpoints，直接核心调用另含本批 classification。策略自身保存参数。weighted_length 使用 `input_weight × input_tokens + output_weight × predicted_output_tokens`，ranking_direction 控制方向，同分稳定。默认 priority_then_light 和 fifo 保留。所有输出 ID 必须完整、唯一且无外来成员；非法顺序在派发前拒绝。路由只能返回当前候选中的 ID。

策略配置支持 module:attribute 的无参类、工厂或实例。保留旧 select(request,endpoints,now_ms) 与 order_batch(requests,endpoints,now_ms) 接口，以免破坏现有研究插件；三份 simulation/routing.py、ordering.py、classification.py 仅保留兼容导入，算法实现位于 policies。async 策略会拒绝。

模拟旧 order_batch 接收保留 priority_level 的观测副本，但其中 output_tokens 已替换为预测值；这样保留旧四档排序且不会把实际输出泄露给排序。只有新 rank_requests 的插件则接收标准核心请求。原始实际观测只交给模拟执行器。新增真实策略应使用标准接口。

## 硬过滤与共享状态

EndpointFilter.register(name, rule) / remove(name) 可增删能力规则。rule(request, config, view, now_ms) 返回 None 表示通过，返回原因字符串表示排除。模型、接口、上下文是默认能力规则；健康、Cooldown 和硬容量保护仍统一执行。过滤只筛选，不打分。

EndpointPool 的 compatible/eligible/feasible 分别表示能力匹配、健康候选、容量可行；exclusions 保存原因。繁忙判断使用 eligible，路由使用通道软筛选后的 feasible。不要把容量耗尽端点从繁忙判定中丢掉。

所有模块从一个 EndpointStateManager 获取快照。可注入已建立的 state_manager，但必须使用相同 SchedulerConfig。states.export(now_ms) 生成动态状态，history.snapshot(ids,now_ms) 读取近期性能。load_health_state(path,now_ms) 仅恢复行政健康/冷却，拒绝未知端点和未完成并发；历史配额/在途续跑不在支持范围内。

## LiteLLM 参数与执行适配

```python
from workload_profiling.adapters import LiteLLMAdapter

adapter = LiteLLMAdapter()
endpoint = scheduler.states.configs[decision.selected_endpoint_id]
params = adapter.build_params(decision, endpoint)

# 真正接入时由应用安装 SDK 并提供环境凭据：
# import litellm
# result = adapter.execute(decision, endpoint, litellm.completion,
#                          on_chunk=send_chunk_to_client)
# scheduler.accept_feedback(result)
```

参数保留 model、messages、max_tokens、stream、metadata，透传 temperature/top_p/tools/tool_choice/parallel_tool_calls/response_format/stop/seed/user。model 默认 target_model，配置 deployment_model 时使用实际部署名；api_base 来自所选端点。流式调用请求 include_usage，未知用量仍允许 None。适配器不导入 SDK、不存 API key，也不会在构造时联网；参数是否支持取决于具体提供商。

metadata.force_endpoint 总是写为所选 ID，但它只是项目/网关约定，**不能假定 LiteLLM 原生按这个字段强制路由**。执行前必须配置端点 api_base、唯一 deployment_model，或注入 endpoint_resolver(endpoint,request) 返回部署绑定参数。resolver 可以映射到自定义网关别名；网关必须落实该映射。默认模拟端点没有部署地址，execute 会拒绝未绑定调用；build_params 仍可用于审查参数。

execute 接受同步 completion 函数，可以是 LiteLLM completion 或项目网关。普通响应提取实际 usage 和耗时；流式响应逐块回调，忽略仅 role 的首块，内容/工具输出首块产生 TTFT，已知输出 token 数及多块输出产生平均 TPOT。缺少观测时不伪造指标。适配器返回反馈，由调用方回送核心；不是独立后台执行系统。

默认错误映射：400/404/422 为 request，429 为 rate_limit，5xx/TimeoutError/ConnectionError 为 endpoint，未知原因保留 None；可注入 error_classifier 针对 SDK 异常扩展。输出回调异常归为 request，避免错误冷却端点。参考 [LiteLLM 参数文档](https://docs.litellm.ai/docs/completion/input) 与 [流式接口](https://docs.litellm.ai/docs/completion/stream)。

## 模拟与本地路由记录

旧 API 继续可用：

```python
from workload_profiling.simulation import load_config, SimulationRunner, WorkloadRequest

config = load_config()
runner = SimulationRunner(config)
result = runner.run([WorkloadRequest("sample", 100, 300)])
```

WorkloadRequest 是离线实际观测，output_tokens 为实际输出；可附上 priority=0/1、predicted_output_tokens、messages、target_model、stream、max_tokens、slo、metadata。RequestParameterGenerator.prepare 生成标准请求和独立观测；target_model 非 null 时保留，否则使用 SimulationConfig.target_model，默认 deepseek-flash。显式非法模型值在观测校验时拒绝。SimulatedRequestSender.schedule 只决定到达时间，submit(request,scheduler) 使用同一入口。真实接入绕过这些模拟模块。

SimulationRunner 每实例运行一次。可注入 strategy、batch_order、classification_policy、executor、on_route、on_request；executor.plan(decision,observation) 返回 ExecutionResult，便于模拟失败/限流；不另维护资源计数。on_route(decision) 在已预留的实际派发时同步调用，先于 executor.plan；不能是 async，也不能返回 awaitable。回调错误会终止回放，核心将当前执行标为 request 失败并释放并发；未知实际用量仍保留估计，RPM 不立即撤回。on_request(request) 在 prepare 完成后、submit 前调用，获得带所有参数和默认值的 SchedulerRequest，之后被拒绝的请求也会被观测。它必须同步，参数按只读使用；回调异常会终止回放。

CLI 的 execute 返回单个 RunResult，负责来源校验、tokenizer、回放与原子记录：

```python
from workload_profiling.simulation.cli import execute
result = execute(load_config(), limit=200,
                 output="workload_profiling/results/routes.jsonl")
# result.requests/events/batches/summary 等可用于内存分析。
```

记录器也可独立用于核心派发回调：

```python
from workload_profiling.adapters import RouteOutputRecorder

with RouteOutputRecorder("workload_profiling/results/routes.jsonl", endpoints) as recorder:
    scheduler = Scheduler(endpoints, settings, on_dispatch=recorder)
    # 在此同步提交请求、推进 tick 并接受反馈，直到本次运行结束。
```

RouteOutputRecorder 使用 EndpointConfig 和 LiteLLMAdapter.build_params 构建每行交付记录；可通过 adapter=LiteLLMAdapter(endpoint_resolver=...) 注入部署映射。未派发或拒绝请求没有记录。上下文正常退出时发布整份输出；异常保留原文件并删除临时文件。同一个记录器只用于一次串行运行；回调本身只负责记录，执行及反馈由调用方继续驱动。

默认回放创建记录器并作为 SimulationRunner.on_route 注入。它记录实际派发顺序，不遍历运行结束后的到达顺序结果表。记录含 request_id、target_model、selected_endpoint_id、dispatched_at_ms、litellm_params；模拟完成事件、响应和状态快照不写入。默认模型为 deepseek-flash，端点 ID 仍为模拟标识；真实部署绑定及凭据由执行环境提供。

## 框架之外的会话发送

[integrations/litellm_gateway.py](../workload_profiling/integrations/litellm_gateway.py) 独立读取 routes.jsonl 中所选的记录，映射远端模型，再通过 OpenSSH 隧道调用代理。端点组可选，无默认组；原记录有组时保留，显式参数可覆盖。它不导入 Scheduler、不改变模拟和四份配置，也不向已结束的模拟运行注入反馈。模块提供 read_route、build_payload、ssh_tunnel、send_request、LiteLLMSession；命令见 [接入说明](litellm_gateway.md)。

send_request 每次调用执行一个 POST，返回 http_status、指定响应头、response_format（json/text/sse）和 response。普通响应解析为完整 JSON，非 JSON 保留文本；SSE 通过可选 on_response_chunk(text) 同步回调实时输出，完整原文也返回在 response 中。CLI 默认显示普通响应正文或实时流式事件及最终状态，HTTP 错误正文同样可见。响应不写入路由文件；一次发送标记已废弃，不再创建或检查。网络失败不自动重试或重连。

LiteLLMSession 是同步上下文管理器，在进入时建立一次 SSH 隧道并读取一次 KEY（或隐藏输入），所有 send(payload) 复用该隧道和凭据；退出时关闭隧道并清空会话持有的凭据。会话外调用 send 会拒绝。可在后续外围实验驱动器中复用：

```python
from workload_profiling.integrations.litellm_gateway import LiteLLMSession, build_payload

# route_records 由调用方提供；执行环境提供 KEY 和 SSH 认证。
with LiteLLMSession("ubuntu@118.195.173.231") as session:
    for route in route_records:
        payload = build_payload(route)
        result = session.send(payload)
        # 调用方处理 result；如需实时 SSE，可传 on_response_chunk 回调。
```

命令行 --send --session 复用相同接口，每次输入请求 ID 时重新读取 JSONL；回车发送默认选择，/quit、EOF 或 Ctrl+C 关闭会话。连续发送为串行执行，不模拟到达速率，不去重，不自动消费整份路由文件。

## 集成模拟后发送

[integrations/replay_litellm.py](../workload_profiling/integrations/replay_litellm.py) 提供 replay_and_send 和 replay-send CLI。统一入口新增命令映射，simulation.cli.execute 和 SimulationRunner 只增加可选 on_request 观测接口，核心调度逻辑没有改动。replay_and_send 进入会话后才调用 load_config 和原 execute，模拟结束后用 iter_routes 按交付文件顺序读取、校验所有 payload，再复用同一会话串行发送。

replay_and_send 和 build_payload 不接收全局 model 参数，CLI 也不再提供 --model。RouteOutputRecorder 显式保存 decision.request.target_model；build_payload 将该值作为真实请求 model。兼容旧交付文件中已有效的 litellm_params.model；旧 default 占位会拒绝。直接 LiteLLMAdapter 的 deployment_model/resolver 绑定能力保持原义，SSH 代理接入使用原请求的逻辑模型名。

```python
from workload_profiling.integrations.replay_litellm import replay_and_send

result = replay_and_send(
    ssh_target="ubuntu@118.195.173.231", limit=3,
    # 可传 config_path、source、source_format、output。
)
print(result.prepared_count, result.sent_count)
```

返回 ReplayDeliveryResult，包含原 RunResult（simulation）、prepared_count、sent_count、dry_run 和日志目录 log_path。可注入 tokenizer、progress、on_response(route, response)、log_dir；旧 log_file 兼容转换为去掉 .jsonl 后缀的目录，不能与 log_dir 同传。响应先记录日志，再交给 on_response，未提供时在终端显示完整普通响应，SSE 默认实时显示。dry_run=True 不创建会话，只模拟及校验；它仍发布路由输出及逐请求日志，sent_count=0。endpoint_groups=None 不补入默认组。

## 日志接口

RunLog 位于 common/run_log.py，默认在 results/logs 创建新运行目录，path 表示目录、run_file 表示 run.jsonl。record(event, request_id=..., **fields) 按请求分文件写入并立即 flush/fsync；没有 request_id 的事件写入 run.jsonl。request_path(request_id) 返回该请求的文件路径，request_filename 处理特殊字符、Windows 保留名和过长 ID，防止路径穿越与文件名混淆。scheduler_request(request) 保存 asdict(request) 的完整快照，simulation_result(result) 保存每条模拟状态。schema_version=2，sequence 为本次运行的全局事件顺序，可跨文件重建顺序。每条事件写完关闭文件，避免大规模回放耗尽文件句柄。目录独占创建，旧目录拒绝；异常退出保留已写事件并在 run.jsonl 记录 run_finished 及请求文件数量。凭据字段与 register_secret 注册的认证值脱敏；消息、SLO、metadata、真实回答和用量均保留。

```python
from workload_profiling.common.run_log import RunLog
from workload_profiling.simulation.cli import execute

with RunLog(mode="replay") as log:
    result = execute(config, on_request=log.scheduler_request)
    log.simulation_result(result)
print(log.path)
print(log.request_path("request_000000"))
```

execute 的 Python API 默认不创建日志文件，调用方通过 on_request 注入记录器；CLI 默认启用。LiteLLMSession 可传 run_log=log，并用 send(payload, request_id=..., selected_endpoint_id=...) 关联真实事件。send_request 是底层单次 HTTP 函数，本身不创建日志。会话在发送前记录真实 payload，收到 SSE 时记录片段，正常返回后记录完整响应；异常记录发送失败且不重试。使用同一 RunLog 注入模拟和会话即可关联完整数据流，replay_and_send 已完成这项编排。读取示例见 [运行日志](litellm_gateway.md#运行日志)。

DeliveryError 表示真实发送失败或结果未知，携带 request_id、successful_count、pending_count，停止后续请求并关闭会话；pending_count 不包含刚失败的请求。HTTP 错误正文先显示或交给 on_response，网络失败不重试。路由文件已经发布，异常不会删除。重新调用会重新模拟和发送，不是断点续发；取消、输入错误和认证失败也会正常释放已打开的会话。

这个编排复用完整模拟，真实发送发生在模拟结束之后，不是替换 SimulatedExecutor，也不会向已结束的 Scheduler 写入真实反馈。limit 为输入记录上限，拒绝请求没有路由交付，发送条数可小于 limit。测试用真实模拟和假 HTTP 验证 3/N 条派发顺序、虚拟结果与原 execute 一致、配置加载前认证、单会话复用、错误停止及文件保留。

## 验证

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
```

测试覆盖三条路径、模型池繁忙与硬容量区别、过滤/恢复、滚动额度、反馈匹配/迟到/实际用量、缓存边界、优先级、发送模式、策略协议、分层配置、LiteLLM参数/流式/错误、tokenizer 依赖、路由记录，以及外部接入的可选组映射、单条选择、请求上限、重复发送、会话认证复用、退出清理与失败不重试。单元测试使用假 HTTP 响应，不产生模型费用。

当前主线保留原始请求、冻结参考、tokenizer 缓存、四份有效配置、核心算法及模拟反馈。网页服务、HTML 可视化、报表导出与旧网页结果已删除；本地保存路由交付参数、逐请求日志及运行级记录。
