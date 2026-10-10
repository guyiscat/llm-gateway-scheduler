# LiteLLM 外部接入与会话发送

[项目首页](../README.md) · [使用指南](guide.md) · [开发接口](development.md)

会话与手动发送实现：[litellm_gateway.py](../workload_profiling/integrations/litellm_gateway.py)。集成模拟后发送实现：[replay_litellm.py](../workload_profiling/integrations/replay_litellm.py)。调度策略保持原流程，模拟层增加标准请求观测回调。默认 replay 产生本地 routes.jsonl 和请求日志；显式 replay-send 才启动完整登录、模拟及真实发送流程。

## 集成流程：启动登录，模拟 N 条，再连续发送

从根目录运行，默认模拟 3 条本地原始请求；也可将 --limit 改为其他正整数：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling replay-send `
  --limit 3 --ssh-target ubuntu@118.195.173.231
```

该命令直接执行真实发送，不需要 --send、--session 或手动逐条选择。执行顺序为：

1. 解析并校验启动参数，然后 LiteLLMSession 建立 SSH 隧道。输入一次 SSH 密码和一次 API Key；已配置密钥认证或 KEY 环境变量时使用现有认证。
2. 登录完成后才调用 load_config，再读取本地数据集、加载 tokenizer 并调用原 simulation.cli.execute。不会等到第一条请求派发才登录。
3. 原有参数生成、优先级、繁忙判断、即时/窗口调度、排序、路由和模拟反馈正常运行。RouteOutputRecorder 依实际派发顺序原子发布 routes.jsonl。
4. iter_routes 读取刚生成的交付记录，build_payload 校验全部真实调用参数。任何一条参数非法都会在第一个 POST 前终止。
5. 按记录顺序调用同一 session.send，逐条发送原始消息及控制参数、显示响应。不会重新登录，不会把请求改成 hi。
6. 全部发送完成或运行异常时关闭 SSH 隧道，清空会话持有的 API Key。

--limit 是输入记录数的上限；输入不足时只处理已有记录，拒绝或未派发请求不调用模型。默认输出仍为 workload_profiling/results/routes.jsonl；--output 可指定其他 JSONL。--source、--source-format 和 --config 复用原数据来源和配置机制，调度、到达模式、流式默认值等通过同一配置文件修改。

模型由每条请求的 target_model 决定，两个发送入口均已移除 --model 参数。未指定模型的模拟请求使用 simulation_adaptive.json 中的 target_model，默认 deepseek-flash；端点 supported_models 同步配置为 deepseek-flash。--endpoint-group 非必需且没有默认值。--max-tokens N、--no-max-tokens 只影响实际发送参数，未指定时保留路由中的上限；--proxy-port 默认 4000，--timeout 默认 60 秒。真实普通响应、SSE、错误正文均显示在终端并记录到运行日志。

本地检查完整流程而不登录或调用模型：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling replay-send `
  --limit 3 --dry-run
```

dry-run 同样执行模拟、发布路由文件并校验调用参数，发送数为 0。真实发送中任一 HTTP 错误、网络错误或超时会显示结果并停止后续请求，关闭隧道、返回退出码 2；已经发布的路由文件保留。不会重试、自动恢复或从上次位置续发，重新执行命令会重新模拟并发送所选记录。Ctrl+C 或 EOF 关闭会话并返回退出码 130。拒绝请求会被报告，CLI 最终返回退出码 2。

真实请求在模拟结束后串行发送，不按虚拟到达时间等待，也不将真实 usage、延迟或错误反馈写回模拟调度器。此入口用于“已有模拟路由 → 真实接口交付”验证；实时调度反馈仍使用下文所述在线接口。当前模拟 endpoint_a/b/c 尚未与远端部署逐一绑定，真实模型由请求的 target_model 指定，可选组由调用方提供。

## 每条请求的模型数据流

```text
原始记录 target_model（或 prompt.target_model / prompt.model）
→ WorkloadRequest.target_model
→ RequestParameterGenerator：缺失或 null 才使用配置默认值
→ SchedulerRequest.target_model
→ 硬过滤：端点 supported_models 匹配该请求
→ 路由交付记录 target_model
→ build_payload：LiteLLM 请求 model = 该记录 target_model
```

prompt 来源优先读取记录顶层 target_model，其次 prompt.target_model，最后 prompt.model；选中的值缺失或 null 时使用模拟配置默认值。lengths 来源优先读取 target_model，其次 model。显式空字符串、空白或非字符串会拒绝，不会被默认值覆盖。不同请求可使用不同模型，必须有兼容的本地端点配置及远端模型部署。

新路由文件明确保存 target_model；它是 SSH 代理发送时的权威模型名，优先于适配器记录的部署别名。旧文件没有该字段时可以沿用已有 litellm_params.model，但旧占位值 default 无法发送，需要按新配置重新回放生成。发送层不凭空补模型，也不统一覆盖整批请求。

## 数据路径

```text
本地 routes.jsonl
→ 选择一条已派发记录（默认第一条，也可指定 request_id）
→ 使用记录的 target_model 构建真实调用（endpoint_group 可选）
→ 默认打印预览
→ 显式 --send 时建立 SSH 隧道
→ POST /v1/chat/completions（--session 在同一隧道内连续发送）
→ 终端显示完整响应正文、HTTP 状态与 x-litellm-model-api-base
→ 关闭 SSH 隧道
```

SSH 登录的是 `ubuntu@118.195.173.231`。工具将本机临时 loopback 端口转发到远端 `127.0.0.1:4000`，再发送 HTTP 请求；与 SSH 登录后在服务器执行 curl 的目标相同。依赖系统 OpenSSH 客户端及 Python 标准库，不新增 SDK 或 SSH 包。

## 预览与发送

从项目根目录执行。只预览你给出的最小接入请求，不连接服务器：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling.integrations.litellm_gateway `
  --smoke
```

`--smoke` 保留选中路由记录的关联 ID，但将消息和生成控制改为 `hi`、非流式；默认不设置 max_tokens。这验证远端接入，不代表已经验证本地 endpoint_a/b/c 与真实部署的一一对应。

实际单次发送命令如下，按终端提示输入 SSH 密码及 LiteLLM API Key：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling.integrations.litellm_gateway `
  --smoke `
  --ssh-target ubuntu@118.195.173.231 --send
```

API Key 也可来自当前进程环境变量 `KEY`；没有该变量时使用隐藏交互输入。密码由 OpenSSH 隐藏读取，凭据不写入代码、配置、文档或进程命令行。系统已配置 SSH 密钥和 ssh-agent 时可直接使用，无须输入服务器密码。

## 一次登录，连续发送

使用 `--send --session` 启动交互会话。启动时登录一次 SSH、读取一次 API Key，随后每次手动选择请求都使用同一隧道：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling.integrations.litellm_gateway `
  --ssh-target ubuntu@118.195.173.231 --send --session
```

登录成功后显示 `request_id>` 提示，此时尚未发送任何模型请求：

- 输入 routes.jsonl 中的完整 request_id 并回车，发送该条路由记录。
- 直接回车，发送启动参数 `--request-id` 指定的请求；未指定时发送文件第一条。再次回车会再次实际发送该请求。
- 输入 `/quit`、按 Ctrl+C 或输入 EOF，结束会话并关闭隧道。

每次选择都会重新读取文件，因此可以读取模拟器后来发布的新路由结果。普通响应、HTTP 错误与流式响应均在终端显示；找不到记录、校验失败或网络失败会报告错误并回到提示符，不自动重发。HTTP 非成功状态也不会终止交互会话。连接断开后不自动重连，需退出并重新启动会话。会话结束后不再持有 API Key。

不加 `--session` 时仍发送所选的一条请求后退出；重新启动进程会建立新隧道。该工具不自动遍历整个文件，也不按模拟到达时间发送。`--smoke` 可与会话一起使用，但每次发送的消息都会变为 `hi`、非流式。

发送原始路由消息时去掉 `--smoke`。默认只选择第一条；指定请求只读取该条，不批量发送整份回放：

```powershell
# 本地预览某条路由的完整消息；不会发送
& .\.venv\Scripts\python.exe -m workload_profiling.integrations.litellm_gateway `
  --request-id request_000000
```

不传 `--max-tokens` 时，普通模式保留路由记录原有上限；记录没有上限或值为 null 时不发送 max_tokens。`--smoke` 默认也不发送该字段。显式 `--max-tokens N` 才覆盖为正整数 N；`--no-max-tokens` 则移除记录里已有的上限，且不能与 --max-tokens 同时使用。省略该字段后，由远端服务及模型的默认设置决定输出上限。

预览不携带 max_tokens 的完整路由请求：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling.integrations.litellm_gateway `
  --no-max-tokens
```

普通模式保留记录里的 messages、stream、metadata 和工具/生成控制参数。`--routes` 可选择其他 JSONL；`--proxy-port` 默认 4000，`--timeout` 默认 60 秒。

## 查看返回内容

实际发送后，非流式响应会直接显示在终端的 `response` 字段中，保留完整 JSON，包括 choices、message.content、可选 reasoning_content、finish_reason 及 usage。HTTP 错误的 error 正文也会显示；普通发送随后返回非零退出码，会话模式继续等待下一条请求。非 JSON 正文保留为文本。

流式请求按收到的 SSE 数据实时输出，包括 delta 内容、可选工具/推理字段、用量事件与 `[DONE]`；结束后再打印状态码和目标响应头，不重复打印整段流。流式内容按 UTF-8 增量解码，中文跨网络分块不会被拆坏。流中断时已收到的部分仍在终端可见，不自动重试。

响应保留在运行内存、终端和新的运行日志中，不写入 routes.jsonl。旧版本没有记录的响应无法追溯补看。

## 运行日志

实现：[common/run_log.py](../workload_profiling/common/run_log.py)。replay、replay-send 默认在 results/logs 新建运行目录，手动网关实际 --send 也生成日志，纯预览不创建。每条请求有自己的文件；同一文件的所有请求事件属于同一个 request_id。例如 --limit 2 成功运行后：

```text
workload_profiling/results/logs/<UTC时间>_<run_id>/
  run.jsonl                      本次运行开始、结束及运行级错误
  requests/
    request_000000.jsonl          第一个请求：完整参数、模拟结果、发送与响应
    request_000001.jsonl          第二个请求：完整参数、模拟结果、发送与响应
```

可用 `--log-dir PATH` 指定尚不存在的目录，不会复用旧目录。兼容旧 --log-file PATH.jsonl，将其转换为 PATH 目录；原文件已经存在时拒绝，不会覆盖。每条事件有 schema_version=2、run_id、UTC timestamp、sequence、event，请求事件另有 request_id。sequence 表示本次运行的全局事件顺序。SchedulerRequest.arrival_time 和 dispatched_at_ms 仍是模拟毫秒，不是日志墙钟时间。

| event | 记录内容 |
| --- | --- |
| run_started / run_finished | 只在 run.jsonl：模式、完成/失败/取消状态、错误信息和请求文件数量 |
| scheduler_request | 补齐默认值、计算优先级和 Token 估计后的完整请求对象，包含所有 dataclass 字段 |
| simulation_outcome | 模拟状态、拒绝原因、所选端点和派发时刻；不是实际 LiteLLM 结果 |
| litellm_send_started | 即将发送的真实 payload，包括组、输出上限等外层参数调整后的值 |
| litellm_stream_chunk | 已收到的 SSE 原文片段，逐块落盘 |
| litellm_response | HTTP 状态、目标响应头、response_format 和完整 response；错误正文也保留 |
| litellm_send_failed | 网络失败、超时或取消的类型及信息 |

标准请求在生成后、进入调度器前记录，所有已生成的合法请求都有记录，包括后来被拒绝的请求。模拟层有请求日志，手动网关只读已有路由文件，因此只记录发送与响应，不从交付参数猜造完整 SchedulerRequest。需要完整请求与响应关联时使用 replay-send。

每条事件写完立即 flush/fsync，异常不删除日志。开始发送记录必须落盘后才执行 POST；日志写入失败会停止流程。普通响应记录完整 JSON（包括回答、推理、工具调用、usage），非 JSON 响应记录文本；SSE 成功结束后另记录完整原文，流中断时已有片段仍保留。凭据字段和本次认证 API Key 脱敏，日志不采集 SSH 密码或 Authorization 请求头。消息及回答正文保留，不截断。

每条请求文件仍有多行，每行对应这条请求的一个阶段，而不是再次发送。普通成功请求通常有 scheduler_request、simulation_outcome、litellm_send_started、litellm_response 四行；流式请求还会有若干片段行。被拒绝或尚未发送的请求没有真实响应事件。不同运行中的相同请求 ID 分别保存，不互相覆盖。同一交互会话内再次发送同一个 ID，则继续追加到它自己的文件。

默认生成的 request_000000 等 ID 直接用作文件名。自定义 ID 的特殊字符、保留名及过长名称会转换成带摘要的安全文件名，正文保留原始 ID。日志不会将上游 ID 当作路径使用。

本次改造前已有的两份日志已按相同目录结构拆分，原始事件、时间、run_id、request_id 和正文原样保留，迁移记录仍为 schema_version=1。新运行使用上述 schema_version=2 格式。

读取最新运行中第一个请求的完整日志：

```powershell
$taskRun = (Get-ChildItem workload_profiling/results/logs -Directory |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
$taskLog = Join-Path $taskRun 'requests/request_000000.jsonl'
Get-Content -Encoding UTF8 -LiteralPath $taskLog |
  ForEach-Object { $_ | ConvertFrom-Json } |
  ConvertTo-Json -Depth 100
```

只看这条请求的标准参数或真实响应：

```powershell
Get-Content -Encoding UTF8 -LiteralPath $taskLog |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.event -eq 'scheduler_request' } |
  ForEach-Object { $_.scheduler_request | ConvertTo-Json -Depth 100 }

Get-Content -Encoding UTF8 -LiteralPath $taskLog |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.event -eq 'litellm_response' } |
  ForEach-Object { $_.result | ConvertTo-Json -Depth 100 }
```

replay-send --dry-run 仍创建逐请求日志，没有 litellm_send_started 或 litellm_response。默认每次新目录，历史运行不覆盖；同一个 request_id 跨运行由 run_id 区分。日志目录不得覆盖源数据或路由输出，也不能把路由输出放进该目录。

## 端点映射与请求结构

本地 endpoint_a/b/c 是模拟 ID。远端模型别名需要有效，端点组是可选字段：

- 模型来自每条请求的 target_model，缺省为 deepseek-flash；没有 --model 命令行参数。
- `--endpoint-group` 非必需，没有默认组。仅显式传入时覆盖 metadata.endpoint_group，可重复指定。省略时保留记录中明确提供的组；记录也没有时完全不发送该字段，`--smoke` 同样适用。不会补入 official、null 或空列表。原记录提供的组必须是非空字符串列表。
- `official` 只是先前部署提供的组名；是否参与实际筛选取决于远端配置或自定义处理，目前未核对服务器端实现。可选用 `--endpoint-group official`，也可完全省略。
- 本地 metadata.force_endpoint 不发送给远端；它仍保存在原始 routes.jsonl 中。
- api_base 属于客户端部署绑定参数，工具通过 SSH 隧道访问代理，不把它作为请求正文传入。

当前所有模拟端点没有各自的真实部署映射。未来需要区分各端点时，应在外部接入层配置相应组或部署映射；不要把三个模拟 ID 都解释为不同的远端 official 部署。本工具的转换不会修改原记录或调度配置。

路由模块的 request 是经过接入校验后的 **完整 SchedulerRequest**，不是只有文本，也不是未经规范化的原始 API JSON。它包含 request_id、target_model、messages、input_tokens、predicted_output_tokens、priority、arrival_time、max_tokens、stream、api_type、slo、metadata。Router 将其保存在 RouteDecision.request；LiteLLMAdapter 再提取调用字段生成 litellm_params。输入/预测长度、优先级与 SLO 用于调度，不直接变成模型生成参数。

## 多次发送与失败处理

工具默认预览；只有 `--send` 才连接并发送。原来跨进程的一次发送限制已移除，不再读取或创建 `workload_profiling/cache/litellm_send_once.json`。可以反复运行普通发送命令，也可使用 --session 连续发送。每次显式发送都执行一个 POST，没有按 request_id 去重；同一个请求 ID 可发送多次。

客户端仍不自动重试或跟随重定向，并通过 `x-litellm-num-retries: 0` 和 `num_retries=0` 请求关闭 LiteLLM 重试；正文同时设空的常规、上下文及内容策略 fallback 列表。超时不代表远端未执行，该工具不会自行补发。重试头含义见 [LiteLLM 官方说明](https://docs.litellm.ai/docs/proxy/request_headers)。

历史接入验证曾发送 `hi` / `deepseek-flash` / `official` / `max_tokens=1`，返回 HTTP 200、`x-litellm-model-api-base=https://api.deepseek.com/v1`、`x-litellm-attempted-retries=0`。此次会话改造只使用假响应测试，未新增真实模型调用。

该工具独立发送日志中的交付参数，不将真实反馈写回已结束的模拟 Scheduler。需要持续在线执行与反馈时，仍使用开发接口中的 on_dispatch、LiteLLMAdapter.execute 和 accept_feedback。
