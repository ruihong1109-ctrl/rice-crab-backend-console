# 稻蟹共生后端控制台

这是一套可以直接部署到 Render 的 FastAPI 后端示例，目标是给你的 cockpit 前端提供一套可联调、可交互、可实时推送的后端基础。

## 包含能力

- REST API
- WebSocket 实时推送
- 田块水质实时模拟
- 风险等级与建议动作
- 告警生成
- 指令下发与自动回执
- 设备影子状态
- 内置控制台页面
- Render 一键部署配置

## 目录结构

```text
rice_crab_backend_demo/
├── app/
│   └── main.py
├── static/
│   └── demo.html
├── requirements.txt
├── render.yaml
└── README.md
```

## 本地运行

```bash
cd rice_crab_backend_demo
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Windows PowerShell 可以用下面这组命令。

```powershell
cd rice_crab_backend_demo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## 本地访问地址

- Swagger 文档: `http://127.0.0.1:8000/docs`
- 控制台页面: `http://127.0.0.1:8000/console`
- 健康检查: `http://127.0.0.1:8000/api/v1/health`

## Render 部署方式

### 方式一：通过 `render.yaml`

1. 将整个项目推送到 GitHub。
2. 在 Render 中选择 **New +** → **Blueprint**。
3. 连接仓库后，Render 会自动识别 `render.yaml`。
4. 部署完成后，将 `ALLOWED_ORIGINS` 设置为你的 Netlify 域名，例如：

```text
https://your-site.netlify.app
```

如果本地也要联调，可以写成逗号分隔：

```text
http://localhost:5173,https://your-site.netlify.app
```

### 方式二：手动创建 Web Service

- Runtime: Python
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

环境变量建议配置：

- `ALLOWED_ORIGINS=https://your-site.netlify.app`

## 与 Netlify 前端联调建议

- HTTP API 建议通过 Netlify 将 `/api/*` 代理到 Render
- WebSocket 建议前端直接连接 Render 的 `wss://.../ws/cockpit`
- 若前端需要携带凭证，请确保 `ALLOWED_ORIGINS` 不要使用 `*`，而是明确写你的域名

## 主要接口

- `GET /api/v1/health`
- `GET /api/v1/cockpit/summary`
- `GET /api/v1/plots`
- `GET /api/v1/plots/{plot_id}`
- `GET /api/v1/plots/{plot_id}/water-quality/state`
- `GET /api/v1/devices`
- `GET /api/v1/alarms`
- `POST /api/v1/commands`
- `GET /api/v1/commands/{command_id}`
- `WS /ws/cockpit`

## 已内置的交互演示

控制台页面支持以下动作：

- 调整增氧等级
- 调整投喂指数
- 开关排灌泵
- 查看 WebSocket 实时消息
- 查看命令状态变化
- 查看实时水质波动与风险状态

## 下一步可继续扩展

- 拆出真实的设备适配层
- 把内存 Store 改成数据库存储
- 增加 MQTT Broker 接入
- 增加多田块与多基地
- 增加登录鉴权
- 增加更完整的水质规则引擎
