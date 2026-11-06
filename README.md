# pavo-proxy

把 Pavo（app.pavo-ai.cn）的 **Agnes 生图 / 生视频** 包成本地 OpenAI 兼容 API。纯标准库，零第三方依赖。

> 本项目仅供学习与个人自用，与 Pavo 官方无关。请遵守 Pavo 的服务条款，不要公开部署或批量滥用。仓库不包含任何账号 Token。

## 启动

```bash
python pavo_proxy.py --port 8787 --key sk-pavo
```

或双击 `启动反代.bat`（默认端口 8787、key `sk-pavo`）。

OpenAI 客户端配置：

- Base URL：`http://127.0.0.1:8787/v1`
- API Key：`sk-pavo`（与启动时的 `--key` 一致；不传 `--key` 则不校验）

## 接口

### `GET /healthz`

存活检查，不需要鉴权。

### `GET /v1/models`

列出图片和视频模型，结果缓存 10 分钟。

### `POST /v1/images/generations`（同步）

```json
{
  "model": "agnes-image",
  "prompt": "窗台上睡觉的橘猫，午后阳光，电影感",
  "size": "1024x1024",
  "n": 1,
  "quality": "hd",
  "response_format": "url"
}
```

- `size` 会换算成最接近的 `ratio`；也可以直接传 `"ratio": "16:9"`
- `quality`：`standard` / `hd` / `uhd`；也可以传 `"resolution": "sd|hd|uhd"`
- `n`：1–7
- `response_format`：`url`（默认）或 `b64_json`

### `POST /v1/videos/generations`（默认异步）

```json
{
  "prompt": "清晨的森林，薄雾，阳光穿过树叶",
  "model": "agnes-video-new-flash",
  "duration": 4,
  "ratio": "16:9",
  "resolution": "hd",
  "sound": true
}
```

返回 `202 {"id": "...", "status": "queued"}`，然后：

```bash
curl http://127.0.0.1:8787/v1/videos/<id> -H "Authorization: Bearer sk-pavo"
```

状态为 `succeeded` 时 `data[]` 里带 `url` / `first_frame_url` / `gif` / `webp_url`。
请求体加 `"wait": true` 则同步等结果。

- `duration`：4–12 秒
- `agnes-video-new-flash`：免费（720p）
- `agnes-video-new`：8 积分 / 4s（1080p / 2K）
- 同时排队/执行的视频任务最多 4 个；完成的任务保留 1 小时

## Token

按顺序查找：

1. 环境变量 `PAVO_TOKEN`（值形如 `Bearer <token>`，不带前缀也行）
2. 环境变量 `PAVO_HEADERS_FILE` 指向的 JSON，或脚本同目录的 `req_headers.json`
   （格式：`[{"headers": {"authorization": "Bearer <token>"}}]`）
3. 已登录 Pavo 的浏览器 + CDP 9222（可选，需要 `pip install playwright`）

JWT 过期时，请求遇到 401 会自动强制重读一次 token；仍然失败就检查上面的来源。

## 注意

- 出图约 20–80 秒，出视频约 3–4 分钟；同步调用要给客户端留足超时，视频建议用默认异步模式。
- 默认只监听 `127.0.0.1`。不要在不设 `--key` 的情况下用 `--host 0.0.0.0` 暴露到局域网/公网，否则任何人都会消耗你账号的额度。
- 中文提示词经 Git Bash 的 `curl -d` 可能乱码，建议用 JSON 文件：`curl --data-binary @body.json`。
- 本项目按网页端接口逆向实现，仅供个人使用，请自行注意 Pavo 的服务条款与额度消耗。

## 文件

```
pavo-proxy/
├─ pavo_proxy.py    # HTTP 反代（OpenAI 兼容接口）
├─ pavo_client.py   # Pavo 接口客户端（token / 生图 / 生视频 / SSE）
├─ README.md
├─ LICENSE
└─ 启动反代.bat
```

## 免责声明

本项目按网页端接口逆向实现，仅供学习与个人自用，与 Pavo 官方无关。使用本项目的全部风险由使用者自行承担；请遵守 Pavo 的服务条款，不要用于商业用途、批量滥用或任何违法场景。

## License

MIT

