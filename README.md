# 大模型请求调度与工作负载分析

用于比较请求排序、重型分类和端点路由的离线仿真框架。输入与输出长度来自已记录的 prompt/response 或长度 JSONL，端点和服务时间均为模拟参数，时钟使用虚拟毫秒。

流程图对应的新入口是 **四档优先级、繁忙判定、即时/窗口自适应调度**，配置为 `simulation_adaptive.json`。第4档绕过窗口；其他请求仅在所有端点繁忙时进入窗口。路由沿用 min_rpm。默认旧配置仍保留 `heavy_only` 行为，方便历史实验对照。

## 快速开始

已验证 Python 3.12；在项目根目录执行 Windows PowerShell 命令：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 9 条长度样例：全部执行，不需要原始数据或 tokenizer
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --config workload_profiling/config/simulation_all_requests.json `
  --source examples/length_requests.jsonl --source-format lengths `
  --output-dir workload_profiling/results/simulation_example/all_requests

# 48 条自适应样例：三个调度分支均触发，不需要 tokenizer
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --config workload_profiling/config/simulation_adaptive.json `
  --source examples/adaptive_requests.jsonl --source-format lengths `
  --output-dir workload_profiling/results/simulation/adaptive_example

# 全量原始请求的自适应模式；首次下载固定 tokenizer，不下载模型权重
& .\.venv\Scripts\python.exe -m workload_profiling replay `
  --config workload_profiling/config/simulation_adaptive.json `
  --output-dir workload_profiling/results/simulation/adaptive

# 本地网页：http://127.0.0.1:8765；选择“自适应”可编辑繁忙阈值和四档优先级
& .\.venv\Scripts\python.exe -m workload_profiling web --open

& .\.venv\Scripts\python.exe -m workload_profiling --help
& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
```

Linux/macOS 用 `python3.12` 创建环境，解释器替换为 `.venv/bin/python`。所有运行命令统一使用 `python -m workload_profiling <子命令>`；旧根目录 baseline.py/demo.py 转发文件已删除。

## 只需要读这四份文档

| 文档 | 内容 |
| --- | --- |
| [使用指南](docs/guide.md) | 安装、配置、全部 CLI 参数、动态资源复现、示例与排错 |
| [架构与数据](docs/architecture.md) | 目录职责、请求口径、调度时序、结果文件和字段 |
| [开发接口](docs/development.md) | Python API、策略扩展、sender、HTTP 接口与代码迁移 |
| [实验说明](docs/experiments.md) | 对照实验、依赖、历史分析、研究结论与待完成项 |

[历史修改记录](docs/archive/history.md) 用于追溯。各次科学实验报告保存在 `workload_profiling/results/stage*/`。

## 目录结构

```text
.
├── requirements.txt             # 唯一依赖版本清单
├── docs/                        # 四份主题文档 + archive/history.md
├── examples/                    # 小型数据、排序/路由/sender 示例
├── experiments/                 # 对照实验、并发审计与诊断
└── workload_profiling/
    ├── __main__.py              # replay / web / stream / experiment
    ├── simulation/                # 调度模拟、分类、排序、路由、导出
    ├── web/                     # cli + simulation_console/profile_console + templates/
    ├── runtime/                 # 独立逐条计数、同步 sender、百分位策略
    ├── common/                  # tokenizer、规范化、校验、统一指标
    ├── stages/                  # Stage 1 / 2 / 2.1 历史分析
    ├── config/                  # 场景配置
    ├── tests/                   # 行为与已有科学产物集成测试
    └── data/ / results/ / cache/ # 派生数据、实验报告、可重建缓存
```

`prompt数据/` 是默认原始请求来源；`实验数据2/` 是历史网关资料，当前回放不读取。原始数据、历史科学产物及路径保持原样。

## 原窗口分类对照结论

当前平级场景下，输出最短优先平均延迟约 134.4631ms，固定 80% 分类约 134.8794ms，动态分类约 134.8741ms。动态机制已生效，尚未证明优于输出最短参照。真实模型执行、SLO、主引擎跨批排序等边界见[实验说明](docs/experiments.md)。

实验命令由 `examples.compare_*` 移至 `experiments.compare_*`，推荐使用统一 experiment 命令。仓库未附全部历史 `results/reproduction/`；缺少参考产物时先运行普通 replay。
