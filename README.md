# Endpoint 自适应调度

当前主线：统一请求接入、硬过滤、目标模型池繁忙判断、即时/窗口调度、独立排序和统一端点路由。核心只处理优先级 0/1 的标准请求；数据预处理、流量发送和虚拟服务时间位于独立模拟层。默认回放继续使用已有算法和参数，不调用网络模型。

## 快速开始

在项目根目录运行：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 48条请求覆盖三个调度分支，不需要 tokenizer
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --source examples/adaptive_requests.jsonl --source-format lengths `
  --output-dir workload_profiling/results/simulation/adaptive_example

# 默认配置回放全部原始请求；首次下载固定 tokenizer，不下载模型权重
& .\.venv\Scripts\python.exe -m workload_profiling replay

# 本地调度网页
& .\.venv\Scripts\python.exe -m workload_profiling web --open

& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
```

默认读取 `workload_profiling/config/simulation_adaptive.json`，再加载同目录的端点及调度配置。Linux/macOS 使用 `.venv/bin/python`。

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
examples/adaptive_requests.jsonl  唯一可执行样例
workload_profiling/
  core/                        标准请求、硬过滤、繁忙判断、窗口、统一路由、共享状态与历史
  policies/                    可替换排序/路由、冻结ECDF和输出分类策略
  adapters/                    LiteLLM参数转换、执行反馈适配
  simulation/                  来源、参数生成、fixed/random/burst发送、虚拟执行、导出
  common/                      消息规范化、tokenizer、I/O与统计
  web/                         调度控制台与页面
  config/                      端点、调度、模拟三层配置及动态分类策略
  tests/                       主线行为、容量、分类、接口与导出验证
  data/artifacts/              百分位参考及校验元数据
  data/tokenizer/              可重用的固定tokenizer缓存
  results/                     当前回放、百分位表格
  cache/                       可重建的本地验证缓存
```

- [使用指南](docs/guide.md)：运行、配置、输入与结果。
- [架构](docs/architecture.md)：请求流程、容量与模块边界。
- [开发接口](docs/development.md)：Python API、策略扩展与网页接口。

`prompt数据/` 保留默认原始请求来源；未被当前主线使用的备用实验数据已清理。百分位参考表格位于 `workload_profiling/results/reference_view/output-percentile-reference-table.html`。历史回放结果可按原配置重新生成，不作为项目依赖。

本次清理记录见 [cleanup_report.json](cleanup_report.json)。仍待处理的旧备份、验证资产和无权限临时目录列在报告中；[cleanup_pending.ps1](cleanup_pending.ps1) 默认只预览，需显式传入 `-Execute` 才会删除。原始请求、参考分布、tokenizer、四份有效配置及运行环境保留。
