"""路径只在此处定义；所有默认路径相对代码位置，不依赖启动目录。"""
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parent
DATA = PACKAGE / "data"
ARTIFACTS = DATA / "artifacts"
RESULTS = PACKAGE / "results"
CACHE = PACKAGE / "cache"
TOKENIZER_DIR = DATA / "tokenizer" / "Qwen3-8B"
POLICY_CONFIG = PACKAGE / "config" / "pressure_threshold_policy.json"
DEFAULT_SOURCE = ROOT / "prompt数据" / "prompt回答数据包_3168条" / "prompt回答.jsonl"
