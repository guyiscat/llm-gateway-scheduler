"""Authenticated discovery, clock separation and quota-preserving maintenance."""
from contextlib import nullcontext, redirect_stdout
from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import deepcopy
from dataclasses import replace
from io import StringIO
import json
import os
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ..core import Scheduler
from ..core.config import SchedulerConfig
from ..core.request import ExecutionResult, SchedulerRequest
from ..endpoint_monitor import EndpointMonitor, filter_catalog, load_state_configs
from ..endpoint_monitor.service import response_usage
from ..integrations import litellm_gateway as gateway, replay_litellm as bridge
from ..simulation.config import load_config
from .support import config, temporary_directory
from .test_litellm_gateway import Response


def catalog():
    return {"data": [{"model_name": "deepseek-flash", "litellm_params": {
        "model": "openai/provider-" + name, "api_base": f"https://{name}.example/v1", "rpm": 60,
        "api_key": "provider-secret", "tags": [name], "input_cost_per_token": 2e-6,
        "output_cost_per_token": 8e-6, "unused": None}, "model_info": {"id": "deployment-" + name,
        "rpm": None, "tpm": None, "max_input_tokens": None}} for name in ("official", "relay")]}


class Session:
    def __init__(self):
        self.raw = catalog()
        self.gets, self.calls = 0, []

    def get_model_info(self):
        self.gets += 1
        return deepcopy(self.raw)

    def send(self, payload, **kwargs):
        self.calls.append(payload)
        groups = payload.get("metadata", {}).get("endpoint_group")
        matches = [row for row in self.raw["data"] if row["model_name"] == payload["model"] and
                   (groups is None or set(groups).intersection(row["litellm_params"].get("tags", [])))]
        if len(matches) != 1:
            raise AssertionError("Policy shell requires a public model and an unambiguous endpoint group")
        return {"http_status": 200, "response_format": "json", "x-litellm-model-id": matches[0]["model_info"]["id"],
                "response": {"choices": [{"message": {"content": "answer"}}],
                             "usage": {"prompt_tokens": 5, "completion_tokens": 3}}}


def request():
    return SchedulerRequest("request_000000", "deepseek-flash", 10, 20,
                            messages=({"role": "user", "content": "question"},))


def route():
    return {"request_id": "request_000000", "target_model": "deepseek-flash", "selected_endpoint_id": "deployment-official"}


def background(callback):
    result = Future()
    def run():
        try:
            result.set_result(callback())
        except BaseException as error:
            result.set_exception(error)
    Thread(target=run, daemon=True).start()
    return result


class EndpointMonitorTests(unittest.TestCase):
    def settings(self):
        return SchedulerConfig(busy_concurrency_reserve=0)

    def monitor(self, session, directory, **options):
        return EndpointMonitor(session, directory / "endpoints", self.settings(), periodic=False, **options)

    def test_filter_uses_deployment_ids_remote_rpm_and_explicit_unknown_limits(self):
        rows = filter_catalog(catalog())
        self.assertEqual([row["endpoint_id"] for row in rows], ["deployment-official", "deployment-relay"])
        self.assertEqual(rows[0]["rpm_limit"], 60)
        self.assertEqual(rows[0]["deployment_model"], "deepseek-flash")
        self.assertIsNone(rows[0]["tpm_limit"])
        self.assertIsNone(rows[0]["concurrency_limit"])
        self.assertEqual(rows[0]["input_price_per_million"], 2)
        self.assertEqual(rows[0]["output_price_per_million"], 8)
        self.assertNotIn("unused", rows[0])
        self.assertNotIn("api_key", rows[0])

    def test_invalid_catalog_and_limits_fail_without_inventing_endpoints(self):
        duplicate = catalog()
        duplicate["data"][1]["model_info"]["id"] = duplicate["data"][0]["model_info"]["id"]
        for raw in ({}, {"data": []}, duplicate):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                filter_catalog(raw)
        for overrides in ({"tpm_limit": 0}, {"concurrency_limit": True}, {"invalid": 3}):
            with self.assertRaises(ValueError):
                filter_catalog(catalog(), limit_overrides=overrides)
        rows = filter_catalog(catalog(), limit_overrides={"tpm_limit": 1000, "concurrency_limit": 10})
        self.assertEqual(rows[0]["tpm_limit"], 1000)
        self.assertEqual(rows[0]["limit_sources"]["concurrency_limit"], "local_override")

    def test_saved_raw_and_filtered_state_are_separate_and_credentials_redacted(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            raw = json.loads(monitor.raw_path.read_text(encoding="utf-8"))
            state = json.loads(monitor.state_path.read_text(encoding="utf-8"))
            self.assertIsNone(raw["data"][0]["litellm_params"]["unused"])
            self.assertEqual(raw["data"][0]["litellm_params"]["api_key"], "[REDACTED]")
            self.assertNotIn("provider-secret", monitor.state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(load_state_configs(monitor.state_path)), 2)
            self.assertEqual(state["clock"], "unix_milliseconds")

    def test_unknown_limits_work_with_scheduler_and_finite_limits_still_filter(self):
        configs = filter_catalog(catalog())
        from ..core.config import EndpointConfig
        endpoints = tuple(EndpointConfig(**{key: row[key] for key in EndpointConfig.__dataclass_fields__}) for row in configs)
        scheduler = Scheduler(endpoints, self.settings())
        scheduler.submit_request(request())
        self.assertEqual(scheduler.records[request().request_id].decision.selected_endpoint_id, "deployment-official")
        snapshot = scheduler.states.snapshots(0)[0]
        self.assertIsNone(snapshot.tpm_utilization)
        self.assertIsNone(snapshot.concurrency_utilization)
        limited = replace(endpoints[0], tpm_limit=1)
        limited_scheduler = Scheduler((limited,), self.settings())
        limited_scheduler.submit_request(request())
        self.assertEqual(limited_scheduler.records[request().request_id].status, "rejected")

    def test_actual_usage_and_rolling_window_survive_catalog_refresh(self):
        session, clock = Session(), [100.0]
        with temporary_directory() as directory, self.monitor(session, directory, clock=lambda: clock[0]) as monitor:
            monitor.capture_request(request())
            monitor.send(route(), {"model": "deepseek-flash", "messages": [{"role": "user", "content": "question"}]})
            session.raw["data"][0]["litellm_params"]["rpm"] = 50
            monitor.refresh()
            state = monitor.states.states["deployment-official"]
            self.assertEqual(state.config.rpm_limit, 50)
            self.assertEqual(state.total_requests, 1)
            self.assertEqual(state.tokens_in_window, 8)
            self.assertEqual(state.concurrency, 0)
            self.assertEqual(len(state.window), 1)
            clock[0] = 160.001
            monitor.publish()
            self.assertEqual(len(state.window), 0)
            self.assertEqual(state.tokens_in_window, 0)
            self.assertEqual(state.total_tokens, 8)

    def test_refresh_drains_all_requests_and_delays_new_sends_through_the_get(self):
        session, started, release, fetching, release_get = Session(), [Event(), Event()], [Event(), Event()], Event(), Event()
        original_send, original_get = session.send, session.get_model_info
        with temporary_directory() as directory, self.monitor(session, directory) as monitor:
            for index in range(3):
                monitor.capture_request(replace(request(), request_id=f"request_{index:06d}"))
            def send(payload, **kwargs):
                index = int(kwargs["request_id"].split("_")[-1])
                if index < 2:
                    started[index].set()
                    if not release[index].wait(2):
                        raise TimeoutError("test request was not released")
                return original_send(payload, **kwargs)
            def get():
                self.assertFalse(monitor.states.inflight)
                self.assertTrue(all(state.concurrency == 0 for state in monitor.states.states.values()))
                fetching.set()
                if not release_get.wait(2):
                    raise TimeoutError("test catalog was not released")
                return original_get()
            session.send = send
            session.get_model_info = get
            def dispatch(index):
                return monitor.send({**route(), "request_id": f"request_{index:06d}",
                    "selected_endpoint_id": "deployment-relay" if index == 1 else "deployment-official"},
                    {"model": "deepseek-flash"})
            first, second = background(lambda: dispatch(0)), background(lambda: dispatch(1))
            try:
                self.assertTrue(all(event.wait(2) for event in started))
                refresh = background(monitor.refresh)
                with monitor._condition:
                    self.assertTrue(monitor._condition.wait_for(lambda: monitor._refresh_phase == "waiting_for_idle", 2))
                snapshot = json.loads(monitor.state_path.read_text(encoding="utf-8"))
                self.assertTrue(snapshot["refresh_pending"])
                self.assertEqual([row["runtime"]["concurrency"] for row in snapshot["endpoints"]], [1, 1])
                third = background(lambda: dispatch(2))
                with self.assertRaises(FutureTimeout):
                    third.result(.05)
                self.assertEqual(session.gets, 1)
                release[0].set()
                first.result(2)
                self.assertFalse(fetching.wait(.05))  # Relay is still running.
                release[1].set()
                second.result(2)
                self.assertTrue(fetching.wait(2))
                with self.assertRaises(FutureTimeout):
                    third.result(.05)  # Still blocked while GET is running.
                release_get.set()
                refresh.result(2)
                third.result(2)
                self.assertEqual(len(session.calls), 3)
                self.assertEqual(session.gets, 2)
                snapshot = json.loads(monitor.state_path.read_text(encoding="utf-8"))
                self.assertFalse(snapshot["refresh_pending"])
                self.assertEqual(snapshot["refresh_phase"], "idle")
                self.assertEqual([row["runtime"]["requests_in_window"] for row in snapshot["endpoints"]], [2, 1])
                self.assertEqual([row["runtime"]["total_tokens"] for row in snapshot["endpoints"]], [16, 8])
            finally:
                for event in (*release, release_get):
                    event.set()

    def test_rate_limit_cooldown_and_uncertain_network_failure_are_retained(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            monitor.capture_request(request())
            monitor.session.send = lambda *a, **kw: {"http_status": 429, "response": {}, "response_format": "json"}
            monitor.send(route(), {"model": "deepseek-flash"})
            state = monitor.states.states["deployment-official"]
            self.assertGreater(state.cooldown_until_ms, monitor.states.now_ms)
            monitor.refresh()
            self.assertGreater(state.cooldown_until_ms, monitor.states.now_ms)
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            monitor.capture_request(request())
            with patch.object(monitor.session, "send", side_effect=TimeoutError("unknown")) as send:
                with self.assertRaises(TimeoutError):
                    monitor.send(route(), {"model": "deepseek-flash"})
                send.assert_called_once()
            state = json.loads(monitor.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["uncertain_requests"][0]["request_id"], request().request_id)
            self.assertEqual(state["endpoints"][0]["runtime"]["concurrency"], 0)

    def test_removed_endpoint_is_disabled_without_dropping_observed_usage(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            monitor.capture_request(request())
            monitor.send(route(), {"model": "deepseek-flash"})
            monitor.session.raw["data"] = monitor.session.raw["data"][1:]
            monitor.refresh()
            self.assertEqual(len(monitor.endpoint_configs()), 1)
            removed = monitor.states.states["deployment-official"]
            self.assertFalse(removed.healthy)
            self.assertEqual(removed.total_requests, 1)
            with self.assertRaises(ValueError):
                monitor.bind_payload(route(), {"model": "deepseek-flash"})

    def test_refresh_failure_keeps_last_valid_catalog(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            original = json.loads(monitor.state_path.read_text(encoding="utf-8"))
            original_raw = monitor.raw_path.read_bytes()
            monitor.session.raw = {"data": "invalid"}
            with self.assertRaises(ValueError):
                monitor.refresh()
            updated = json.loads(monitor.state_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["endpoints"], original["endpoints"])
            self.assertEqual(updated["catalog_fetched_at"], original["catalog_fetched_at"])
            self.assertEqual(monitor.raw_path.read_bytes(), original_raw)
            self.assertFalse(updated["refresh_pending"])
            self.assertEqual(updated["last_refresh_error"], "ValueError")

    def test_refresh_failure_releases_waiting_requests_without_retry(self):
        fetching, release = Event(), Event()
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            monitor.capture_request(request())
            def failed_get():
                fetching.set()
                if not release.wait(2):
                    raise TimeoutError("test catalog was not released")
                raise OSError("catalog offline")
            monitor.session.get_model_info = failed_get
            refresh = background(monitor.refresh)
            try:
                self.assertTrue(fetching.wait(2))
                waiting = background(lambda: monitor.send(route(), {"model": "deepseek-flash"}))
                with self.assertRaises(FutureTimeout):
                    waiting.result(.05)
                release.set()
                with self.assertRaises(OSError):
                    refresh.result(2)
                waiting.result(2)
                self.assertEqual(len(monitor.session.calls), 1)
                self.assertEqual(monitor.last_error, "OSError")
            finally:
                release.set()

    def test_online_feedback_can_drain_while_admission_waits_even_with_zero_rpm(self):
        clock = [100.0]
        with temporary_directory() as directory, self.monitor(Session(), directory, clock=lambda: clock[0]) as monitor:
            scheduler = Scheduler(monitor.endpoint_configs(), self.settings(), state_manager=monitor.states)
            with monitor.scheduler_guard():
                scheduler.submit_request(replace(request(), arrival_time=monitor.now_ms()))
            decision = scheduler.records[request().request_id].decision
            clock[0] = 161.0  # RPM is now zero, but the long request is still in flight.
            refresh = background(monitor.refresh)
            with monitor._condition:
                self.assertTrue(monitor._condition.wait_for(lambda: monitor._refresh_phase == "waiting_for_idle", 2))
            def admission():
                with monitor.scheduler_guard():
                    return True
            waiting = background(admission)
            try:
                self.assertTrue(all(not state.window for state in monitor.states.states.values()))
                self.assertEqual(monitor.session.gets, 1)
                with self.assertRaises(FutureTimeout):
                    waiting.result(.05)
            finally:
                with monitor.scheduler_guard(admission=False):
                    scheduler.accept_feedback(ExecutionResult(request().request_id, decision.selected_endpoint_id,
                        True, decision.dispatched_at_ms, monitor.now_ms()), drain=False)
            refresh.result(2)
            self.assertTrue(waiting.result(2))
            self.assertEqual(monitor.session.gets, 2)

    def test_periodic_refresh_wait_is_cancelled_on_exit_and_new_send_is_not_admitted(self):
        started, release = Event(), Event()
        with temporary_directory() as directory:
            monitor = self.monitor(Session(), directory, update_interval_seconds=.01)
            monitor.__enter__()
            closing = None
            try:
                monitor.capture_request(request())
                monitor.capture_request(replace(request(), request_id="next"))
                original = monitor.session.send
                def send(payload, **kwargs):
                    started.set()
                    if not release.wait(2):
                        raise TimeoutError("test request was not released")
                    return original(payload, **kwargs)
                monitor.session.send = send
                active = background(lambda: monitor.send(route(), {"model": "deepseek-flash"}))
                try:
                    self.assertTrue(started.wait(2))
                    monitor.thread = Thread(target=monitor._maintain, daemon=True)
                    monitor.thread.start()
                    with monitor._condition:
                        self.assertTrue(monitor._condition.wait_for(lambda: monitor._refresh_phase == "waiting_for_idle", 2))
                    waiting = background(lambda: monitor.send({**route(), "request_id": "next"}, {"model": "deepseek-flash"}))
                    with self.assertRaises(FutureTimeout):
                        waiting.result(.05)
                    closing = background(monitor.__exit__)
                    with self.assertRaisesRegex(RuntimeError, "closing"):
                        waiting.result(2)
                    monitor.thread.join(2)
                    self.assertFalse(monitor.thread.is_alive())
                    self.assertEqual(monitor.session.gets, 1)
                    with self.assertRaises(FutureTimeout):
                        closing.result(.05)  # Accepted request must finish first.
                finally:
                    release.set()
                    active.result(2)
            finally:
                release.set()
                if closing is not None:
                    closing.result(2)
                else:
                    monitor.__exit__()
            self.assertEqual(len(monitor.session.calls), 1)
            self.assertFalse(monitor._lock_path.exists())

    def test_malformed_response_releases_reservation_for_refresh_and_exit(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            monitor.capture_request(request())
            monitor.session.send = lambda *a, **kw: {"response": {}}
            with self.assertRaises(KeyError):
                monitor.send(route(), {"model": "deepseek-flash"})
            self.assertFalse(monitor.states.inflight)
            monitor.refresh()
            self.assertEqual(monitor.session.gets, 2)

    def test_state_write_failure_does_not_leave_admission_closed(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            with patch("workload_profiling.endpoint_monitor.service.write_json", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    monitor.refresh()
            with monitor.scheduler_guard():
                self.assertEqual(monitor._refresh_pending, 0)
            monitor.capture_request(request())
            monitor.send(route(), {"model": "deepseek-flash"})
            self.assertEqual(len(monitor.session.calls), 1)

    def test_empty_refresh_disables_all_previous_endpoints(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            monitor.session.raw = {"data": []}
            monitor.refresh()
            self.assertTrue(all(not state.healthy for state in monitor.states.states.values()))
            with self.assertRaises(ValueError):
                monitor.bind_payload(route(), {"model": "deepseek-flash"})

    def test_periodic_updates_continue_until_context_exit(self):
        session, refreshed = Session(), Event()
        original = session.get_model_info
        def get():
            result = original()
            if session.gets >= 2:
                refreshed.set()
            return result
        session.get_model_info = get
        with temporary_directory() as directory:
            with EndpointMonitor(session, directory / "endpoints", self.settings(), update_interval_seconds=.02) as monitor:
                self.assertTrue(refreshed.wait(2))
            self.assertFalse(monitor.thread.is_alive())
            self.assertFalse(monitor._lock_path.exists())

    def test_same_directory_cannot_have_two_writers(self):
        with temporary_directory() as directory, self.monitor(Session(), directory):
            with self.assertRaises(FileExistsError):
                with self.monitor(Session(), directory):
                    pass

    def test_monitor_cannot_refresh_outside_its_owned_lifecycle(self):
        with temporary_directory() as directory:
            monitor = self.monitor(Session(), directory)
            with self.assertRaises(RuntimeError):
                monitor.refresh()
            self.assertFalse(monitor.directory.exists())
            with monitor:
                pass
            with self.assertRaises(RuntimeError):
                monitor.publish()

    def test_removed_endpoint_reappearance_preserves_administrative_health(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            monitor.states.update_health("deployment-official", False, monitor.now_ms())
            monitor.session.raw["data"] = monitor.session.raw["data"][1:]
            monitor.refresh()
            monitor.session.raw = catalog()
            monitor.refresh()
            self.assertFalse(monitor.states.states["deployment-official"].healthy)

    def test_sse_usage_is_parsed_and_invalid_usage_remains_unknown(self):
        response = {"response_format": "sse", "response": 'data: {"usage":{"prompt_tokens":7,"completion_tokens":9}}\n\ndata: [DONE]\n\n'}
        self.assertEqual(response_usage(response), [7, 9])
        self.assertEqual(response_usage({"response": {"usage": {"prompt_tokens": True, "completion_tokens": -1}}}), [None, None])

    def test_explicit_groups_filter_the_discovered_pool_and_pin_the_deployment(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            configs = monitor.endpoint_configs(endpoint_groups=["official"])
            self.assertEqual([config.endpoint_id for config in configs], ["deployment-official"])
            payload = monitor.bind_payload(route(), {"model": "deepseek-flash", "metadata": {"endpoint_group": ["official"]}})
            self.assertEqual(payload["model"], "deepseek-flash")
            self.assertEqual(payload["metadata"]["endpoint_group"], ["official"])
            with self.assertRaises(ValueError):
                monitor.bind_payload(route(), {"model": "deepseek-flash", "metadata": {"endpoint_group": ["relay"]}})

    def test_standalone_gateway_uses_public_model_and_preserves_recorded_group(self):
        handoff = {**route(), "litellm_params": {"model": "deepseek-flash",
                    "messages": [{"role": "user", "content": "question"}], "stream": False,
                    "metadata": {"endpoint_group": ["official"]}}}
        payload = gateway.build_payload(handoff)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["metadata"]["endpoint_group"], ["official"])
        handoff["litellm_params"]["model"] = "deployment-official"
        del handoff["litellm_params"]["metadata"]
        with self.assertRaisesRegex(ValueError, "regenerate"):
            gateway.build_payload(handoff)

    def test_group_binding_rejects_ambiguous_tags_and_keeps_other_metadata(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            payload = monitor.bind_payload(route(), {"model": "deployment-official", "metadata": {"user_id": "u"}})
            self.assertEqual(payload, {"model": "deepseek-flash", "metadata": {"user_id": "u", "endpoint_group": ["official"]}})
            monitor.session.raw["data"][1]["litellm_params"]["tags"] = ["official"]
            monitor.refresh()
            with self.assertRaisesRegex(ValueError, "unique endpoint_group"):
                monitor.bind_payload(route(), {"model": "deepseek-flash"})
            monitor.session.raw["data"][0]["litellm_params"]["tags"].append("selected-only")
            monitor.refresh()
            self.assertEqual(monitor.bind_payload(route(), {})["metadata"]["endpoint_group"], ["selected-only"])

    def test_public_model_without_tags_only_binds_a_single_deployment(self):
        session = Session()
        for row in session.raw["data"]:
            row["litellm_params"]["tags"] = []
        with temporary_directory() as directory, self.monitor(session, directory) as monitor:
            with self.assertRaisesRegex(ValueError, "unique endpoint_group"):
                monitor.bind_payload(route(), {})
            session.raw["data"] = session.raw["data"][:1]
            monitor.refresh()
            self.assertEqual(monitor.bind_payload(route(), {}), {"model": "deepseek-flash"})

    def test_mismatched_reported_deployment_stops_without_false_usage_attribution(self):
        with temporary_directory() as directory, self.monitor(Session(), directory) as monitor:
            monitor.capture_request(request())
            with patch.object(monitor.session, "send", return_value={"http_status": 200,
                    "x-litellm-model-id": "deployment-relay", "response": {"usage": {"prompt_tokens": 1000}}}) as send:
                with self.assertRaisesRegex(ValueError, "different deployment"):
                    monitor.send(route(), {"model": "deepseek-flash"})
                send.assert_called_once()
            self.assertEqual(monitor.states.states["deployment-official"].total_tokens, 30)
            self.assertEqual(monitor.uncertain_requests[-1]["reported_endpoint_id"], "deployment-relay")

    def test_discovery_failure_prevents_replay_and_paid_calls(self):
        session = Session()
        session.raw = {"data": []}
        with temporary_directory() as directory, patch.object(bridge, "LiteLLMSession", return_value=nullcontext(session)), \
                patch.object(bridge, "load_scheduler_settings", return_value=self.settings()), patch.object(bridge, "execute") as execute, redirect_stdout(StringIO()):
            with self.assertRaises(ValueError):
                bridge.replay_and_send(ssh_target="ubuntu@example.com", log_dir=directory / "logs", endpoint_state_dir=directory / "endpoints")
            execute.assert_not_called()
            self.assertEqual(session.calls, [])

    def test_integrated_default_uses_real_catalog_instead_of_static_endpoint_file(self):
        session = Session()
        with temporary_directory() as directory:
            source, output = directory / "source.jsonl", directory / "routes.jsonl"
            source.write_text('\n'.join(json.dumps({"input_tokens": 1, "output_tokens": 2,
                "messages": [{"role": "user", "content": "question"}]}) for _ in range(2)), encoding="utf-8")
            scheduler_file, simulation_file = directory / "scheduler.json", directory / "simulation.json"
            scheduler_file.write_text(json.dumps(self.settings().to_dict()), encoding="utf-8")
            simulation_file.write_text(json.dumps({"endpoint_config": "does_not_exist.json", "scheduler_config": "scheduler.json",
                "arrival_mode": "fixed", "priority_assignment": "uniform"}), encoding="utf-8")
            with patch.object(bridge, "LiteLLMSession", return_value=nullcontext(session)), redirect_stdout(StringIO()):
                result = bridge.replay_and_send(ssh_target="ubuntu@example.com", config_path=simulation_file,
                    source=source, source_format="lengths", limit=2, output=output, log_dir=directory / "logs",
                    endpoint_state_dir=directory / "endpoints")
            routes = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["selected_endpoint_id"] for row in routes], ["deployment-official", "deployment-relay"])
            self.assertEqual([payload["model"] for payload in session.calls], ["deepseek-flash"] * 2)
            self.assertEqual([payload["metadata"]["endpoint_group"] for payload in session.calls], [["official"], ["relay"]])
            self.assertEqual([row["litellm_params"]["metadata"]["endpoint_group"] for row in routes], [["official"], ["relay"]])
            self.assertTrue(all(row["litellm_params"]["model"] == row["target_model"] for row in routes))
            self.assertEqual(result.sent_count, 2)
            state = json.loads((directory / "endpoints/endpoint_state.json").read_text(encoding="utf-8"))
            self.assertEqual([row["runtime"]["total_tokens"] for row in state["endpoints"]], [8, 8])

    def test_get_model_info_uses_one_authenticated_get_on_existing_session(self):
        calls = []
        class JsonResponse(Response):
            def read(self, size=-1):
                return super().read(size)
        def opened(req, **kw):
            calls.append(req)
            return JsonResponse(json.dumps(catalog()).encode())
        with patch.dict(os.environ, {"KEY": "test-key"}), \
                patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)), \
                patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=opened)):
            with gateway.LiteLLMSession("ubuntu@example.com") as session:
                self.assertEqual(session.get_model_info(), catalog())
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].get_method(), "GET")
            self.assertEqual(calls[0].full_url, "http://127.0.0.1:1234/model/info")
            self.assertEqual(calls[0].get_header("Authorization"), "Bearer test-key")
