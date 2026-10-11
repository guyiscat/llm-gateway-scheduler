# 开发接口与验证

[项目首页](../README.md) · [架构](architecture.md) · [完整流程](../项目完整运行流程.md)

## 层次

core 只依赖标准请求、状态与策略协议，不导入 integrations 或模拟数据集。policies 提供排序、路由、ECDF 与动态长短分类；adapters 构建交付参数及原子路由记录。simulation 提供数据读取、标准参数生成、虚拟到达和执行反馈，仍被主线调用，没有独立 CLI。

integrations 管理 SSH/HTTP、后台生命周期和模拟后发送；endpoint_monitor 发现真实部署并维护本会话真实负载。兼容转发模块已删除，策略直接从 policies 导入。

## 主要接口

| 文件 / API | 作用 |
| --- | --- |
| core/request.py / SchedulerRequest | 每条完整标准请求，包括 target_model、Token 估计、messages 和控制参数 |
| core/scheduler.py / Scheduler | submit_request、tick、flush、accept_feedback、next_wakeup_ms |
| core/endpoint_filter.py / EndpointFilter | 兼容、可用与容量候选池，支持规则扩展 |
| core/busy_detector.py / LoadMonitor | RPM/TPM/并发软繁忙判断 |
| core/batch_window.py / BatchWindow | 聚合、截止与满批释放 |
| core/endpoint_state.py / EndpointStateManager | 预留、反馈修正、额度过期和恢复；真实与虚拟各自实例 |
| core/endpoint_history.py / EndpointHistory | 近期性能窗口 |
| policies/ranking.py / load_batch_order | fifo、light_heavy、weighted_length 及导入式策略 |
| policies/light_heavy_ranking.py / LightHeavyRankingPolicy | 独立完成压力分档、门槛选择、轻重分类和稳定排序 |
| policies/routing.py / load_strategy | min_rpm 或自定义同步选择器 |
| simulation/config.py / load_config | 必须注入已发现的 endpoint_configs；拒绝静态端点配置 |
| simulation/replay.py / execute | 原有虚拟排程编排，输出路由，支持 on_request 与 route_adapter |
| endpoint_monitor/service.py / EndpointMonitor | 首次 GET、周期刷新、状态文件、真实 send 与反馈 |
| integrations/replay_litellm.py / replay_and_send | 需要已认证 session 和它的 monitor，不再自行登录或提供离线模式 |

## 配置加载

simulation/config.py 的 load_scheduler_settings 先读取配置引用的 scheduler_config.json，登录前校验。load_config(path, endpoint_configs=...) 在批次开始注入 Monitor.endpoint_configs() 读取的真实能力，再附加 service_time 与 endpoint_service，构成虚拟排程配置。

config 只有请求/模拟、调度、动态分类三份 JSON。静态端点加载函数和 endpoint_config.json 已删除。SimulationConfig.endpoints 是本批注入后的运行时值，不是配置文件中的离线来源。程序化核心与测试仍可构造 EndpointConfig；测试夹具只存在 tests，不会成为生产默认端点。

排序支持 rank_requests 或既有 order_batch；路由支持 select_endpoint 或 select。导入策略采用 module:attribute，必须同步且候选/排序输出合法。默认 min_rpm 使用 RPM 利用率，RPM 上限未知时使用窗口请求数；价格、历史和 SLO 通过 RoutingContext 提供研究接口，未实现 Pareto 决策。

## 两级优先级与窗口排序

请求与运行统计只使用 priority=0/1，1 为高优先级。simulation/priority.py 保留显式值，默认 binary 根据 request_id 与种子生成两级值，high_priority_ratio 控制高优先级概率；uniform 只将缺少值的请求设为 0。旧 priority_level 输入被拒绝，不进行隐式映射。

高优先级绕过窗口，可以使用软繁忙门槛之后仍未用尽的容量，仍须满足模型、健康、Cooldown 与硬限额。低优先级在非繁忙时即时选择非繁忙端点；繁忙时进入窗口，释放后选择满足硬限制的端点。

默认 light_heavy 的键为 (-priority, heavy)。窗口只有低优先级，实际效果为轻请求优先，同类保持到达顺序。heavy 表示长度分类，不是另一档请求优先级；policies/classification.py、output_heavy_policy.py、percentile_reference.py、pressure_threshold_policy.json 与固定 Parquet 参考仍在主线使用。窗口释放时分类冻结，后续容量等待不会重新排序。weighted_length 则按 input_weight*input_tokens + output_weight*predicted_output_tokens 排序。

## 独立的轻重分类排序策略

scheduler_config.json 中 ranking_policy="light_heavy" 选择 LightHeavyRankingPolicy。它通过现有 rank_requests 接口一次完成压力计算 → 百分位门槛选择 → 输入/输出轻重分类 → 轻请求优先排序。分类组件由策略内部持有；Scheduler 与 SimulationRunner 不再加载或直接调用 OutputPercentileClassifier，也不再接受独立 classifier/classification_policy 参数。分类与百分位文件仍作为该策略的内部组件保留。

```python
from workload_profiling.policies.ranking import load_batch_order

policy = load_batch_order("light_heavy", config=scheduler_settings)
result = policy.rank_requests(requests, {
    "endpoints": endpoint_views,
    "now_ms": now_ms,
    "ready_count": ready_count,
    "batch_id": batch_id,
    "classification_context": "window",
})
ordered_requests = result.requests
classification_by_id = result.classification
pressure_snapshot = result.snapshot
```

requests 是 SchedulerRequest 序列，endpoints 是调度器全部端点快照，ready_count 不包含本次批次，now_ms 为当前毫秒时刻。RankingResult 定义在 policies/ranking.py，包含有序请求、逐请求分类字典和可选压力快照；结果与后续批次隔离。Scheduler 校验请求没有增加、遗漏或重复，再保存分类与日志、依序派发。普通 rank_requests 插件仍可返回请求序列，order_batch 插件仍可返回 ID 序列。

light_heavy 还提供可选 classify_requests(requests, context) 接口，供即时请求记录轻重信息；它返回 RankingResult，不改变即时调度路径，也不对即时队列排序。核心通过可选接口调用并记录结果，不计算具体压力或长度门槛。

output_classification 仍控制本策略内部的 tokens、percentile_fixed、percentile_dynamic 三种方式。默认动态方式保留 idle/normal/busy/critical 对应 0.9/0.8/0.7/0.6；并发上限未知时采用 normal。tokens 模式不加载历史百分位参考。切换 fifo、weighted_length 或不提供分类诊断的插件时，不运行动态分类，也不加载相关参考文件；记录中的轻重字段为 null，统计计入 unclassified_requests，避免将未分类请求误报为轻请求。自定义参考或分类器可通过 LightHeavyRankingPolicy(config, classifier=...) 注入，随后将整个策略作为 Scheduler.ranking_policy 或 SimulationRunner.batch_order 传入。

旧 priority_then_light 策略名与实现已移除，使用 light_heavy。RPM/TPM/并发决定是否进入窗口的软繁忙机制继续由 core/busy_detector.py 与 admission.py 维护，不属于该排序策略。

## 路由输入与输出

core/router.py 的 Router.route(request, candidates, now_ms) 接收完整 SchedulerRequest、已由调度器过滤的 EndpointView 序列和毫秒时刻；空候选返回 None。Router 通过注入的 EndpointStateManager 获取静态配置与近期性能，组成 RoutingContext(now_ms, endpoint_configs, performance)。

```python
selected_endpoint_id = policy.select_endpoint(request, candidates, context)
decision = router.route(request, candidates, now_ms)
# decision: RouteDecision(request, selected_endpoint_id, dispatched_at_ms, candidate_ids, metadata)
```

SchedulerRequest 的必填字段为 request_id、target_model、input_tokens、predicted_output_tokens；其他字段为 priority、arrival_time、messages、max_tokens、stream、api_type、slo、metadata。消息正文连同参数一起传入，路由无需重新解析原始数据集。EndpointView 包含三类容量上限及窗口用量、并发、健康、Cooldown 等。

策略只返回候选中的一个端点 ID，Router 校验后封装 RouteDecision，不发送 HTTP。adapters/litellm_adapter.py 的 build_params(decision, endpoint) 才生成交付参数；主线注入 Monitor.route_adapter()，使用公共模型名与唯一 endpoint_group 绑定部署。adapters/route_output.py 保存 request_id、target_model、selected_endpoint_id、dispatched_at_ms、litellm_params 到 routes.jsonl。

路由记录中的 metadata.force_endpoint 用于本地说明所选部署；integrations/litellm_gateway.py 的 build_payload 在真实 HTTP 发送前移除它，并筛选调用字段，加入禁用重试与 fallback 的控制参数。当前远端的部署绑定依赖公共 model 与 metadata.endpoint_group，不能仅依靠 force_endpoint。逐请求日志中的实际 payload 可核对最终发送内容。

## 后台生命周期

session_service.login 校验 → 建立隧道与读 Key → 隐藏 worker → 匿名管道交接 → 首次目录获取 → ready 返回。worker 用 WindowsProcessOwner 接管 SSH HANDLE，同一生命周期持有一个 LiteLLMSession 和 EndpointMonitor。

SessionBroker 处理受鉴权的本机 status、replay、close。串行锁避免多个批次覆盖输出或混用请求日志。--idle-timeout 控制连续空闲；活动批次不超时，结束后重计时。close 标记停止，不允许新批次或后续 POST，等待当前任务排空。

send_batch 消费输出事件、SSE 片段和最终结果，跨进程回传终端；没有自动重试。控制连接断开时无法保证已经发出的请求未执行，调用方必须查看日志。API Key 经匿名 stdin 传递，只保存在 session 内存；控制文件仅存本地令牌，目录设置当前用户 ACL。

启动失败清理本次拥有的控制文件与进程。正常退出停止维护、关闭隧道并删除锁。已有旧锁不自动删除，避免终止或覆盖其他活动会话。

## 批次与日志

replay_and_send 的 connected_session 与 connected_monitor 必须对应。参数包含 config_path、source/source_format、limit、output、endpoint_groups、max_tokens/no_max_tokens、log_dir，以及程序化 tokenizer、progress、on_response 和 cancelled 回调。

每批创建 RunLog，再用 session.recording(log) 临时绑定日志，结束恢复先前绑定，不结束登录。虚拟 execute 的 on_request 同时记录标准请求和交给 Monitor.capture_request。模拟结束后迭代本批交付记录，先校验全部 payload，再按顺序调用 Monitor.send。返回 ReplayDeliveryResult，包含 simulation、prepared_count、sent_count 和 log_path。

DeliveryError 保存 request_id、successful_count、pending_count。普通 HTTP/网络错误的 pending_count 不含已经失败的请求；发送前取消时包含尚未发送的当前请求。错误停止本批，后台会话保留。真实反馈不写回虚拟 Scheduler。

RunLog.record(event, request_id=...) 写入该请求文件，未提供 ID 时写入 run.jsonl。sequence 为本批统一递增序号，schema_version=2。即时 flush/fsync、凭据脱敏、目录独占；历史记录不覆盖。路由记录器也先写临时文件，模拟与源文件哈希校验成功后原子发布。

## 刷新同步与保留内容

Monitor 的刷新门先暂停新发送，再等真实在途与并发归零，GET 后 finally 解除暂停。反馈能继续排空。调度在线接口通过 scheduler_guard 控制提交和 tick，反馈使用 admission=False 或 accept_feedback(..., drain=False)，避免刷新与完成反馈死锁。

capacity 为 null 时对应利用率未知；未知容量不参与该项硬限制与软繁忙判断。固定百分位参考及 tokenizer 仍被主线使用。虚拟 SimulatedExecutor 与状态算法继续保留，因为当前仍先模拟再交付。不存在旧模拟端点文件、离线 CLI、旧手动网关 CLI、独立 endpoint-watch 或策略兼容转发。

## 验证

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
```

测试覆盖过滤、优先级、即时/窗口路径、排序、容量恢复、输出分类、原子交付、日志、刷新同步、重复批次、错误无重试、空闲超时和关闭排空。后台集成测试使用真实本机控制进程和假 HTTP 服务，替代 SSH/模型提供方，不产生模型费用。ACL 行为单独验证；代理环境的文件系统沙箱不允许实际 WRITE_DAC。
