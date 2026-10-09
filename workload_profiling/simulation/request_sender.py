"""Fixed, random and burst arrivals; the scheduler never generates traffic."""
import random

from .arrivals import burst_arrival_times


class SimulatedRequestSender:
    def __init__(self, config):
        self.config = config

    def schedule(self, observations):
        c = self.config
        if c.arrival_mode == "burst":
            observations = tuple(observations)
            yield from zip(burst_arrival_times(len(observations), c), observations)
            return
        random_source = random.Random(c.arrival_seed)
        time_ms = 0
        for index, observation in enumerate(observations):
            if c.arrival_mode == "fixed":
                time_ms = index * c.arrival_interval_ms
            elif index:
                time_ms += random_source.randint(c.random_min_interval_ms, c.random_max_interval_ms)
            yield time_ms, observation

    def submit(self, request, scheduler):
        return scheduler.submit_request(request)
