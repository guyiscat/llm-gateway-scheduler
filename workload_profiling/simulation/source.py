"""Read one original prompt/response pair at a time, without expanding history."""
import json
from pathlib import Path

from ..common.conversation import normalize_message
from ..common.tokenization import LengthCounter
from .models import WorkloadRequest
from .priority import priority_fields


def read_prompt_requests(source, tokenizer, *, limit=None, progress=None):
    counter = LengthCounter(tokenizer)
    with Path(source).open(encoding="utf-8-sig") as stream:
        for index, line in enumerate(stream):
            if limit is not None and index >= limit:
                break
            try:
                row = json.loads(line)
                prompt = row["prompt"]
                messages = prompt["messages"]
                if not isinstance(messages, list) or not messages:
                    raise ValueError("Empty messages")
                messages = [normalize_message(message) for message in messages]
                # Original API records sometimes end in assistant. Count their original
                # contexts rather than silently dropping or rewriting those requests.
                controls = {k: prompt[k] for k in ("tool_choice", "parallel_tool_calls") if k in prompt}
                lengths = counter.input_lengths(messages, tools=prompt.get("tools"), prompt_controls=controls)
                output = counter.output_length(row["response"])
                priorities = priority_fields(row)
            except (ValueError, TypeError, KeyError, IndexError) as error:
                raise ValueError(f"Invalid prompt/response or tokenization at source line {index + 1} ({type(error).__name__})") from None
            if progress and (index + 1) % 100 == 0:
                progress(index + 1)
            yield WorkloadRequest(f"request_{index:06d}", lengths["input_tokens"], output, index, **priorities)


def read_length_requests(source, *, limit=None):
    # Optional portable JSONL input for experiments; no tokenizer is needed.
    with Path(source).open(encoding="utf-8-sig") as stream:
        for index, line in enumerate(stream):
            if limit is not None and index >= limit:
                break
            try:
                row = json.loads(line)
                yield WorkloadRequest(row.get("request_id", f"request_{index:06d}"),
                                      row["input_tokens"], row["output_tokens"], index, **priority_fields(row))
            except (ValueError, TypeError, KeyError) as error:
                raise ValueError(f"Invalid length record at source line {index + 1} ({type(error).__name__})") from None
