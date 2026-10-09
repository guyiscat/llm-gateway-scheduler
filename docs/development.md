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
    request_id="req_001", target_model="default",
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

## 模拟、导出与网页

旧 API 继续可用：

```python
from workload_profiling.simulation import load_config, SimulationRunner, WorkloadRequest

config = load_config()
runner = SimulationRunner(config)
result = runner.run([WorkloadRequest("sample", 100, 300)])
```

WorkloadRequest 是离线实际观测，output_tokens 为实际输出；可附上 priority=0/1、predicted_output_tokens、messages、target_model、stream、max_tokens、slo、metadata。RequestParameterGenerator.prepare 生成标准请求和独立观测；SimulatedRequestSender.schedule 只决定到达时间，submit(request,scheduler) 使用同一入口。真实接入绕过这些模拟模块。

SimulationRunner 每实例运行一次。可注入 strategy、batch_order、classification_policy、executor；executor.plan(decision,observation) 返回 ExecutionResult，便于模拟失败/限流；不另维护资源计数。CLI execute 负责来源校验、tokenizer、回放与原子单文件导出。百分位模式生成自包含 replay_config.json。

网页接口保持：GET /、GET /api/simulation/config、POST /api/simulation/run、GET /api/simulation/status、GET /api/simulation/requests?offset=0&limit=100、GET /simulation/requests.csv。POST 可传 {"limit":200} 或完整组合 config；limit 省略/null表示全部。运行中拒绝第二个任务，分页 limit 1～200，结果保存到 results/simulation/web/唯一ID。网页只允许内置策略，不运行网络模型。

## 验证

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
```

当前 125 项测试覆盖原基线、三条路径、模型池繁忙与硬容量区别、过滤/恢复、滚动额度、反馈匹配/迟到/实际用量、缓存边界、二档概率、随机发送、策略协议、分层配置、LiteLLM参数/流式/错误、本地 HTTP 及 tokenizer 的当前依赖。完整 3168 条回放与重构前快照逐项比较，原字段一致；策略类模块路径允许迁移，新增字段单独验证。

## 清理边界与待处理目录

已清理未使用的备用实验数据、重复模拟输出、过时的数据说明、下载临时记录、编译缓存和未调用的辅助函数。当前输入口径统一在使用指南中说明；tokenizer 缓存校验文件改为 tokenizer_manifest.json，不再要求已移除的 scipy/matplotlib 元数据依赖。

[cleanup_report.json](../cleanup_report.json) 保存清理清单、输入与参考哈希、测试/完整回放验证，以及已删除生成结果的运行配置，便于需要时重建。没有新建文档备份目录。

`.checkpoints/` 和 `workload_profiling/cache/modular_refactor/` 包含旧备份与验证资产，自动审批拒绝永久删除，因此仍保留，属于待处理内容而非核心依赖。`cache/tmp8pamrgwx` 和 `cache/tmptxhn8jwj` 删除时返回 UnauthorizedAccessException；没有擅自修改它们的所有者或 ACL。

[cleanup_pending.ps1](../cleanup_pending.ps1) 列出这四个具体目标。默认运行只预览；确认永久删除后由用户显式传入 -Execute。脚本校验工作区边界，遇到链接目录或权限错误即停止该目标，不修改文件权限。临时目录仍拒绝访问时，需用户通过 Windows 文件夹属性的安全设置处理权限，或交由管理员处理。不要把删除目标改为项目根目录、.git 或 .venv。
