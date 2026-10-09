from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from ..simulation.classification import OutputPercentileClassifier
from ..simulation.models import EndpointView
from ..simulation.cli import execute
from ..common.paths import PACKAGE
from ..common.io import sha256_file
from ..policies import PercentileReference
from .support import config, endpoint, req, temporary_directory


class DynamicEntryTests(unittest.TestCase):
    def settings(self, **changes):
        return config(**(dict(output_classification="percentile_dynamic",
                             batch_order="priority_then_light", batch_size=1,
                             endpoints=(endpoint(concurrency=1, latency=9),)) | changes))

    def profile(self):
        return json.loads((PACKAGE / "config/pressure_threshold_policy.json").read_text(encoding="utf-8"))

    def reference(self):
        return PercentileReference.from_lengths(list(range(1,11)))

    def test_custom_rules_are_used_and_snapshots_are_isolated(self):
        with temporary_directory() as directory:
            profile = self.profile()
            profile["congestion_threshold_mapping"] = dict(idle=.91, normal=.81, busy=.71, critical=.61)
            profile["pressure_rules"]["normal_concurrency_threshold"] = .2
            profile["pressure_rules"]["busy_concurrency_threshold"] = .4
            path = directory / "policy.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            policy = OutputPercentileClassifier(self.settings(), self.reference(), policy_path=path)
            _, snapshot = policy.classify_batch((req(0,1,8),), (EndpointView("a",1000,1000000,10,0,0,3),),0,0,0)
            self.assertEqual(snapshot["threshold"],.81)
            data = policy.policy.configuration
            data["congestion_threshold_mapping"]["normal"] = .5
            self.assertEqual(policy.policy.configuration["congestion_threshold_mapping"]["normal"], .81)
            artifacts = policy.artifacts()
            artifacts["trace"][0]["threshold"] = .1
            self.assertEqual(policy.trace[0]["threshold"], .81)

    def test_invalid_profiles_fail_before_processing(self):
        with temporary_directory() as directory:
            base = self.profile()
            bad_profiles = []
            profile = deepcopy(base)
            profile["congestion_threshold_mapping"]["idle"] = .5
            bad_profiles.append(profile)
            for rules in ({"unknown":1}, {"normal_concurrency_threshold":True},
                          {"normal_concurrency_threshold":.9, "busy_concurrency_threshold":.8},
                          {"critical_excess_ratio":0}, {"critical_excess_ratio":float("inf")}):
                profile = deepcopy(base)
                profile["pressure_rules"] = rules
                bad_profiles.append(profile)
            for i, profile in enumerate(bad_profiles):
                path = directory / f"bad_{i}.json"
                path.write_text(json.dumps(profile), encoding="utf-8")
                with self.subTest(i=i), self.assertRaises(ValueError):
                    OutputPercentileClassifier(self.settings(), self.reference(), policy_path=path)

    def test_execute_retains_dynamic_and_fixed_classification_without_export(self):
        with temporary_directory() as directory:
            reference = self.reference()
            artifact = directory / "reference.parquet"
            metadata = directory / "reference.json"
            reference.save(artifact)
            metadata.write_text(json.dumps({"percentile_definition": "empirical_cdf_right",
                "artifact_sha256": sha256_file(artifact), "reference_sample_count": reference.sample_count}), encoding="utf-8")
            settings = self.settings(output_reference_path=str(artifact),
                                     output_reference_metadata_path=str(metadata))
            source = directory / "requests.jsonl"
            source.write_text('{"request_id":"a","input_tokens":1,"output_tokens":8}\n'
                              '{"request_id":"b","input_tokens":1,"output_tokens":8}\n', encoding="utf-8")
            output = directory / "routes/dynamic.jsonl"
            result = execute(settings, source=source, source_format="lengths", output=output)
            self.assertEqual(result.summary["threshold_values_used"], [.6,.9])
            self.assertEqual([s["threshold"] for s in result.classification_artifacts["trace"]], [.9,.6])
            fixed = replace(settings, output_classification="percentile_fixed", output_percentile_threshold=.8)
            result = execute(fixed, source=source, source_format="lengths", output=output.parent / "fixed.jsonl")
            self.assertEqual(result.summary["threshold_values_used"], [.8])
            self.assertEqual(result.summary["threshold_update_count"], 0)
            self.assertEqual({p.name for p in output.parent.iterdir()}, {"dynamic.jsonl", "fixed.jsonl"})

    def test_resource_path_validation(self):
        for changes in ({"output_reference_path":"one.parquet"}, {"output_reference_metadata_path":"meta.json"},
                        {"output_policy_path":" "}, {"output_policy_path":True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.settings(**changes)
