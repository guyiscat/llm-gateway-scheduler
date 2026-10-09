"""固定 Qwen3 tokenizer/config/template，校验本地缓存；所有长度均为离线代理。"""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
import sys
import time

TOKENIZER_ID = "Qwen/Qwen3-8B"
# 固定到具体提交，避免远端 main 更新后，相同文本的长度或模板发生变化。
TOKENIZER_REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
from .paths import TOKENIZER_DIR
from .io import sha256_file
# 仅下载这些 tokenizer/config 文件；仓库中不存在的可选文件不会下载，不包含模型权重。
FILES = ["config.json", "tokenizer.json", "tokenizer_config.json", "vocab.json",
         "merges.txt", "special_tokens_map.json", "added_tokens.json", "chat_template.jinja"]
# 所有 request 共用同一模板参数，并原样写入 metadata。
# add_generation_prompt 补上待生成回答的开头；enable_thinking=False 固定其模板形式。
# truncation=False/padding=False 保留完整长度，不因模型最大上下文长度而截断。
CHAT_KWARGS = {
    "tokenize": True, "add_generation_prompt": True,
    "enable_thinking": False, "continue_final_message": False,
    "padding": False, "truncation": False, "return_tensors": None,
    "return_dict": False,
}


def load_tokenizer():
    from huggingface_hub import snapshot_download
    from httpx import HTTPError
    from transformers import AutoTokenizer

    TOKENIZER_DIR.mkdir(parents=True, exist_ok=True)
    lock = TOKENIZER_DIR / "tokenizer_manifest.json"
    if lock.exists():
        # 已下载时先核对仓库、版本和文件哈希；文件改变或缺失直接报错，不静默换版本。
        manifest = json.loads(lock.read_text(encoding="utf-8"))
        if manifest["tokenizer_id"] != TOKENIZER_ID or manifest["revision"] != TOKENIZER_REVISION:
            raise ValueError("Tokenizer lock has a different repository or revision")
        for name, expected in manifest["files_sha256"].items():
            if not (TOKENIZER_DIR / name).is_file() or sha256_file(TOKENIZER_DIR / name) != expected:
                raise ValueError(f"Tokenizer file missing or changed: {name}")
    else:
        revision = TOKENIZER_REVISION
        # 首次下载允许有限次数重试，处理临时网络错误；失败后仍抛出异常。
        for attempt in range(3):
            try:
                snapshot_download(
                    repo_id=TOKENIZER_ID, revision=revision, local_dir=TOKENIZER_DIR,
                    allow_patterns=FILES, max_workers=2,
                )
                break
            except (OSError, HTTPError) as exc:
                if attempt == 2:
                    raise
                print(f"Tokenizer download retry {attempt + 1}/2 ({type(exc).__name__})", file=sys.stderr, flush=True)
                time.sleep(2 * (attempt + 1))
        for name in ("tokenizer.json", "tokenizer_config.json", "config.json"):
            # 下载完成后检查必需文件，再写锁文件，避免不完整缓存被当作可用缓存。
            if not (TOKENIZER_DIR / name).is_file():
                raise ValueError(f"Required tokenizer file not downloaded: {name}")
        manifest = {
            "tokenizer_id": TOKENIZER_ID, "revision": revision,
            "download_allowlist": FILES,
            "files_sha256": {name: sha256_file(TOKENIZER_DIR / name)
                             for name in FILES if (TOKENIZER_DIR / name).is_file()},
        }
        lock.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # 下载完成后只从本地加载，不执行仓库自定义代码，也不加载模型。
    tokenizer = AutoTokenizer.from_pretrained(
        str(TOKENIZER_DIR), local_files_only=True, trust_remote_code=False,
        use_fast=True,
    )
    if not tokenizer.chat_template:
        raise ValueError("Qwen tokenizer has no chat template")
    # 同时记录模板哈希、参数、规范化口径和库版本，便于复现与解释长度差异。
    metadata = {
        **manifest, "purpose": "offline length construction tokenizer",
        "tokenizer_class": type(tokenizer).__name__, "is_fast": tokenizer.is_fast,
        "model_max_length": tokenizer.model_max_length,
        "loading": {"local_files_only": True, "trust_remote_code": False, "use_fast": True},
        "chat_template_sha256": hashlib.sha256(tokenizer.chat_template.encode("utf-8")).hexdigest(),
        "input_chat_template_parameters": CHAT_KWARGS,
        "tools": "Each source row's original prompt.tools is passed unchanged; absent => None",
        "other_prompt_parameters": "tool_choice and parallel_tool_calls are passed when present; the official template does not render these API controls",
        "output_encode_parameters": {"add_special_tokens": False, "truncation": False},
        "output_policy": "Assistant content text only, excluding tool_calls and separate reasoning_content",
        "content_policy": "Concatenate type=text content parts in source order without inserting separators; null assistant tool-call content becomes empty text in history",
        "template_policy": "Official Qwen template unchanged, including its historical reasoning handling and tool serialization",
        "packages": {name: version(name) for name in (
            "transformers", "tokenizers", "huggingface-hub", "pandas", "numpy", "pyarrow", "jinja2",
        )},
        "production_token_counts": False,
    }
    return tokenizer, metadata

