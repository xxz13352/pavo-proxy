# -*- coding: utf-8 -*-
"""Pavo（app.pavo-ai.cn）Agnes 生图 / 生视频客户端（纯标准库）。

仅供 pavo_proxy.py 调用，不含命令行入口。
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

BASE = "https://api.pavo-ai.cn"
DEFAULT_IMAGE_MODEL = "agnes-image"
DEFAULT_VIDEO_MODEL = "agnes-video-new-flash"

RATIOS = [
    ("9:16", 9 / 16), ("2:3", 2 / 3), ("3:4", 3 / 4), ("1:1", 1.0),
    ("4:3", 4 / 3), ("3:2", 3 / 2), ("16:9", 16 / 9), ("21:9", 21 / 9),
]

_TOKEN = None


def _as_bearer(value):
    value = str(value).strip()
    return value if value.lower().startswith("bearer ") else "Bearer " + value


def _from_headers_file():
    """从 PAVO_HEADERS_FILE 或脚本同目录的 req_headers.json 读取 authorization。"""
    candidates = []
    env_path = os.environ.get("PAVO_HEADERS_FILE")
    if env_path:
        candidates.append(pathlib.Path(env_path))
    candidates.append(pathlib.Path(__file__).resolve().with_name("req_headers.json"))
    for path in candidates:
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(items, dict):
                items = [items]
            return items[0]["headers"]["authorization"]
        except Exception:
            continue
    return None


def _from_cdp():
    """从已登录的浏览器（CDP 9222）读取 localStorage.token；需要 playwright。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            for context in browser.contexts:
                for page in context.pages:
                    if "pavo" not in (page.url or ""):
                        continue
                    value = page.evaluate("() => localStorage.getItem('token')")
                    if value:
                        return value
    except Exception:
        pass
    return None


def get_token(force=False):
    """环境变量 PAVO_TOKEN → headers 文件 → 浏览器 CDP。返回带 Bearer 前缀的 token。"""
    global _TOKEN
    if _TOKEN and not force:
        return _TOKEN
    token = os.environ.get("PAVO_TOKEN") or _from_headers_file() or _from_cdp()
    if not token:
        raise RuntimeError(
            "找不到 Pavo token：请设置环境变量 PAVO_TOKEN，"
            "或提供 PAVO_HEADERS_FILE / 同目录 req_headers.json，"
            "或让浏览器登录 Pavo 并通过 CDP 9222 暴露 localStorage")
    _TOKEN = _as_bearer(token)
    return _TOKEN


def _headers(token, stream=False):
    headers = {
        "x-platform": "1",
        "authorization": token,
        "content-type": "application/json",
    }
    if stream:
        headers["accept"] = "text/event-stream"
    return headers


def _post_once(path, body, timeout):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(get_token()),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _post(path, body, timeout=30):
    """POST JSON；遇到 401 时强制重读 token 再试一次（JWT 过期场景）。"""
    try:
        return _post_once(path, body, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        exc.close()
        get_token(force=True)
        return _post_once(path, body, timeout)


def _open_stream(body, timeout):
    request = urllib.request.Request(
        BASE + "/api/v1/chat/stream",
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(get_token(), stream=True),
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _generate(mode, prompt, model, ratio, resolution, count=1, duration=None,
              sound=None, timeout=900, verbose=False):
    creative = json.dumps([{"type": "text", "content": prompt}], ensure_ascii=False)
    conversation = _post("/api/v1/chat/conversation",
                         {"title": creative, "folder_id": "", "kb_strict": False})
    body = {
        "conversation_id": conversation["data"]["conversation_id"],
        "prompt": prompt,
        "mode": mode,
        "model": model,
        "ratio": ratio,
        "resolution": resolution,
        "creative_prompt_json": creative,
    }
    if duration is not None:
        body["duration"] = duration
    if sound is not None:
        body["sound"] = bool(sound)
    if count and count > 1:
        body["count"] = count

    try:
        response = _open_stream(body, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        exc.close()
        get_token(force=True)
        response = _open_stream(body, timeout)

    buffer = b""
    event = None
    started = time.time()
    last_log = started
    try:
        while time.time() - started < timeout:
            chunk = response.read1(4096) if hasattr(response, "read1") else response.read(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if text.startswith("event:"):
                    event = text[6:].strip()
                    continue
                if not text.startswith("data:"):
                    continue
                try:
                    payload = json.loads(text[5:].strip())
                except Exception:
                    continue
                kind = payload.get("type") or event
                if verbose and time.time() - last_log > 10:
                    print("[pavo] %s %.0fs" % (kind, time.time() - started), flush=True)
                    last_log = time.time()
                if kind == "GenerationSuccess" or event == "GenerationSuccess":
                    return payload.get("data", {}).get("results", [])
                if kind in ("GenerationFailure", "GenerationFailed", "Error"):
                    raise RuntimeError("Pavo 生成失败: " +
                                       json.dumps(payload, ensure_ascii=False)[:300])
                if kind == "QuotaTryDeduct" and (payload.get("insufficient_hint") or payload.get("error")):
                    raise RuntimeError("Pavo 额度不足: " +
                                       str(payload.get("insufficient_hint") or payload.get("error")))
    finally:
        response.close()

    raise TimeoutError("Pavo 生成超时（%.0fs）" % timeout)


def generate_image(prompt, ratio="auto", resolution="sd", model=DEFAULT_IMAGE_MODEL,
                   count=1, timeout=300, verbose=False):
    """返回结果列表：[{url, width, height, mimetype, ...}]"""
    return _generate("generate_image", prompt, model, ratio, resolution,
                     count=count, timeout=timeout, verbose=verbose)


def generate_video(prompt, duration=4, ratio="16:9", resolution="hd",
                   model=DEFAULT_VIDEO_MODEL, sound=True, count=1,
                   timeout=900, verbose=False):
    """返回结果列表：[{url, first_frame_url, gif, webp_url, duration, ...}]"""
    return _generate("generate_video", prompt, model, ratio, resolution,
                     count=count, duration=duration, sound=sound,
                     timeout=timeout, verbose=verbose)


def size_to_ratio(size):
    """OpenAI 的 "1024x1792" → Pavo 的 "9:16"；无法解析时原样返回。"""
    if not size or size == "auto":
        return "auto"
    try:
        width, height = [int(part) for part in str(size).lower().split("x")]
    except Exception:
        return str(size)
    return min(RATIOS, key=lambda item: abs(item[1] - width / height))[0]


def quality_to_res(quality):
    return {
        "standard": "sd", "hd": "hd", "uhd": "uhd",
        "low": "sd", "high": "hd",
        "sd": "sd", "1k": "sd", "2k": "hd", "4k": "uhd",
    }.get(str(quality).lower(), "sd")
