"""Replay local requests and record dispatched LiteLLM parameters as JSONL."""
import os
from pathlib import Path

from ..adapters import RouteOutputRecorder
from ..common.io import sha256_file
from ..common.paths import DEFAULT_SOURCE, RESULTS
from ..common.tokenizer import load_tokenizer
from ..common.validation import positive_integer
from .engine import SimulationRunner
from .source import read_length_requests, read_prompt_requests

DEFAULT_OUTPUT = RESULTS / "routes.jsonl"


def execute(config, *, source=DEFAULT_SOURCE, source_format="prompt", limit=None,
            output=DEFAULT_OUTPUT, tokenizer=None, progress=None,
            strategy=None, batch_order=None, on_request=None, route_adapter=None):
    if limit is not None:
        positive_integer(limit, "limit")
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or (output.exists() and os.path.samefile(source, output)):
        raise ValueError("Route output must not overwrite the source file")
    if output.suffix.lower() != ".jsonl":
        raise ValueError("Route output must be a .jsonl file")
    source_hash = sha256_file(source)
    if source_format == "prompt":
        if tokenizer is None:
            tokenizer, _ = load_tokenizer()
        requests = read_prompt_requests(source, tokenizer, limit=limit, progress=progress)
    elif source_format == "lengths":
        requests = read_length_requests(source, limit=limit)
    else:
        raise ValueError("source_format must be prompt or lengths")
    with RouteOutputRecorder(output, (e.to_core() for e in config.endpoints), adapter=route_adapter) as recorder:
        result = SimulationRunner(config, strategy=strategy, batch_order=batch_order,
                                  on_route=recorder, on_request=on_request).run(requests)
        if sha256_file(source) != source_hash:
            raise ValueError("Source changed during replay")
    return result

