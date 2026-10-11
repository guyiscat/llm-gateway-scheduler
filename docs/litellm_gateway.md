# LiteLLM 登录、发送与关闭

[项目首页](../README.md) · [参数指南](guide.md) · [端点状态](endpoint_state.md)

## 新命令流程

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling litellm-login `
  --ssh-target ubuntu@118.195.173.231 --idle-timeout 600 `
  --endpoint-update-interval 30

& .\.venv\Scripts\python.exe -m workload_profiling replay-send --limit 3
& .\.venv\Scripts\python.exe -m workload_profiling replay-send --limit 5
& .\.venv\Scripts\python.exe -m workload_profiling litellm-status
& .\.venv\Scripts\python.exe -m workload_profiling litellm-close
```

登录、发送、关闭是独立命令。登录完成认证、后台初始化和首次端点发现后返回终端，不调用模型。后续发送不登录、不结束隧道，成功或普通批次失败后仍可发送下一批。--ssh-target、--timeout、--proxy-port、端点刷新及容量覆盖参数只属于登录命令。发送必须已有就绪会话。

每次 replay-send 读取数据集前 N 条并重新模拟、发送，不是断点续发。默认 N=3；输入不足或部分被拒绝时实际发送少于 N。一个会话仅允许一批运行，并发提交拒绝且不调用模型。

## 实现与认证

| 文件 | 职责 |
| --- | --- |
| [session_cli.py](../workload_profiling/integrations/session_cli.py) | 登录、状态、关闭的命令解析 |
| [session_service.py](../workload_profiling/integrations/session_service.py) | 后台进程、空闲计时、串行任务和关闭排空 |
| [session_control.py](../workload_profiling/integrations/session_control.py) | 本地控制客户端、私有目录与 SSH 进程 HANDLE |
| [litellm_gateway.py](../workload_profiling/integrations/litellm_gateway.py) | OpenSSH 隧道、可复用 HTTP 会话、参数转换及响应显示 |
| [replay_litellm.py](../workload_profiling/integrations/replay_litellm.py) | 本批模拟、记录校验、真实发送和日志 |

OpenSSH 将本机随机 loopback 端口转发到远端 127.0.0.1:4000；密码由 OpenSSH 在终端输入。LiteLLM API Key 使用 KEY 环境变量或隐藏输入，登录时读取一次。

登录进程通过匿名 stdin 管道把连接信息和 API Key 交给隐藏后台，不通过命令行参数或磁盘文件传递。后台持有已认证 SSH 进程的 Windows HANDLE，避免按可能复用的 PID 错杀进程。首次 GET /model/info 成功后才确认交接；启动失败关闭本次隧道，不发送模型请求。

后台控制服务只绑定 127.0.0.1，使用随机控制令牌鉴权。默认 workload_profiling/cache/litellm_session 仅当前 Windows 用户可访问，保存 session.json、control.key、.session.lock 和诊断 worker.log；控制令牌不是 LiteLLM API Key。无法设置目录访问权限时拒绝登录。真实认证密钥只留在后台内存，关闭后清空。

当前后台交接使用 Windows。保留登录时的终端窗口：原生 OpenSSH 在此窗口完成交互认证，关闭窗口可能终止隧道。后续命令可从其他终端执行。--session-dir 指定其他私有目录时，登录、发送、状态、关闭须使用同一目录。

## 空闲与手动关闭

--idle-timeout SECONDS 必须为正数，默认 600 秒，即连续空闲十分钟：

- 登录初始化完成后开始计时。
- 整个批次准备、模拟和发送期间不触发空闲关闭。
- 批次成功或失败结束后重新计时。
- status 查询和端点维护 GET 不延长计时。

status 返回 ready/closing、busy、配置和状态目录、维护间隔及剩余空闲秒数；busy 时剩余秒数为 null。

close 立即标记 closing，拒绝新批次并取消未发送部分；等待正在执行的请求及维护退出，再关闭 SSH。命令先返回关闭已受理的信息，后台随后完成排空。若仍在模拟准备阶段，会在实际发送前检查关闭标志。关闭不会自动重发，也不强制打断已付费的在途请求。

隧道自行退出时后台停止接收并清理。程序不自动重连或重新认证。正常关闭删除会话控制文件及端点维护锁，保留日志和状态快照。强制杀进程可能留下锁；确认锁中的进程已经退出后才手动删除 .session.lock 或 .endpoint_monitor.lock，程序不会自动猜测并清除旧锁。

## 请求与实际交付

每条 SchedulerRequest 自带 target_model；原始记录缺失或 null 才采用请求配置默认 deepseek-flash。HTTP model 使用公共模型名。selected_endpoint_id 是真实部署 ID，用于本地状态归属及响应核对；所选部署的唯一 tag 自动绑定到 metadata.endpoint_group。当前远端 policy shell 不接受部署 ID 作为公共模型名。

--endpoint-group 可省略，无默认值。显式指定时限制本地候选，并在发送前核对组与选中部署是否匹配。多个同模型部署无法通过唯一 tag 绑定时拒绝发送；不会偷偷切换部署。其他 metadata 保留，本地 force_endpoint 或 api_base 不作为远端调用控制。

--max-tokens N 覆盖真实输出上限；--no-max-tokens 删除记录中的上限；两者互斥。均未传时保留记录值，null 则不发送该字段。模型最终输出仍受提供方约束。

stream 由请求或配置决定。普通 JSON/文本响应完整显示；SSE 按收到的片段实时回传到发送终端并保存完整原文。实际 payload 设置 num_retries=0 和空 fallback 链，不自动重试或跨组回退。

## 模拟、真实状态和错误

本批先用发现的端点能力完成虚拟调度，产生 routes.jsonl，再校验全部参数并按记录顺序串行 POST。模拟时间、模拟完成反馈与真实使用量分开；真实反馈不回写已经结束的虚拟 Scheduler。Monitor 保留跨批次真实 RPM/TPM、Cooldown 和性能历史，周期刷新不清零。详见 [端点维护](endpoint_state.md)。

普通 HTTP 错误、网络超时、非法参数或调度设置变化停止本批，发送命令返回 2，后台会话保留。HTTP 错误正文可见，之前的日志和交付文件保留。重新执行会创建新批次并再次发送所选记录。

Ctrl+C 返回 130，断开本次控制连接；后台在写输出时发现断开后停止余下发送。已经发出的请求可能尚未结束或结果未知。用 status 和日志检查，不自动续发；需要结束会话则执行 close。

## 运行日志

每批新建 results/logs/<时间戳>_<run_id>/，--log-dir 可指定尚不存在的目录。同一 request_id 在不同批次可能重复，由 run_id 和目录区分。

```text
<运行目录>/
  run.jsonl
  requests/
    request_000000.jsonl
    request_000001.jsonl
```

requests 下每个文件只属于一条请求，但可有多行事件：

| 事件 | 内容 |
| --- | --- |
| scheduler_request | 完整标准请求、模型、messages、Token、priority、stream、SLO、metadata |
| simulation_outcome | 本条虚拟调度结果 |
| litellm_send_started | 实际 payload 与选中端点 |
| litellm_stream_chunk | 实时 SSE 片段 |
| litellm_response | HTTP 状态、指定响应头及完整返回正文 |
| litellm_send_failed | 请求异常或结果未知 |

run.jsonl 记录运行级开始、结束或错误。sequence 是该运行所有事件的统一顺序，不是请求 ID 或文件行号。每条事件立即 flush/fsync，凭据字段和注册认证值脱敏；回答内容仍保留。

旧手动网关、单条 smoke 发送、离线 replay/dry-run 和 endpoint-watch 已移除。litellm_gateway 现在仅为后台主线提供 SSH/HTTP 函数，不再有独立命令行入口。
