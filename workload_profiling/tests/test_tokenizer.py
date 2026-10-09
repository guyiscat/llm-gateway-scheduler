"""Tokenizer loading must work with only the current declared dependencies."""
from importlib.metadata import PackageNotFoundError
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ..common.io import sha256_file
from ..common import tokenizer as tokenizer_module
from .support import temporary_directory


class TokenizerDependencyTests(unittest.TestCase):
    def test_valid_local_cache_does_not_require_removed_plotting_packages(self):
        fake = SimpleNamespace(chat_template="template", is_fast=True, model_max_length=1000)
        fake_transformers = SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **kw: fake))
        fake_hub = SimpleNamespace(snapshot_download=lambda **kw: self.fail("Valid local cache must not download"))
        def installed_version(name):
            if name in ("scipy", "matplotlib", "tzdata"):
                raise PackageNotFoundError(name)
            return "test-version"
        with temporary_directory() as directory:
            artifact = directory / "tokenizer.json"
            artifact.write_text("{}", encoding="utf-8")
            (directory / "tokenizer_manifest.json").write_text(json.dumps({
                "tokenizer_id": tokenizer_module.TOKENIZER_ID,
                "revision": tokenizer_module.TOKENIZER_REVISION,
                "files_sha256": {artifact.name: sha256_file(artifact)},
            }), encoding="utf-8")
            with patch.object(tokenizer_module, "TOKENIZER_DIR", directory), patch.object(
                    tokenizer_module, "version", side_effect=installed_version), patch.dict(
                    "sys.modules", {"transformers": fake_transformers, "huggingface_hub": fake_hub}):
                loaded, metadata = tokenizer_module.load_tokenizer()
            self.assertIs(loaded, fake)
            self.assertEqual(metadata["revision"], tokenizer_module.TOKENIZER_REVISION)
