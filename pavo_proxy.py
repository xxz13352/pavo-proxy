# -*- coding: utf-8 -*-
"""Pavo Agnes 生图 / 生视频 · OpenAI 兼容反代（纯标准库）。

启动:
  python pavo_proxy.py --port 8787 --key sk-pavo

接口:
  GET  /healthz
  GET  /v1/models
  POST /v1/images/generations        同步
  POST /v1/videos/generations        默认异步（202 + id）；GET /v1/videos/<id> 轮询；
                                     "wait": true 时同步等待
"""
import argparse
import base64
import hmac
import json
import os
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pavo_client import (BASE, DEFAULT_IMAGE_MODEL, DEFAULT_VIDEO_MODEL,
                         generate_image, generate_video, get_token,
                         quality_to_res, size_to_ratio)

KEY = ""
TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_TTL = 3600          # 完成的视频任务保留 1 小时
MAX_VIDEO_TASKS = 4      # 同时排队/执行的视频任务上限
MODELS_TTL = 600
_MODELS = {"at": 0.0, "data": None}

FALLBACK_MODELS = [
    ("agnes-image", "Agnes Image 2.5 Flash", "image"),
    ("gpt-image-2", "全能图片 G2", "image"),
    ("gpt-image-2-5-flare", "全能图片 G2.5 Flare", "image"),
    ("gpt-image-2-5-sunburst", "全能图片 G2.5 Sunburst", "image"),
    ("mj-v8-2", "影像美学 V8.2", "image"),
    ("nano2", "NB 2", "image"),
    ("agnes-video-new-flash", "Agnes Video Flash", "video"),
    ("agnes-video-new", "Agnes Video", "video"),
]


def list_models():
    """带 10 分钟缓存；请求失败时返回内置列表。"""
    now = time.time()
    if _MODELS["data"] is not None and now - _MODELS["at"] < MODELS_TTL:
        return _MODELS["data"]
    try:
        request = urllib.request.Request(
            BASE + "/api/v1/pixa/models",
            headers={"x-platform": "1", "authorization": get_token()})
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())["data"]["models"]
        models = [(m["model_code"], m.get("model_alias") or m["model_code"], m.get("model_type"))
                  for m in data if m.get("model_type") in ("image", "video")]
        if models:
            _MODELS.update(at=now, data=models)
            return models
    except Exception:
        pass
    return FALLBACK_MODELS


def _gc_tasks():
    cutoff = time.time() - TASK_TTL
    with TASKS_LOCK:
        for task_id in [tid for tid, task in TASKS.items()
                        if task.get("created", 0) < cutoff]:
            TASKS.pop(task_id, None)


def _run_video(task_id, prompt, model, ratio, resolution, duration, sound):
    started = time.time()
    with TASKS_LOCK:
        if task_id not in TASKS:
            return
        TASKS[task_id]["status"] = "running"
    try:
        data = generate_video(prompt, duration=duration, ratio=ratio,
                              resolution=resolution, model=model, sound=sound)
        with TASKS_LOCK:
            TASKS[task_id].update(status="succeeded", data=data,
                                  finished=int(time.time()))
        print("[video %s] done in %.0fs" % (task_id[:8], time.time() - started), flush=True)
    except Exception as exc:
        with TASKS_LOCK:
            TASKS[task_id].update(status="failed", error=str(exc),
                                  finished=int(time.time()))
        print("[video %s] failed: %s" % (task_id[:8], exc), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err(self, code, message):
        self._json(code, {"error": {"message": message, "type": "pavo_error"}})

    def _authed(self):
        if not KEY:
            return True
        got = self.headers.get("Authorization", "").encode("utf-8")
        return hmac.compare_digest(got, ("Bearer " + KEY).encode("utf-8"))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2 * 1024 * 1024:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._json(200, {"ok": True, "image_model": DEFAULT_IMAGE_MODEL,
                                    "video_model": DEFAULT_VIDEO_MODEL})
        if path.startswith("/v1/videos/"):
            if not self._authed():
                return self._err(401, "bad proxy key")
            task_id = path[len("/v1/videos/"):]
            with TASKS_LOCK:
                task = dict(TASKS.get(task_id) or {})
            if not task:
                return self._err(404, "no such video task")
            return self._json(200, {
                "id": task_id,
                "object": "video.generation",
                "status": task.get("status"),
                "created": task.get("created"),
                "data": task.get("data"),
                "error": task.get("error"),
            })
        if path == "/v1/models":
            if not self._authed():
                return self._err(401, "bad proxy key")
            return self._json(200, {"object": "list", "data": [
                {"id": code, "object": "model", "owned_by": "pavo", "name": name, "type": kind}
                for code, name, kind in list_models()]})
        self._err(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/videos/generations":
            return self._video()
        if path == "/v1/images/generations":
            return self._image()
        self._err(404, "not found")

    def _image(self):
        if not self._authed():
            return self._err(401, "bad proxy key")
        try:
            request = self._read_json()
        except Exception:
            return self._err(400, "bad json")
        prompt = (request.get("prompt") or "").strip()
        if not prompt:
            return self._err(400, "prompt required")
        model = request.get("model") or DEFAULT_IMAGE_MODEL
        try:
            count = max(1, min(7, int(request.get("n") or 1)))
        except Exception:
            count = 1
        ratio = request.get("ratio") or size_to_ratio(request.get("size"))
        resolution = request.get("resolution") or quality_to_res(request.get("quality") or "standard")
        response_format = request.get("response_format") or "url"
        if response_format not in ("url", "b64_json"):
            return self._err(400, "response_format must be url or b64_json")

        started = time.time()
        try:
            results = []
            for _ in range(count):
                results.extend(generate_image(prompt, ratio=ratio, resolution=resolution,
                                              model=model, count=1))
            data = []
            for item in results[:count]:
                if response_format == "b64_json":
                    with urllib.request.urlopen(item["url"], timeout=120) as response:
                        raw = response.read()
                    data.append({"b64_json": base64.b64encode(raw).decode("ascii"),
                                 "revised_prompt": prompt})
                else:
                    data.append({"url": item["url"], "revised_prompt": prompt})
            print("[image] %s %s/%s -> %d img in %.0fs" % (
                model, ratio, resolution, len(data), time.time() - started), flush=True)
            self._json(200, {"created": int(time.time()), "data": data})
        except Exception as exc:
            traceback.print_exc()
            self._err(502, str(exc))

    def _video(self):
        if not self._authed():
            return self._err(401, "bad proxy key")
        try:
            request = self._read_json()
        except Exception:
            return self._err(400, "bad json")
        prompt = (request.get("prompt") or "").strip()
        if not prompt:
            return self._err(400, "prompt required")
        model = request.get("model") or DEFAULT_VIDEO_MODEL
        ratio = request.get("ratio") or (size_to_ratio(request.get("size"))
                                         if request.get("size") else "16:9")
        resolution = request.get("resolution") or "hd"
        try:
            duration = max(4, min(12, int(request.get("duration") or 4)))
        except Exception:
            duration = 4
        sound = bool(request.get("sound", True))

        _gc_tasks()
        if request.get("wait"):
            try:
                data = generate_video(prompt, duration=duration, ratio=ratio,
                                      resolution=resolution, model=model, sound=sound)
            except Exception as exc:
                return self._err(502, str(exc))
            return self._json(200, {"created": int(time.time()), "data": data})

        with TASKS_LOCK:
            active = sum(1 for task in TASKS.values()
                         if task.get("status") in ("queued", "running"))
        if active >= MAX_VIDEO_TASKS:
            return self._err(429, "too many video tasks, retry later")

        task_id = uuid.uuid4().hex
        with TASKS_LOCK:
            TASKS[task_id] = {"status": "queued", "created": int(time.time())}
        threading.Thread(target=_run_video,
                         args=(task_id, prompt, model, ratio, resolution, duration, sound),
                         daemon=True).start()
        print("[video %s] queued %s %s/%s %ds" % (
            task_id[:8], model, ratio, resolution, duration), flush=True)
        return self._json(202, {"id": task_id, "object": "video.generation",
                                "status": "queued", "created": int(time.time())})

    def log_message(self, fmt, *args):
        pass


def main():
    global KEY
    parser = argparse.ArgumentParser(description="Pavo Agnes OpenAI 兼容反代")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--key", default=os.environ.get("PAVO_PROXY_KEY", ""),
                        help="入站 Bearer key；不设则不校验")
    args = parser.parse_args()
    KEY = args.key

    try:
        token = get_token()
        print("Pavo token loaded (%s...)" % token[:12], flush=True)
    except Exception as exc:
        print("warning: %s" % exc, flush=True)
    if not KEY:
        print("warning: no --key set; any local process can use this proxy", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Pavo Agnes proxy -> http://%s:%d/v1/images/generations" % (args.host, args.port),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
