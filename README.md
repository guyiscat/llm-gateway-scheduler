# Endpoint 自适应调度

当前主线：统一请求接入、硬过滤、目标模型池繁忙判断、即时/窗口调度、独立排序和统一端点路由。核心只处理优先级 0/1 的标准请求；数据预处理、流量发送和虚拟服务时间位于独立模拟层。默认 replay 在本地保存路由交付参数和标准请求日志；显式 replay-send 可先登录，再模拟 N 条请求并连续发送到 LiteLLM，记录真实参数及响应。

## 快速开始

在项目根目录运行：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 使用本地原始请求试运行前200条
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --limit 200 --output workload_profiling/results/routes.jsonl

# 默认配置回放全部原始请求；首次下载固定 tokenizer，不下载模型权重
& .\.venv\Scripts\python.exe -m workload_profiling replay

& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
```

默认读取 `workload_profiling/config/simulation_adaptive.json`，再加载同目录的端点及调度配置。Linux/macOS 使用 `.venv/bin/python`。

## 模拟调度后发送到 LiteLLM

下列命令启动时立即进行 SSH 登录及 API Key 认证，然后从本地数据集中模拟前 3 条请求，按实际派发顺序发送路由参数，全程复用一个 SSH 隧道：

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling replay-send `
  --limit 3 --ssh-target ubuntu@118.195.173.231
```

`--limit N` 控制模拟输入条数，默认 3；拒绝请求不发送，数据不足时只处理已有记录。`--endpoint-group` 可省略，没有默认组。真实回答或流式事件显示在终端并写入运行日志，路由交付记录仍保存到默认 routes.jsonl；可用 --output 另存。此命令直接执行真实发送，不需要 --send 或逐条输入请求 ID；--dry-run 则只模拟和校验，不登录、不调用模型。

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling replay-send `
  --limit 3 --dry-run
```

模拟仍使用虚拟时钟和模拟反馈；真实发送在整次模拟完成后串行执行，不把真实响应写回已结束的调度器。详细参数、登录与失败处理见 [LiteLLM 接入说明](docs/litellm_gateway.md)。

## 本地路由输出

默认路由输出为 `workload_profiling/results/routes.jsonl`，按实际派发顺序每行记录一次交付：

```json
{"request_id":"request_000000","target_model":"deepseek-flash","selected_endpoint_id":"endpoint_a","dispatched_at_ms":0,"litellm_params":{"model":"deepseek-flash","messages":[{"role":"user","content":"请求内容"}],"stream":false,"metadata":{"force_endpoint":"endpoint_a"}}}
```

`litellm_params` 由同一 LiteLLM 适配器生成，包含所选部署、原请求消息与调用参数。拒绝或未派发请求没有记录。整次回放及输入校验成功后，原子替换输出；再次运行覆盖同一路径，可用 `--output PATH.jsonl` 另存。统计、反馈、分类轨迹只保留在内存中供调度和 Python API 使用。

每条 SchedulerRequest 必须携带 target_model。请求明确提供的模型优先；未提供时使用 simulation_adaptive.json 的 target_model，当前默认 deepseek-flash。默认三个端点均支持此模型。交付记录保留 target_model，发送层将其用作 LiteLLM 的 model，不需要终端 --model 参数。直接适配器执行的端点绑定仍通过 api_base、deployment_model 或部署解析器提供；SSH 接入使用已部署的 LiteLLM 代理，凭据由执行环境提供。

## 请求与响应日志

replay、replay-send 默认为每次运行新建 `workload_profiling/results/logs/<时间戳>_<run_id>/`，终端显示目录。`requests/` 下每条请求一个 JSONL 文件，例如 `request_000000.jsonl`；它的各行只记录这条请求的完整 SchedulerRequest、模拟结果、真实发送 payload、HTTP 状态、完整响应及流式片段。被拒绝请求也有自己的文件。`run.jsonl` 单独记录运行开始、结束和运行级错误，不混入请求事件。

`--log-dir PATH` 可指定新的运行日志目录，已有目录会拒绝，不覆盖历史日志。旧 `--log-file PATH.jsonl` 兼容映射为不带 `.jsonl` 后缀的目录。日志逐条刷新并同步落盘，失败或中断时保留已写内容；API Key、认证头和凭据字段脱敏。单独 replay 或 --dry-run 只记录本地请求及模拟结果，不生成假的 LiteLLM 响应。详见 [日志读取方式](docs/litellm_gateway.md#运行日志)。

## 调度规则

| 请求 | 调度方式 | 端点范围 |
| --- | --- | --- |
| priority=1 | 即时 | 硬过滤后的全部候选 |
| priority=0，目标模型池不繁忙 | 即时 | 当前不繁忙且满足硬过滤的候选 |
| priority=0，目标模型池全部繁忙 | 窗口 | 派发时重新硬过滤的全部候选 |

端点繁忙条件取 OR：RPM 利用率达到 95%、TPM 利用率达到 95%、并发数达到上限减 3；目标模型池内全部健康候选繁忙才判定该池繁忙。高优先级跳过窗口，仍受模型、接口、健康、Cooldown、上下文及硬容量限制。默认模拟保留四档分布以复核旧基线：4 映射为核心的 1，其余映射为 0；新实验可使用 `--priority-assignment binary --high-priority-ratio 0.1`。

排序与路由可独立替换。默认仍是 `priority_then_light` 和 `min_rpm`，新增可选 `weighted_length`。历史性能与静态价格已进入路由上下文，Pareto 保留接入位置。LiteLLM 适配器支持参数转换及注入式执行，不新增 SDK 依赖。

## 结构与文档

```text
docs/                          使用指南、架构、开发接口
prompt数据/                     默认原始 prompt/response 数据
workload_profiling/
  core/                        标准请求、硬过滤、繁忙判断、窗口、统一路由、共享状态与历史
  policies/                    可替换排序/路由、冻结ECDF和输出分类策略
  adapters/                    LiteLLM参数转换、路由记录、执行反馈适配
  integrations/                LiteLLM SSH 会话、交互发送、模拟后发送编排
  simulation/                  来源、参数生成、fixed/random/burst发送、虚拟执行
  common/                      消息规范化、tokenizer、I/O与统计
  config/                      端点、调度、模拟三层配置及动态分类策略
  tests/                       主线行为、容量、分类、适配与路由记录验证
  data/artifacts/              百分位参考及校验元数据
  data/tokenizer/              可重用的固定tokenizer缓存
  results/routes.jsonl         本地 LiteLLM 路由交付记录
  results/logs/<运行>/         run.jsonl 与 requests/ 下逐请求日志
  cache/                       可重建的本地验证缓存
```

- [使用指南](docs/guide.md)：运行、配置、输入与结果。
- [架构](docs/architecture.md)：请求流程、容量与模块边界。
- [开发接口](docs/development.md)：Python API、策略扩展与路由记录接口。
- [LiteLLM 外部接入](docs/litellm_gateway.md)：SSH 会话复用、路由预览、多次发送、可选端点组与终端响应展示。
- [项目完整运行流程](项目完整运行流程.md)：从本地数据集到路由输出，每个行为对应的代码位置。

`prompt数据/` 是默认原始请求来源；冻结百分位参考、tokenizer 缓存及四份有效配置是当前运行所需输入。网页可视化、旧网页结果和实验报表导出已移除。
