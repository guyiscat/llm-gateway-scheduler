# LiteLLM 端点发现与状态维护

[项目首页](../README.md) · [运行与发送](litellm_gateway.md) · [开发接口](development.md)

## 端点唯一来源

端点只来自已登录 LiteLLM 的 GET /model/info。登录后首次获取成功并筛选、保存状态，发送命令才可接入。静态模拟端点配置已删除，不回退到 endpoint_a/b/c 或虚构的容量。

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling litellm-login `
  --ssh-target ubuntu@118.195.173.231 --endpoint-update-interval 30
& .\.venv\Scripts\python.exe -m workload_profiling replay-send --limit 3
```

--endpoint-update-interval 为正秒数，默认 30；--endpoint-state-dir 指定保存目录，默认 workload_profiling/results/endpoints。端点维护与登录同寿命，批次完成后不停止；关闭或空闲超时后停止。不再提供独立 endpoint-watch 命令。

## 文件和字段

| 文件 | 内容 |
| --- | --- |
| endpoint_catalog_raw.json | /model/info 的原始结构，API Key 等敏感字段脱敏 |
| endpoint_state.json | 筛选后的配置、当前运行状态及刷新阶段 |
| .endpoint_monitor.lock | 后台维护进程 PID，保证同目录只有一个维护者 |

catalog.py 提取 model_info.id 为 endpoint_id；model_name 为支持的公共模型；tags 用于唯一 endpoint_group 绑定。保留 API 类型、基础 URL、价格、上下文限制及已明确提供的容量。未使用字段和 null 杂项不进入端点配置。

RPM、TPM、并发上限缺失时为 null，对应利用率也是未知；不采用旧模拟容量代替。可用登录命令的 --endpoint-rpm-limit、--endpoint-tpm-limit、--endpoint-concurrency-limit 显式覆盖所有发现端点，正整数才有效，来源标记为 local_override。

state_scope=this_process_requests 表示状态仅覆盖这个后台会话发送的请求。/model/info 是部署目录，不提供项目可直接使用的全局实时负载；其他客户端的用量未知。状态文件不能据此代表整台 LiteLLM 的 RPM/TPM 或真实并发。

## 维护和发送同步

EndpointMonitor 位于 [service.py](../workload_profiling/endpoint_monitor/service.py)，字段转换与状态加载位于 [catalog.py](../workload_profiling/endpoint_monitor/catalog.py)。

刷新到期先设置 refresh_pending，暂停新发送；等待 inflight 为空且所有本地并发为零，再拉取部署目录。期间已有请求的完成反馈可以执行，从而排空。GET 结束后校验、更新配置与文件，最后解除暂停；刷新异常也会解除暂停并记录 last_refresh_error。

不能用“RPM 为零”代替无在途请求：流式请求可超过 60 秒，RPM 记录已过期但仍在执行。当前门槛使用本进程在途及并发，能够覆盖这种情况。关闭会话时也取消等待刷新门的后续发送。

目录更新保留累计请求/Token、滚动额度、性能历史和管理健康状态。移除的端点变为目录不可用，重新出现后恢复配置，保留相关状态。周期维护从上一轮尝试结束后等待间隔，不是同时发多个 GET。

## 两个时钟

共享状态算法为 core/endpoint_state.py 的 EndpointStateManager，历史算法为 core/endpoint_history.py。真实 Monitor 采用 Unix 毫秒，虚拟 Scheduler 采用模拟毫秒，实例完全独立。

每批模拟从状态文件提取真实端点能力，但不把真实当前 RPM/TPM 计数或真实历史注入虚拟 Scheduler。虚拟执行器用于产出路由；真实交付时 Monitor 再做容量预留和状态校验。

真实发送前按 SchedulerRequest 的估计长度预留 RPM、TPM 和并发；响应后用 prompt_tokens/completion_tokens 修正额度、释放并发并更新健康、Cooldown 与历史。SSE 读取 usage 事件；缺少用量时保留估计，不编造实际值。响应有 x-litellm-model-id 时核对所选部署，归属不一致则停止并记录不确定状态。

RPM/TPM 按派发时间形成滚动 60 秒窗口，在状态推进或读取时移除到期记录；并非每个自然分钟清零。批次结束不清零，目录刷新也不清零。后台每次维护发布状态，真实发送前后也发布；这不是由 /model/info 获得实时用量。

### RPM 更新的代码与含义

1. endpoint_monitor/catalog.py 从部署信息读取 rpm 作为 rpm_limit，不从 GET 响应中获得当前请求计数。
2. endpoint_monitor/service.py 的 send 在 POST 前调用 EndpointStateManager.reserve，加入一条派发时间记录；requests_in_window 为仍在滚动窗口内的记录数量，因此增加 1。
3. core/endpoint_state.py 的 complete 释放并发、修正 Token；已尝试请求即使失败也保留 RPM 记录，不在响应完成时减 1。若预留后未能发出请求，当前也保留该记录，属于保守计数。
4. expire 在 now_ms >= dispatched_at_ms + 60000 时移除该记录。发送预留、反馈、快照及 Monitor.publish 都会推进并清理到期记录。后台还在周期维护时发布；磁盘快照不会在没有发布的每一个毫秒自动改写。

例如 10:00:10 和 10:00:40 发出两条请求，10:01:00 时仍计 2；10:01:10 第一条移出，10:01:40 第二条移出。新发送的容量检查先处理过期项，不依赖磁盘快照是否恰好已更新。

### 没有 TPM 上限的端点

tpm_limit=null 表示未知，不表示上限为 0，也不保证供应商没有限额。仍记录 tokens_in_window：发送前使用 input_tokens + predicted_output_tokens，响应后使用有效 usage 替换已知部分，缺少 usage 则保留对应估计。

EndpointView.can_accept 不对未知 TPM 做本地硬限制，core/busy_detector.py 也跳过这一维的软繁忙判断；tpm_utilization 保持 null。已知 RPM、并发及健康/Cooldown 条件照常生效。若三个容量上限都未知，负载阈值无法证明该端点繁忙，仍可能收到上游 429；当前不会自动重试。明确知道限额时，可在登录时传 --endpoint-tpm-limit 正整数，作为本地覆盖，而非远端实测值。

当前保存的目录快照中四个端点的 rpm_limit 均为 60，tpm_limit 与 concurrency_limit 均为 null。因此对应端点默认在窗口请求数达到 57 时因 RPM 判为繁忙，达到 60 时触及 RPM 硬上限。并发容量未知时，动态输出分类采用 normal 对应百分位门槛 0.8；这与调度路径的繁忙判断是两套独立机制。此处描述的是已有本地快照，新登录会重新发现实际配置。

## 共享算法为何保留

虚拟排程要依靠容量预留、完成反馈、窗口过期及恢复时刻才能跑通；真实发送同样需要这些算法。因此保留共享状态与虚拟排程，不维护旧的本地静态端点来源，也不输出一份模拟端点状态文件。

Monitor.capture_request 暂存本批完整请求；批次结束释放正文引用。逐请求日志保存完整参数和响应，滚动用量与性能历史继续保留。会话关闭后保留最后快照，但下一次登录不恢复其旧运行计数；不同后台运行之间的负载连续性没有实现。

## 在线接口

如未来将真实反馈直接接入 Scheduler，可注入 Monitor.states，并使用 scheduler_guard() 包围提交/tick；accept_feedback(..., drain=False) 或 scheduler_guard(admission=False) 允许在刷新排空期提交完成反馈。反馈不能被新请求暂停门阻塞。当前命令行仍是先虚拟模拟再真实发送，没有启用该在线反馈调度方式。
