from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

import pandas as pd

from ..simulation import SimulationRunner, load_config
from ..simulation.classification import OutputPercentileClassifier
from ..simulation.models import EndpointView
from ..simulation.reporting import write_outputs
from ..simulation.cli import execute
from ..common.paths import PACKAGE
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

    def test_exported_resources_replay_without_original_policy(self):
        with temporary_directory() as directory:
            settings = self.settings(output_percentile_threshold=.75)
            path = directory / "original_policy.json"
            path.write_text(json.dumps(self.profile()), encoding="utf-8")
            policy = OutputPercentileClassifier(settings, self.reference(), policy_path=path)
            result = SimulationRunner(settings, classification_policy=policy).run([req(0,1,8),req(1,1,8)])
            output = directory / "exported"
            write_outputs(result, settings, output)
            path.unlink()
            replay_config = load_config(output / "replay_config.json")
            self.assertEqual(Path(replay_config.output_policy_path), output / "classification_policy.json")
            reloaded = SimulationRunner(replay_config).run([req(0,1,8),req(1,1,8)])
            self.assertEqual(result.requests, reloaded.requests)
            self.assertEqual(result.events, reloaded.events)
            self.assertEqual(result.batches, reloaded.batches)
            self.assertEqual(result.endpoints, reloaded.endpoints)
            self.assertEqual(result.summary, reloaded.summary)
            trace = pd.read_csv(output / "threshold_trace.csv")
            self.assertEqual(trace.threshold.tolist(), [.9,.6])
            self.assertEqual(trace.previous_threshold.iloc[0], .75)

    def test_standard_execute_exports_trace_and_fixed_mode(self):
        with temporary_directory() as directory:
            reference = self.reference()
            initial = self.settings()
            runner = SimulationRunner(initial, classification_policy=OutputPercentileClassifier(initial, reference))
            write_outputs(runner.run([]), initial, directory / "resources")
            settings = load_config(directory / "resources/replay_config.json")
            source = directory / "requests.jsonl"
            source.write_text(' {"request_id":"a","input_tokens":1,"output_tokens":8}\n'
                              '{"request_id":"b","input_tokens":1,"output_tokens":8}\n', encoding="utf-8")
            result, summary = execute(settings, source=source, source_format="lengths", output=directory / "dynamic")
            self.assertEqual(summary["threshold_values_used"], [.6,.9])
            self.assertTrue((directory / "dynamic/threshold_trace.csv").is_file())
            self.assertIn("classification", summary["provenance"])
            fixed = replace(settings, output_classification="percentile_fixed", output_percentile_threshold=.8)
            result, summary = execute(fixed, source=source, source_format="lengths", output=directory / "fixed")
            self.assertEqual(summary["threshold_values_used"], [.8])
            self.assertEqual(summary["threshold_update_count"], 0)
            empty_trace = pd.read_csv(directory / "resources/threshold_trace.csv")
            self.assertEqual(len(empty_trace), 0)
            self.assertIn("threshold", empty_trace.columns)

    def test_resource_path_validation(self):
        for changes in ({"output_reference_path":"one.parquet"}, {"output_reference_metadata_path":"meta.json"},
                        {"output_policy_path":" "}, {"output_policy_path":True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.settings(**changes)
