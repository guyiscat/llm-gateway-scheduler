"""Expand every eligible assistant message into a context/response sample."""
# 核心数据流程：校验和规范化消息 → 逐条展开 assistant → 计算 request 长度。
# 完整历史与回答只在内存中使用，返回的长度记录不包含原文。
from __future__ import annotations

from copy import deepcopy


ROLES = {"system", "user", "assistant", "tool"}
# 主数据的固定列顺序；source_message_index/response_origin 用于补充原文定位。
COLUMNS = [
    "conversation_id", "request_index", "source_message_index", "response_origin",
    "input_tokens", "output_tokens", "total_tokens", "message_count",
    "user_message_count", "assistant_message_count", "system_message_count",
    "tool_message_count", "response_has_tool_calls", "tokenization_status",
]


class StructureError(ValueError):
    # 错误消息使用稳定的原因编码，便于按原因汇总异常，而不是写入原始内容。
    pass


def content_text(content) -> str:
    # Qwen 文本模板不能直接处理这里的 content 列表，所以按原序拼接 text 块。
    # 不插入额外分隔符；遇到未知块直接拒绝，避免静默遗漏内容或改变长度口径。
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                raise StructureError("unsupported_content_part")
            parts.append(part["text"])
        return "".join(parts)
    if content is None:
        raise StructureError("null_content_without_tool_calls")
    raise StructureError("unsupported_content_type")


def normalize_message(message: dict) -> dict:
    if not isinstance(message, dict):
        raise StructureError("message_not_object")
    if message.get("role") not in ROLES:
        raise StructureError("unsupported_role")
    # 修改副本，保留原始数据以及 tool_calls、reasoning_content 等附加字段。
    result = deepcopy(message)
    calls = result.get("tool_calls")
    if calls is not None:
        if result["role"] != "assistant" or not isinstance(calls, list):
            raise StructureError("invalid_tool_calls")
        for call in calls:
            if not isinstance(call, dict) or call.get("type", "function") != "function":
                raise StructureError("invalid_tool_call")
            function = call.get("function", call)
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise StructureError("invalid_tool_call_function")
            arguments = function.get("arguments")
            if not isinstance(arguments, (str, dict)):
                raise StructureError("invalid_tool_call_arguments")
            if isinstance(arguments, str):
                import json
                try:
                    # 仅验证参数字符串是合法 JSON，不重新序列化，保留源数据表达。
                    json.loads(arguments)
                except json.JSONDecodeError as error:
                    raise StructureError("invalid_tool_call_arguments_json") from error
    if result.get("content") is None and result["role"] == "assistant" and calls:
        # 工具调用消息允许没有文本。转为空字符串供模板处理，工具调用本身仍保留。
        result["content"] = ""
    else:
        result["content"] = content_text(result.get("content"))
    reasoning = result.get("reasoning_content")
    if reasoning is not None and not isinstance(reasoning, str):
        raise StructureError("invalid_reasoning_content")
    return result


