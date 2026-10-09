"""Normalize recorded requests and estimates without touching endpoint state."""
from ..core.request import SchedulerRequest
from .execution import PreparedSimulationRequest
from .models import WorkloadRequest
from .priority import assign_priority


class RequestParameterGenerator:
    def __init__(self, config):
        self.config = config

    def prepare(self, observation, arrival_time):
        if not isinstance(observation, WorkloadRequest):
            raise TypeError("Simulation expects recorded WorkloadRequest observations")
        observation = assign_priority(observation, self.config)
        prediction = observation.predicted_output_tokens
        if prediction is None:
            prediction = observation.output_tokens if self.config.prediction_mode == "oracle" else self.config.predicted_output_tokens
        priority = observation.priority if observation.priority is not None else int(observation.priority_level == 4)
        request = SchedulerRequest(observation.request_id, observation.target_model or self.config.target_model,
            observation.input_tokens, prediction, priority=priority, arrival_time=arrival_time,
            messages=observation.messages, max_tokens=observation.max_tokens if observation.max_tokens is not None else self.config.max_tokens,
            stream=self.config.stream if observation.stream is None else observation.stream,
            api_type=observation.api_type, slo=observation.slo if observation.slo is not None else self.config.slo,
            metadata=observation.metadata)
        return PreparedSimulationRequest(request, observation)
