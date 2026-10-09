# Endpoint 自适应调度

当前主线：统一请求接入、硬过滤、目标模型池繁忙判断、即时/窗口调度、独立排序和统一端点路由。核心只处理优先级 0/1 的标准请求；数据预处理、流量发送和虚拟服务时间位于独立模拟层。回放只在本地记录路由交付参数，默认不调用网络模型。

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

## 本地路由输出

唯一默认输出为 `workload_profiling/results/routes.jsonl`，按实际派发顺序每行记录一次交付：

```json
{"request_id":"request_000000","selected_endpoint_id":"endpoint_a","dispatched_at_ms":0,"litellm_params":{"model":"default","messages":[{"role":"user","content":"请求内容"}],"stream":false,"metadata":{"force_endpoint":"endpoint_a"}}}
```

`litellm_params` 由同一 LiteLLM 适配器生成，包含所选部署、原请求消息与调用参数。拒绝或未派发请求没有记录。整次回放及输入校验成功后，原子替换输出；再次运行覆盖同一路径，可用 `--output PATH.jsonl` 另存。统计、反馈、分类轨迹只保留在内存中供调度和 Python API 使用。

默认端点的 `default` 是模拟模型占位。真实调用前需配置 `api_base`、`deployment_model` 或部署解析器；`metadata.force_endpoint` 是网关约定。部署凭据由执行环境提供。

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
  simulation/                  来源、参数生成、fixed/random/burst发送、虚拟执行
  common/                      消息规范化、tokenizer、I/O与统计
  config/                      端点、调度、模拟三层配置及动态分类策略
  tests/                       主线行为、容量、分类、适配与路由记录验证
  data/artifacts/              百分位参考及校验元数据
  data/tokenizer/              可重用的固定tokenizer缓存
  results/routes.jsonl         本地 LiteLLM 路由交付记录
  cache/                       可重建的本地验证缓存
```

- [使用指南](docs/guide.md)：运行、配置、输入与结果。
- [架构](docs/architecture.md)：请求流程、容量与模块边界。
- [开发接口](docs/development.md)：Python API、策略扩展与路由记录接口。
- [项目完整运行流程](项目完整运行流程.md)：从本地数据集到路由输出，每个行为对应的代码位置。

`prompt数据/` 是默认原始请求来源；冻结百分位参考、tokenizer 缓存及四份有效配置是当前运行所需输入。网页可视化、旧网页结果和实验报表导出已移除。
