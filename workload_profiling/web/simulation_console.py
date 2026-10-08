"""Local simulation controls with background replay and progress polling."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
import webbrowser

from ..common.paths import RESULTS
from ..simulation.config import SimulationConfig, load_config, positive_integer
from ..simulation.ordering import BUILTIN_BATCH_ORDERS
from ..simulation.cli import execute

PAGE = (Path(__file__).parent / "templates/scheduler.html").read_text(encoding="utf-8")


class SimulationService:
    def __init__(self, *, tokenizer=None, tokenizer_metadata=None):
        self.tokenizer = tokenizer
        self.tokenizer_metadata = tokenizer_metadata
        self.lock = threading.Lock()
        self.state = {"status": "idle", "profiled": 0}
        self.result = None
        self.output = None

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state)

    def start(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("请求必须为 JSON 对象")
        config = SimulationConfig.from_dict(payload.get("config", load_config().to_dict()))
        # UI exposes the built-in simulation. Custom Python strategies use CLI/config.
        if config.strategy != "min_rpm":
            raise ValueError("网页控制台目前使用 min_rpm；自定义策略请使用命令行")
        if config.batch_order not in BUILTIN_BATCH_ORDERS:
            raise ValueError("网页只支持内置批内排序；自定义排序请使用命令行或 Python 接口")
        limit = payload.get("limit")
        if limit is not None:
            positive_integer(limit, "limit")
        with self.lock:
            if self.state["status"] == "running":
                raise ValueError("已有回放正在运行")
            self.result = None
            self.output = RESULTS / "simulation/web" / uuid4().hex
            self.state = {"status": "running", "profiled": 0}

        def progress(n):
            with self.lock:
                self.state["profiled"] = n

        def work():
            try:
                if self.tokenizer is None:
                    from ..common.tokenizer import load_tokenizer
                    self.tokenizer, self.tokenizer_metadata = load_tokenizer()
                result, summary = execute(config, limit=limit, output=self.output,
                                          tokenizer=self.tokenizer, tokenizer_metadata=self.tokenizer_metadata,
                                          progress=progress)
                with self.lock:
                    self.result = result
                    self.state = {"status": "completed", "profiled": summary["total_requests"],
                                  "summary": summary, "endpoints": result.endpoints,
                                  "output_directory": str(self.output)}
            except Exception as error:
                with self.lock:
                    self.state = {"status": "failed", "profiled": self.state["profiled"],
                                  "error": str(error) if isinstance(error, (ValueError, OSError)) else type(error).__name__}

        threading.Thread(target=work, daemon=True).start()
        return {"status": "running"}

    def request_page(self, offset, limit):
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("非法分页参数")
        with self.lock:
            if self.state["status"] != "completed" or self.result is None:
                raise ValueError("回放尚未完成")
            return {"count": len(self.result.requests), "requests": deepcopy(self.result.requests[offset:offset+limit])}

    def csv(self):
        with self.lock:
            if self.state["status"] != "completed":
                raise ValueError("回放尚未完成")
            return (self.output / "requests.csv").read_bytes()


def create_server(service, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, content_type="application/json; charset=utf-8", download=False):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8") if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="simulation_requests.csv"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif parsed.path == "/api/simulation/config":
                    self.send(200, load_config().to_dict())
                elif parsed.path == "/api/simulation/status":
                    self.send(200, service.snapshot())
                elif parsed.path == "/api/simulation/requests":
                    query = parse_qs(parsed.query)
                    self.send(200, service.request_page(int(query.get("offset", [0])[0]), int(query.get("limit", [100])[0])))
                elif parsed.path == "/simulation/requests.csv":
                    self.send(200, service.csv(), "text/csv; charset=utf-8", download=True)
                else:
                    self.send(404, {"error": "未找到页面"})
            except (ValueError, TypeError, OSError):
                self.send(400, {"error": "请求参数无效，或回放尚未完成"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/simulation/run":
                self.send(404, {"error": "未知接口"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 100000:
                    raise ValueError("请求大小应在 1 byte 到 100 KB 之间")
                payload = json.loads(self.rfile.read(length))
                self.send(202, service.start(payload))
            except (ValueError, TypeError, KeyError) as error:
                self.send(400, {"error": str(error)})

    return HTTPServer(("127.0.0.1", port), Handler)


def serve(port=8765, *, open_browser=False):
    service = SimulationService()
    server = create_server(service, port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Simulation console: {url}\nCtrl+C to stop", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
