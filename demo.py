"""Baseline 控制台：python demo.py --open；长度演示：--length-demo；CSV 导出：--export-only。

复用正式逐条接口；每次只计算一个当前请求。演示回复不调用真实模型。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import webbrowser
from uuid import uuid4

from workload_profiling.common.paths import ARTIFACTS, DATA, stage_results
from workload_profiling.common.tokenizer import load_tokenizer

CSV_PATH = DATA / "exports" / "request_workload_stage2_1.csv"
EXAMPLE_RESPONSE = "大语言模型通过大量文本学习语言规律，并根据输入上下文生成回答。当前这段文本仅用于演示长度计算，没有调用真实模型。"


def export_csv(frame, path=CSV_PATH):
    """仅显式导出可读副本，不改变正式 Parquet。空值保持为空，兼容 Excel UTF-8。"""
    # pandas is only needed by the historical Parquet/CSV mode.
    import pandas as pd

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        frame.to_csv(temporary, index=False, encoding="utf-8-sig", na_rep="")
        # CSV 丢失 dtype；回读恢复 nullable boolean 等类型后检查全部值和 null。
        restored = pd.read_csv(temporary, encoding="utf-8-sig", float_precision="round_trip")
        pd.testing.assert_frame_equal(restored.astype(frame.dtypes.to_dict()), frame)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class DemoService:
    def __init__(self, tokenizer, reference, frame, *, config_path=None):
        self.tokenizer, self.reference, self.frame = tokenizer, reference, frame
        self.config_path = config_path or stage_results("stage2_1") / "runtime_policy_config.json"

    def profile(self, payload):
        from workload_profiling.runtime import OutputHeavyPolicy, RequestProcessor
        from workload_profiling.runtime.output_heavy_policy import validate_threshold

        if not isinstance(payload, dict):
            raise ValueError("请输入一个 JSON 对象")
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and not prompt.strip():
            raise ValueError("Prompt 不能为空")
        # 每次单独实例化 policy，页面阈值不会覆盖磁盘配置或其它请求的阈值。
        policy = OutputHeavyPolicy(self.reference, config_path=self.config_path)
        threshold = payload.get("threshold")
        if threshold is not None:
            policy.set_threshold(validate_threshold(threshold))
        mode = payload.get("response_mode", "input_only")
        if mode not in {"input_only", "typed", "example"}:
            raise ValueError("未知回复模式")
        record = {"prompt": {"messages": [{"role": "user", "content": prompt}]}} if isinstance(prompt, str) else {"prompt": prompt}
        if mode == "typed":
            response = payload.get("response")
            if not isinstance(response, str):
                raise ValueError("请输入回复文本")
            record["response"] = response
        elif mode == "example":
            record["response"] = EXAMPLE_RESPONSE
        result = RequestProcessor(self.tokenizer, output_policy=policy).process(record)
        return {"result": result, "active_threshold": policy.get_threshold(),
                "active_threshold_source": policy.threshold_source, "response_mode": mode,
                "example_response": EXAMPLE_RESPONSE if mode == "example" else None,
                "model_called": False}

    def preview(self, page=0, size=30, heavy_only=False):
        if page < 0 or not 1 <= size <= 100:
            raise ValueError("非法分页参数")
        frame = self.frame[self.frame.output_heavy] if heavy_only else self.frame
        records = json.loads(frame.iloc[page * size:(page + 1) * size].to_json(orient="records", double_precision=15))
        return {"columns": list(frame.columns), "records": records, "count": len(frame),
                "page": page, "size": size, "heavy_count": int(self.frame.output_heavy.sum()),
                "reference_count": self.reference.sample_count}


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workload · 单条请求演示</title>
<style>
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#172c40;background:#f3f6fa;font-size:14px}*{box-sizing:border-box}body{margin:0}main{max-width:1260px;margin:auto;padding:32px 24px}h1{font-size:29px;margin:8px 0}h2{font-size:18px;margin:0 0 18px}.eyebrow{letter-spacing:2px;color:#4c6b84;font-size:11px;font-weight:700}.intro{color:#5b7183;line-height:1.7}.layout{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:24px}.card{background:white;border:1px solid #dce4ec;border-radius:15px;padding:24px}label{display:block;font-weight:600;margin:18px 0 8px}textarea,input,select{width:100%;font:inherit;border:1px solid #bac9d7;border-radius:8px;padding:11px;color:#172c40;background:white}textarea{resize:vertical;line-height:1.7}input[type=checkbox]{width:auto;margin-right:7px}.row{display:flex;gap:12px;align-items:center}.row>div{flex:1}.hint{font-size:12px;color:#687e90;line-height:1.7;margin-top:7px}.button,button{border:0;border-radius:8px;padding:11px 18px;background:#174b75;color:white;cursor:pointer;font:inherit;text-decoration:none;display:inline-block}button:disabled{opacity:.5;cursor:wait}.secondary{background:#eaf0f6;color:#264d6d}.actions{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}.status{padding:12px 14px;border-radius:8px;background:#edf4fa;color:#315773;margin:14px 0;line-height:1.6}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{background:#f5f8fb;border-radius:10px;padding:15px}.metric small{display:block;color:#5f7486;margin-bottom:8px}.metric strong{font-size:25px}.barline{display:grid;grid-template-columns:60px 1fr 65px;gap:10px;align-items:center;margin:14px 0}.track{height:11px;border-radius:6px;background:#e7edf3;overflow:hidden}.fill{height:100%;background:#3075ac;border-radius:6px;min-width:0}.fill.output{background:#2d9b8d}.gauge{height:14px;background:#e7edf3;border-radius:7px;position:relative;margin:14px 0}.gauge .fill{background:#2d9b8d}.marker{position:absolute;top:-5px;height:24px;width:2px;background:#a6532b}.badge{display:inline-block;padding:6px 10px;border-radius:6px;background:#e6f3eb;color:#216640}.badge.heavy{background:#fff0df;color:#975314}pre{white-space:pre-wrap;word-break:break-word;background:#f5f8fb;padding:12px;border-radius:8px;font-size:12px;line-height:1.65;max-height:270px;overflow:auto}.wide{margin-top:20px}.tablebox{overflow:auto;margin-top:14px;border:1px solid #e0e7ee;border-radius:8px;max-height:420px}table{border-collapse:collapse;font-size:12px;width:100%}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e8edf2;white-space:nowrap}th{background:#edf3f8;position:sticky;top:0;color:#36556f}td.null{color:#8c9ba7}tbody tr:nth-child(even){background:#fafcfe}.error{color:#a92b2b;margin:12px 0}.footer{color:#6d8192;font-size:12px;margin:20px 0}.hidden{display:none}@media(max-width:800px){main{padding:20px 14px}.layout{grid-template-columns:1fr}.card{padding:18px}h1{font-size:24px}.metrics{gap:6px}.metric{padding:12px}.metric strong{font-size:21px}.row{flex-wrap:wrap}}
</style></head><body><main>
<div class="eyebrow">WORKLOAD PROFILING / LOCAL DEMO</div><h1>一条请求，从输入到长度判断</h1>
<p class="intro">使用固定 Qwen tokenizer 计算长度，按历史输出分布判断 Output-Heavy。每次处理一条，不调用真实模型。</p>
<div class="layout"><section class="card"><h2>输入与阈值</h2>
<label for="prompt">Prompt</label><textarea id="prompt" rows="5">请解释一下大语言模型如何生成回答。</textarea>
<label style="font-weight:400"><input id="jsonPrompt" type="checkbox">使用完整 prompt JSON（含 messages / tools）</label>
<div class="row"><div><label for="threshold">手动阈值 θ</label><input id="threshold" type="number" value="0.95" min="0.001" max="0.999" step="0.01"></div>
<div><label for="mode">回复来源</label><select id="mode"><option value="input_only">仅计算输入</option><option value="typed">我输入一段回复</option><option value="example">使用演示回复</option></select></div></div>
<label style="font-weight:400"><input id="manualOverride" type="checkbox" checked>启用人工覆盖</label>
<div class="hint">0 &lt; θ &lt; 1；标签规则为输出百分位 ≥ θ。手动设置仅对本次判断生效。</div>
<div id="responseArea" class="hidden"><label for="response">回复文本</label><textarea id="response" rows="5"></textarea><div class="actions"><button id="shortExample" class="secondary" type="button">短回复示例</button><button id="longExample" class="secondary" type="button">长回复示例</button></div></div>
<div class="actions"><button id="calculate" type="button">计算当前请求</button><button id="defaultThreshold" class="secondary" type="button">恢复默认阈值</button></div>
<div id="error" class="error" role="alert"></div></section>
<section class="card"><h2>计算结果</h2><div id="status" class="status" aria-live="polite">输入 prompt 后点击计算。尚未提供回复时，输出相关字段保持未知。</div>
<div class="metrics"><div class="metric"><small>Input tokens</small><strong id="input">—</strong></div><div class="metric"><small>Output tokens</small><strong id="output">—</strong></div><div class="metric"><small>Total tokens</small><strong id="total">—</strong></div></div>
<div class="barline"><span>Input</span><div class="track"><div id="inputBar" class="fill"></div></div><span id="inputLabel">—</span></div>
<div class="barline"><span>Output</span><div class="track"><div id="outputBar" class="fill output"></div></div><span id="outputLabel">未知</span></div>
<label>历史输出百分位 <span id="percentile">—</span></label><div class="gauge"><div id="percentileBar" class="fill"></div><div id="thresholdMarker" class="marker" style="left:95%"></div></div>
<div class="row"><span id="heavyBadge" class="badge">等待回复</span><span id="thresholdLabel" class="hint">θ = 0.95</span></div>
<p class="hint">百分位是固定历史长度排名，不是 EVT 概率。橙线表示阈值；没有回复时不产生 Output-Heavy 标签。</p>
<details><summary>查看完整结果 JSON</summary><pre id="resultJson">{}</pre></details></section></div>
<section class="card wide"><div class="row"><div><h2 style="margin-bottom:8px">历史数据预览</h2><div class="hint" id="datasetSummary">正在读取完整数据视图…</div></div><a href="/export.csv" class="button">下载完整 CSV</a></div>
<div class="row" style="margin-top:12px"><label style="font-weight:400;margin:0"><input id="heavyOnly" type="checkbox">仅显示默认 Output-Heavy</label><span class="hint">历史标签是离线 DEFAULT 快照，页面阈值不会改写它们。</span></div>
<div class="tablebox"><table><thead id="thead"></thead><tbody id="tbody"></tbody></table></div>
<div class="actions"><button id="previous" class="secondary">上一页</button><button id="next" class="secondary">下一页</button><span id="pageLabel" class="hint"></span></div>
<p class="hint">null 表示缺失，不是 0 或 false。output_tail_evt 缺失表示 Stage 2 没有稳定的 Output EVT 阈值；congestion_state 缺失表示未提供外部拥塞状态。CSV 中这些值保留为空单元格。</p></section>
<div class="footer">输入内容只在本地计算，不写入历史数据集。关闭运行 demo 的终端进程即可停止服务。</div>
</main><script>
const $=id=>document.getElementById(id);let page=0,totalRows=0;const size=30;
const shortText='大语言模型根据上下文逐个生成 token。';
const paragraph='大语言模型先把输入文本转换为 token，再利用上下文计算下一个 token 的分布，并逐个生成回答。输入长度包含完整上下文和模板开销，输出长度只计算当前回答文本。历史百分位用于比较输出长度，阈值是可以人工调整的操作参数，不代表自然长尾起点。这段内容只是演示文本，没有调用真实模型。';
function modeChanged(){ $('responseArea').classList.toggle('hidden',$('mode').value!=='typed'); }
$('mode').addEventListener('change',modeChanged);
$('shortExample').onclick=()=>{$('response').value=shortText;};
$('longExample').onclick=()=>{$('response').value=Array(12).fill(paragraph).join('\n\n');};
$('manualOverride').onchange=()=>{$('threshold').disabled=!$('manualOverride').checked;};
$('defaultThreshold').onclick=()=>{$('threshold').value=$('threshold').dataset.default||'0.95';$('manualOverride').checked=false;$('threshold').disabled=true;};
$('calculate').onclick=async()=>{
 $('error').textContent='';$('calculate').disabled=true;
 try{let prompt=$('prompt').value;if($('jsonPrompt').checked)prompt=JSON.parse(prompt);
 const threshold=$('manualOverride').checked?Number($('threshold').value):null;if(threshold!==null&&(!Number.isFinite(threshold)||threshold<=0||threshold>=1))throw Error('阈值必须满足 0 < θ < 1');
 const response=await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt,threshold,response_mode:$('mode').value,response:$('response').value})});
 const data=await response.json();if(!response.ok)throw Error(data.error||'计算失败');const r=data.result;
 for(const [id,key]of [['input','input_tokens'],['output','output_tokens'],['total','total_tokens']])$(id).textContent=r[key]===null?'未知':r[key].toLocaleString();
 const max=Math.max(r.input_tokens,r.output_tokens||0,1);$('inputBar').style.width=(100*r.input_tokens/max)+'%';$('outputBar').style.width=(100*(r.output_tokens||0)/max)+'%';$('inputLabel').textContent=r.input_tokens;$('outputLabel').textContent=r.output_tokens===null?'未知':r.output_tokens;
 $('percentile').textContent=r.output_percentile===null?'未知':(100*r.output_percentile).toFixed(3)+'%';$('percentileBar').style.width=(100*(r.output_percentile||0))+'%';$('thresholdMarker').style.left=(data.active_threshold*100)+'%';$('thresholdLabel').textContent='θ = '+data.active_threshold+' · '+data.active_threshold_source;
 $('heavyBadge').textContent=r.output_heavy===null?'等待回复':r.output_heavy?'Output-Heavy':'非 Output-Heavy';$('heavyBadge').classList.toggle('heavy',r.output_heavy===true);
 $('status').textContent=data.response_mode==='input_only'?'已计算输入；尚无回复，输出长度与标签未知。':data.response_mode==='example'?'演示回复已完成长度计算，没有调用真实模型。':'按你提供的回复计算长度，没有调用真实模型。';
 $('resultJson').textContent=JSON.stringify(data,null,2);
 }catch(error){$('error').textContent=error.message;}finally{$('calculate').disabled=false;}
};
async function preview(){try{const response=await fetch('/api/dataset?page='+page+'&size='+size+'&heavy='+($('heavyOnly').checked?'1':'0'));const data=await response.json();if(!response.ok)throw Error(data.error);totalRows=data.count;
 $('datasetSummary').textContent='固定 reference：'+data.reference_count.toLocaleString()+' 条 · '+data.columns.length+' 个字段 · 默认 Heavy：'+data.heavy_count.toLocaleString()+' 条';
 $('thead').replaceChildren();const header=document.createElement('tr');for(const key of data.columns){const th=document.createElement('th');th.textContent=key;header.append(th);}$('thead').append(header);
 $('tbody').replaceChildren();for(const row of data.records){const tr=document.createElement('tr');for(const key of data.columns){const td=document.createElement('td');const value=row[key];td.textContent=value===null?'null':typeof value==='number'&&key==='output_percentile'?value.toFixed(6):String(value);if(value===null)td.className='null';tr.append(td);}$('tbody').append(tr);}
 $('pageLabel').textContent='第 '+(page+1)+' / '+Math.max(1,Math.ceil(totalRows/size))+' 页 · '+totalRows.toLocaleString()+' 条';$('previous').disabled=page===0;$('next').disabled=(page+1)*size>=totalRows;
 }catch(error){$('datasetSummary').textContent=error.message;}}
$('previous').onclick=()=>{if(page>0){page--;preview();}};$('next').onclick=()=>{if((page+1)*size<totalRows){page++;preview();}};$('heavyOnly').onchange=()=>{page=0;preview();};
fetch('/api/config').then(r=>r.json()).then(c=>{$('threshold').value=c.default_threshold;$('threshold').dataset.default=c.default_threshold;$('thresholdLabel').textContent='θ = '+c.default_threshold;$('thresholdMarker').style.left=(c.default_threshold*100)+'%';});preview();
</script></body></html>'''


def create_server(service, *, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 不在日志中记录输入文本。

        def send_body(self, status, body, content_type, *, download=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, value):
            self.send_body(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self.send_body(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif parsed.path == "/api/config":
                    policy = OutputHeavyPolicy(service.reference, config_path=service.config_path)
                    self.send_json(200, {"default_threshold": policy.get_threshold()})
                elif parsed.path == "/api/dataset":
                    query = parse_qs(parsed.query)
                    self.send_json(200, service.preview(int(query.get("page", [0])[0]), int(query.get("size", [30])[0]), query.get("heavy", ["0"])[0] == "1"))
                elif parsed.path == "/export.csv":
                    # 下载直接序列化当前视图，已在 Excel 打开的本地 CSV 不影响页面下载。
                    body = service.frame.to_csv(index=False, na_rep="").encode("utf-8-sig")
                    self.send_body(200, body, "text/csv; charset=utf-8", download=CSV_PATH.name)
                else:
                    self.send_json(404, {"error": "未找到该页面"})
            except (ValueError, TypeError, OSError) as error:
                self.send_json(400, {"error": "数据读取或导出失败，请检查正式数据及文件是否被占用。"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/profile":
                self.send_json(404, {"error": "未知接口"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    raise ValueError("请求大小须在 1 byte 到 2 MB 之间")
                payload = json.loads(self.rfile.read(length))
                self.send_json(200, service.profile(payload))
            except Exception as error:
                # 错误仅描述输入/处理类别，不反射原始 prompt 内容。
                from workload_profiling.runtime import RequestProcessingError
                message = str(error) if isinstance(error, (ValueError, RequestProcessingError)) else "本地计算失败，请检查输入格式"
                self.send_json(400, {"error": message})

    # 单线程 server 与逐条接口一致，不并发发送/计算请求；仅绑定本机。
    return HTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后打开默认浏览器")
    parser.add_argument("--export-only", action="store_true", help="只导出完整 CSV，不启动网页")
    parser.add_argument("--length-demo", action="store_true", help="打开原有单条长度演示")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if not args.export_only and not args.length_demo:
        from workload_profiling.baseline.dashboard import serve
        serve(args.port, open_browser=args.open)
        return
    # Keep historical analysis dependencies out of the default baseline path.
    from workload_profiling.common.datasets import load_dataset
    from workload_profiling.runtime import PercentileReference

    frame = load_dataset("stage2_1")
    try:
        path = export_csv(frame)
    except OSError:
        if args.export_only:
            parser.error("CSV 无法写入，请关闭占用该文件的程序后重试")
        print("CSV 文件无法写入；仍可在页面下载当前完整数据。", flush=True)
    else:
        print(f"CSV: {path} ({len(frame):,} 行，{len(frame.columns)} 列)", flush=True)
    if args.export_only:
        return
    reference = PercentileReference.load(ARTIFACTS / "output_percentile_reference.parquet",
                                         stage_results("stage2_1") / "percentile_reference_metadata.json")
    tokenizer, _ = load_tokenizer()
    service = DemoService(tokenizer, reference, frame)
    try:
        server = create_server(service, port=args.port)
    except OSError:
        parser.error(f"端口 {args.port} 不可用，请通过 --port 指定其它端口")
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Demo: {url}\n按 Ctrl+C 停止。", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
