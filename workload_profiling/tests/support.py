"""Shared isolated fixtures for mainline behavioral tests."""
from contextlib import contextmanager
from copy import deepcopy
import shutil
from uuid import uuid4
from ..common.paths import CACHE
from ..simulation import SimulationConfig, EndpointConfig, WorkloadRequest

@contextmanager
def temporary_directory():
    directory = (CACHE / ("simulation_test_" + uuid4().hex)).resolve()
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        if directory == CACHE.resolve() or not directory.is_relative_to(CACHE.resolve()):
            raise ValueError("Temporary directory escaped the test cache")
        shutil.rmtree(directory)


def endpoint(name="a", rpm=1000, tpm=1000000, concurrency=100, latency=1):
    return EndpointConfig(name, rpm, tpm, concurrency, latency, 1000000, 1000000)


def config(**overrides):
    defaults = dict(arrival_mode="fixed", priority_assignment="uniform", output_classification="tokens", busy_concurrency_reserve=0, endpoints=(endpoint(),), input_threshold_tokens=10,
                    output_threshold_tokens=10, batch_size=2, batch_wait_ms=5)
    return SimulationConfig(**(defaults | overrides))


def req(index, input_tokens=10, output_tokens=1):
    return WorkloadRequest(str(index), input_tokens, output_tokens)



class FakeTokenizer:
    """测试只需一个确定性 tokenizer，生产路径使用固定 Qwen tokenizer。"""
    def __init__(self):
        self.templates = []

    def apply_chat_template(self, messages, **kwargs):
        self.templates.append((deepcopy(messages), deepcopy(kwargs)))
        return list(range(5 + sum(len(message["content"]) for message in messages)))

    def encode(self, text, **kwargs):
        return list(range(len(text)))
