# Endpoint 自适应调度

当前唯一运行主线：单独登录 LiteLLM → 从真实端点目录生成路由模拟 → 发送真实请求 → 手动或空闲超时关闭。核心保留模型过滤、优先级、繁忙判断、即时/窗口调度和可替换排序、路由策略。

## 运行

在项目根目录执行：

```powershell
# 已有 .venv 时无需重复创建
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 只登录并初始化端点，不调用模型；完成后返回终端
& .\.venv\Scripts\python.exe -m workload_profiling litellm-login `
  --ssh-target ubuntu@118.195.173.231 `
  --idle-timeout 600 --endpoint-update-interval 30

# 可以重复执行，每条命令是一批新的模拟与真实发送
& .\.venv\Scripts\python.exe -m workload_profiling replay-send --limit 3
& .\.venv\Scripts\python.exe -m workload_profiling replay-send --limit 5

& .\.venv\Scripts\python.exe -m workload_profiling litellm-status
& .\.venv\Scripts\python.exe -m workload_profiling litellm-close
```

首次登录输入一次 SSH 密码和一次 API Key；已设置密钥认证或 KEY 时使用现有认证。后台进程复用认证、隧道和端点维护模块。请保留执行登录的终端窗口；后台隧道交接目前使用 Windows。

默认连续空闲 600 秒后关闭。批次运行期间不触发空闲关闭，批次结束后重新计时；查询状态和周期维护不延长空闲时间。手动关闭会取消剩余未发送请求，等待正在执行的请求结束后关闭隧道。普通批次失败保留登录，不自动重试。

--limit N 为输入记录上限，默认 3，读取数据集前 N 条；每次命令会重新模拟并发送这些记录，不是续发。默认数据为根目录 prompt数据/prompt回答数据包_3168条/prompt回答.jsonl。拒绝请求不发送；输入不足时只处理已有记录。

## 配置与输出

- config/simulation_adaptive.json：数据请求默认参数、到达模式、预测及虚拟服务时间。
- config/scheduler_config.json：繁忙阈值、窗口、排序、路由及历史参数。
- config/pressure_threshold_policy.json：动态输出分类阈值映射和压力规则。
- results/endpoints/endpoint_catalog_raw.json：登录后获取的部署信息，凭据脱敏。
- results/endpoints/endpoint_state.json：筛选后的真实端点配置及本会话观测状态，是端点来源。
- results/routes.jsonl：最近一批按模拟派发顺序产生的交付记录；用 --output 另存。
- results/logs/<运行>/：每批独立目录，run.jsonl 记录运行级事件，requests/ 每条请求独立 JSONL，记录标准参数、实际 payload 和完整响应。

上述相对路径除 prompt数据 外均位于 workload_profiling。端点身份、支持模型、价格与容量来自 LiteLLM；缺失容量为 null，不沿用模拟值。--endpoint-group 可省略，没有默认值，选中端点的唯一 tag 由路由自动绑定。模型取每条 SchedulerRequest.target_model，默认 deepseek-flash；--max-tokens / --no-max-tokens 控制真实发送上限。

模拟仍使用虚拟时钟和模拟反馈，真实请求在整批模拟完成后串行发送。真实反馈维护独立的真实状态，不回写已经结束的模拟调度器。两个实例复用状态算法，但时间轴独立。

请求只分 priority=1（高）与 priority=0（低）。默认 binary 生成方式对缺少优先级的记录按种子分配，high_priority_ratio=0.1；显式值优先。高优先级绕过窗口，可使用软繁忙门槛之后尚未耗尽的容量，仍受硬限制。低优先级非繁忙时即时选择非繁忙端点，繁忙时聚合到窗口；默认窗口排序为轻请求优先、同类按到达顺序。长度轻重分类仍被使用，与请求优先级独立。

默认 ranking_policy="light_heavy"，实现为 policies/light_heavy_ranking.py 的 LightHeavyRankingPolicy，内部完成压力分档、分类门槛选择、轻重分类及稳定排序，通过统一排序接口返回结果。改为 fifo 或 weighted_length 后不再运行轻重分类。配置与接口见 [开发说明](docs/development.md)。

## 保留的模块

```text
workload_profiling/
  integrations/       登录、后台会话、控制客户端、批次发送及 SSH/HTTP
  endpoint_monitor/   真实端点发现、筛选、周期刷新及真实反馈
  simulation/         数据读取、请求生成、虚拟到达与排程；无独立命令行
  core/               调度核心、共享状态算法和性能历史
  policies/           排序、路由与输出分类
  adapters/           路由记录和参数转换
  common/             tokenizer、日志、I/O、校验和统计
  config/             三份有效配置
  data/               分类参考与固定 tokenizer 缓存
  results/            路由、逐请求日志和真实端点快照
  cache/              本地会话控制与可重建缓存
  tests/              主线与核心行为验证
```

离线 replay/dry-run、独立 endpoint-watch、旧手动网关命令、静态模拟端点配置及兼容转发模块已经删除。虚拟执行器继续用于主线的路由模拟。

测试：`& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .`。测试使用本地假服务，不产生模型费用。

[完整运行流程](项目完整运行流程.md) · [使用指南](docs/guide.md) · [LiteLLM 会话说明](docs/litellm_gateway.md) · [端点状态](docs/endpoint_state.md) · [架构](docs/architecture.md) · [开发接口](docs/development.md)
