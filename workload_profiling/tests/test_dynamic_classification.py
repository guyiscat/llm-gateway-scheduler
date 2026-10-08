import unittest

from ..simulation import SimulationRunner
from ..simulation.classification import OutputPercentileClassifier
from ..simulation.models import EndpointView
from ..policies import PercentileReference
from .support import config, endpoint, req


class DynamicClassificationTests(unittest.TestCase):
    def reference(self):
        return PercentileReference.from_lengths(list(range(1,11)))

    def settings(self, mode="percentile_dynamic", **overrides):
        return config(**(dict(output_classification=mode, batch_order="priority_then_light") | overrides))

    def test_pressure_drives_monotone_threshold_levels(self):
        settings = self.settings()
        policy = OutputPercentileClassifier(settings,self.reference())
        observed = []
        for i,(occupied,ready) in enumerate(((0,0),(6,0),(9,0),(10,10))):
            view = EndpointView("a",1000,1000000,10,0,0,occupied)
            decisions,snapshot = policy.classify_batch((req(i,1,8),),(view,),ready,i,i)
            observed.append(snapshot["threshold"])
        self.assertEqual(observed,[.9,.8,.7,.6])
        self.assertEqual(snapshot["projected_excess_requests"],11)

    def test_fixed_threshold_and_reference_are_unchanged(self):
        settings = self.settings("percentile_fixed")
        reference = self.reference()
        policy = OutputPercentileClassifier(settings,reference)
        for occupied in (0,10):
            decisions,snapshot = policy.classify_batch((req(0,1,8),),
                (EndpointView("a",1000,1000000,10,0,0,occupied),),100,0,0)
            self.assertEqual(snapshot["threshold"],.8)
            self.assertTrue(decisions["0"]["output_heavy"])
            self.assertEqual(decisions["0"]["output_token_cutoff"],8)
        self.assertEqual(reference.sample_count,10)
        self.assertFalse(reference.values.flags.writeable)


    def test_classification_is_frozen_and_replay_repeats(self):
        settings = self.settings(batch_size=1,endpoints=(endpoint(concurrency=1),))
        requests = [req(0,1,8),req(1,1,8)]
        first = SimulationRunner(settings,classification_policy=OutputPercentileClassifier(settings,self.reference())).run(requests)
        second = SimulationRunner(settings,classification_policy=OutputPercentileClassifier(settings,self.reference())).run(requests)
        self.assertEqual(first,second)
        self.assertEqual([r["output_percentile_threshold"] for r in first.requests],[.9,.6])
        self.assertEqual([r["heavy"] for r in first.requests],[False,True])
        arrived = [e for e in first.events if e["event"] == "arrived"]
        self.assertTrue(all(e["heavy"] is None and e["classification_pending"] for e in arrived))
        self.assertEqual(first.summary["threshold_values_used"],[.6,.9])


    def test_invalid_modes_thresholds_and_scope(self):
        for changes in ({"output_classification":"bad"},{"output_percentile_threshold":0},
                        {"output_percentile_threshold":1},{"output_percentile_threshold":True}):
            with self.assertRaises(ValueError):
                config(**changes)
