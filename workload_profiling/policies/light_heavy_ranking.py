"""One policy owning pressure assessment, length classification and light-first order."""
from copy import deepcopy
from dataclasses import replace

from .ranking import RankingResult


class LightHeavyRankingPolicy:
    def __init__(self, config, *, classifier=None):
        self.config = config
        if classifier is not None and config.output_classification == "tokens":
            raise ValueError("Injected percentile classifier requires percentile output_classification")
        if classifier is None and config.output_classification != "tokens":
            from .classification import OutputPercentileClassifier
            classifier = OutputPercentileClassifier.load_default(config)
        self._classifier = classifier

    def classify_requests(self, requests, context):
        """Assess immediate requests without sorting or changing their scheduling path."""
        requests = tuple(requests)
        now_ms = context["now_ms"]
        if self._classifier is None:
            decisions = {}
            for request in requests:
                input_heavy = request.input_tokens >= self.config.input_threshold_tokens
                output_heavy = request.predicted_output_tokens >= self.config.output_threshold_tokens
                decisions[request.request_id] = {
                    "input_heavy": input_heavy, "output_heavy": output_heavy,
                    "heavy": input_heavy or output_heavy, "classified_at_ms": now_ms}
            snapshot = None
        else:
            decisions, snapshot = self._classifier.classify_batch(
                requests, context["endpoints"], context.get("ready_count", 0),
                now_ms, context.get("batch_id"))
            snapshot["classification_context"] = context.get("classification_context", "window")
        # Results belong to the caller; later policy calls cannot overwrite old batches.
        return RankingResult(requests, deepcopy(decisions), deepcopy(snapshot))

    def rank_requests(self, requests, context):
        result = self.classify_requests(requests, context)
        ordered = tuple(sorted(result.requests, key=lambda request:
            (-(request.priority or 0), result.classification[request.request_id]["heavy"])))
        # Scheduler windows contain low priority only; stable ties preserve arrival order.
        return replace(result, requests=ordered)

    def artifacts(self):
        return self._classifier.artifacts() if self._classifier is not None else None
