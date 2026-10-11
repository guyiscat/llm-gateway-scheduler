# 使用指南

[项目首页](../README.md) · [完整流程](../项目完整运行流程.md)

## 命令分工

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling litellm-login `
  --ssh-target ubuntu@118.195.173.231 --idle-timeout 600 `
  --endpoint-update-interval 30

& .\.venv\Scripts\python.exe -m workload_profiling replay-send --limit 3
& .\.venv\Scripts\python.exe -m workload_profiling litellm-status
& .\.venv\Scripts\python.exe -m workload_profiling litellm-close
```

登录命令完成认证及端点初始化后返回终端。每次发送复用后台会话，不输入密码或 API Key，不关闭隧道。发送结束后可继续执行发送命令；手动关闭或连续空闲超时才结束会话。默认空闲超时 600 秒，活动批次不超时，结束后重新计时。status 和周期维护不延长计时。关闭在途请求时，先取消未发送部分，再等待当前请求结束。

会话交接使用 Windows 进程 HANDLE，需系统 OpenSSH。保留登录时的终端窗口；其他终端也可连接同一后台。默认控制目录为 workload_profiling/cache/litellm_session。若使用 --session-dir，四种命令必须传同一目录。具体认证及异常处理见 [会话说明](litellm_gateway.md)。

## 登录参数

| 参数 | 含义 |
| --- | --- |
| --ssh-target USER@HOST | 必需，仅登录命令使用 |
| --config PATH | 请求/模拟配置；默认 simulation_adaptive.json |
| --idle-timeout SECONDS | 正数，默认 600，连续空闲后自动关闭 |
| --proxy-port PORT | 远端代理端口，默认 4000 |
| --timeout SECONDS | 登录等待及真实 HTTP 超时，默认 60 |
| --endpoint-update-interval SECONDS | 端点刷新间隔，默认 30 |
| --endpoint-state-dir PATH | 真实端点原始信息与维护文件目录 |
| --endpoint-rpm-limit / --endpoint-tpm-limit / --endpoint-concurrency-limit | 对所有发现端点使用已确认的本地容量覆盖值 |

初始化 GET 或目录校验失败，不启动发送会话、不调用模型；不会回退到虚构端点。已存在会话时重复登录不重新认证；不同目标或配置须先关闭。

## 发送参数

| 参数 | 含义 |
| --- | --- |
| --limit N | 数据集前 N 条，默认 3；为输入上限 |
| --source PATH | 输入文件，默认本地原始 prompt/response 数据集 |
| --source-format prompt / lengths | prompt 计算 Token；lengths 使用已提供长度 |
| --output PATH | 本批交付 JSONL，默认 results/routes.jsonl |
| --log-dir PATH | 为本批指定尚不存在的日志目录 |
| --config PATH | 默认沿用登录配置；调度设置必须与会话一致 |
| --endpoint-group GROUP | 可重复，限制发现的候选组；可省略，无默认值 |
| --max-tokens N | 覆盖实际输出上限 |
| --no-max-tokens | 省略实际 max_tokens，与上一项互斥 |

发送命令不接收 SSH、HTTP 超时或端点维护参数；这些参数在登录时确定。未登录时发送报错，避免隐式登录。同一后台只接受一批，另一并发批次直接拒绝。每次命令从数据集开头重新处理，重复执行可能再次产生同一记录的真实请求。

每条请求的模型来自 target_model。记录未提供时采用模拟配置默认 deepseek-flash，不提供终端全局 --model。记录的 messages、生成控制、stream、SLO、metadata 等由请求生成器保留；stream 由请求或配置决定。SLO 是数据字段，当前 min_rpm 策略没有实现 SLO 最优决策。

lengths 记录也必须包含有效 messages 才能真实发送。prompt 使用固定 tokenizer 估计长度，录制的 response 用于离线长度和默认 oracle 预测，不作为真实请求正文交付。

## 配置和端点来源

workload_profiling/config 只保留三份文件：

1. simulation_adaptive.json：scheduler_config 引用、fixed/random/burst 到达、两级优先级生成（binary，high_priority_ratio=0.1）、target_model、stream、max_tokens、预测及 service_time。显式 priority=0/1 优先；uniform 将缺少优先级的记录全部设为低优先级，不再接受四档配置。
2. scheduler_config.json：RPM/TPM 繁忙阈值默认 0.95，并发保留槽默认 3，窗口默认 16 条/20ms，路由 min_rpm，排序 light_heavy（低优先级窗口内轻请求优先、同类按到达顺序），额度与历史窗口。
3. pressure_threshold_policy.json：输出百分位门槛及 pressure_rules；它决定长短分类门槛，不修改端点容量或直接决定系统繁忙。

light_heavy 是独立排序策略，内部完成压力分档、轻重分类和轻请求优先排序。切换 fifo 或 weighted_length 后不运行动态分类，不加载百分位参考；对应轻重字段为 null。旧 priority_then_light 名称已移除。策略接口详见 [开发说明](development.md)。

端点来源只有登录后维护的 endpoint_state.json。静态 endpoint_config.json 已删除。未知 RPM/TPM/并发上限保留 null，不参与对应容量和繁忙判断。修改调度配置后须关闭并重新登录；真实用量会跨发送批次保留，按滚动窗口过期，不在每次维护时清零。

burst 是虚拟到达模式，把请求聚集到短时间段中以观察排程，不等同于真实并发压测。真实发送仍在模拟完成后串行执行。

## 输出及失败

routes.jsonl 保存 request_id、target_model、selected_endpoint_id、dispatched_at_ms 和 litellm_params。ID 用于状态归属；HTTP model 使用公共模型名，路由根据选中端点的唯一 tag 写入 metadata.endpoint_group。多个同模型部署无法唯一绑定时拒绝发送。

每批 results/logs 下生成新目录，requests/<请求文件名>.jsonl 的各行只属于同一条请求。scheduler_request 保存完整标准请求；simulation_outcome 保存虚拟结果；litellm_send_started、litellm_response 和 litellm_stream_chunk 保存实际发送和普通/流式响应。run.jsonl 保存运行级事件。sequence 为本批的全局事件顺序，不是请求编号；不同运行由 run_id 区分。

HTTP 错误或网络异常停止本批后续发送，返回 2，保留会话和已写日志，无自动重试。重新执行是新批次，不是续发。Ctrl+C 返回 130；检查状态及日志后决定是否关闭。关闭命令不会强制终止当前付费请求。

```powershell
Get-Content -Encoding UTF8 workload_profiling/results/routes.jsonl -TotalCount 2
& .\.venv\Scripts\python.exe -m workload_profiling replay-send --help
```

离线入口、网页、旧手动发送及 endpoint-watch 已删除；仍被主线调用的虚拟事件循环、模拟反馈和算法测试保留。
