"""Recorded request counting, reference integrity and malformed input boundaries."""
from copy import deepcopy
import json
import unittest

from ..common.paths import ARTIFACTS
from ..policies import PercentileReference
from ..simulation.source import read_length_requests, read_prompt_requests
from .support import FakeTokenizer, temporary_directory


class SourceTests(unittest.TestCase):
    def test_structured_messages_tools_and_controls_are_preserved(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "c", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
                    {"role": "tool", "content": "result", "tool_call_id": "c"}]
        prompt = {"messages": messages, "tools": [{"type": "function", "function": {"name": "lookup"}}],
                  "tool_choice": "auto", "parallel_tool_calls": False, "temperature": .2, "top_p": .9}
        original = deepcopy(prompt)
        tokenizer = FakeTokenizer()
        with temporary_directory() as directory:
            path = directory / "requests.jsonl"
            path.write_text(json.dumps({"prompt": prompt, "response": [{"type": "text", "text": "answer"}]}), encoding="utf-8")
            request = next(read_prompt_requests(path, tokenizer))
        context, controls = tokenizer.templates[0]
        self.assertEqual(context[0]["content"], "ab")
        self.assertEqual(context[1]["content"], "")
        self.assertEqual(context[1]["tool_calls"], messages[1]["tool_calls"])
        self.assertEqual(controls["tools"], prompt["tools"])
        self.assertEqual(controls["tool_choice"], "auto")
        self.assertFalse(controls["parallel_tool_calls"])
        self.assertEqual(request.output_tokens, 6)
        self.assertEqual(request.metadata["temperature"], .2)
        self.assertEqual(request.metadata["top_p"], .9)
        self.assertEqual(request.metadata["parallel_tool_calls"], False)
        self.assertEqual(prompt, original)

    def test_invalid_length_records_include_line_without_source_text(self):
        for row in ({"input_tokens": -1, "output_tokens": 1}, {"input_tokens": True, "output_tokens": 1},
                    {"input_tokens": 1, "output_tokens": 1, "priority_level": "4"}, {"input_tokens": 1}):
            with self.subTest(row=row), temporary_directory() as directory:
                path = directory / "requests.jsonl"
                path.write_text(json.dumps(row), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "source line 1"):
                    list(read_length_requests(path))

    def test_invalid_prompt_and_response_are_rejected(self):
        rows = [{"prompt": {"messages": []}, "response": "a"},
                {"prompt": {"messages": [{"role": "unknown", "content": "secret"}]}, "response": "a"},
                {"prompt": {"messages": [{"role": "user", "content": "secret"}]}, "response": None}]
        with temporary_directory() as directory:
            path = directory / "requests.jsonl"
            for row in rows:
                path.write_text(json.dumps(row), encoding="utf-8")
                with self.subTest(row=row), self.assertRaises(ValueError) as error:
                    list(read_prompt_requests(path, FakeTokenizer()))
                self.assertNotIn("secret", str(error.exception))

    def test_frozen_mainline_reference_loads_and_metadata_detects_corruption(self):
        artifact = ARTIFACTS / "output_percentile_reference.parquet"
        metadata_path = ARTIFACTS / "output_reference_metadata.json"
        reference = PercentileReference.load(artifact, metadata_path)
        self.assertEqual(reference.sample_count, 5186)
        self.assertEqual(len(reference.values), 754)
        with temporary_directory() as directory:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["artifact_sha256"] = "invalid"
            path = directory / "metadata.json"
            path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum"):
                PercentileReference.load(artifact, path)
