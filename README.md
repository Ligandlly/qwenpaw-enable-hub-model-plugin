# qwenpaw-enable-hub-model-plugin

[![tests](https://github.com/Ligandlly/qwenpaw-enable-hub-model-plugin/actions/workflows/tests.yml/badge.svg)](https://github.com/Ligandlly/qwenpaw-enable-hub-model-plugin/actions/workflows/tests.yml)

给 **QwenPaw Hub 用 Docker 拉起的运行时**用的插件：装上之后，自动把 Hub 组织配置的
**默认模型**启用为这个运行时的个人可用模型，并把它设为运行时的**默认对话模型**。

```
Hub 模型目录 (GET /api/hub/model-runtime/catalog)
        │  default_model_id = "qwen3-235b"
        ▼
运行时插件（安装时 + 每次启动）
        ├─ hub-model-selection.json  ← 选中 / 标记已读 / 取消隐藏
        └─ active_model.json         ← provider_id=hub-managed, model=qwen3-235b
```

## 它做什么

1. 读 `QWENPAW_HUB_MODEL_URL` / `QWENPAW_HUB_MODEL_TOKEN` 判断当前运行时是否由 Hub
   托管（这两个变量由 Hub 的 Docker provisioner 注入）。不是 Hub 运行时 → 直接跳过，
   不改任何模型配置。
2. 拉取该运行时的模型目录，取 `default_model_id`；如果这个模型没有授权给当前用户
   （目录里没有它），退回目录里的第一个模型。
3. 把模型写进个人模型池（`hub-model-selection.json` 的 `selected`/`seen`，并清掉
   `hidden`），让它从"候选"变成"已启用"。
4. 把运行时默认 `chat` 槽指向 `hub-managed/<model>`。

**默认策略**：如果运行时已经选中了另一个**仍然可用**的 Hub 模型，保留用户的选择
（只保证它处于启用状态）。这样每次重启不会把用户在控制台里改过的选择重置。
需要"永远钉在 Hub 组织默认模型上"时，把 `force` 设为 `true`，见下文配置。

插件**不会**：禁用其它 provider、清空其它模型、触碰 `decision` 槽、改动某个 Agent
自己的模型覆盖（Agent 级配置优先于运行时默认模型）。

## 安装

插件以标准 ZIP 分发，安装一次即可；之后每次运行时启动都会重新应用。

```bash
make bundle     # -> dist/enable-hub-model-1.0.0.zip
```

### 方式一：qpm（推荐，批量）

在 Hub 宿主机上用 [qwenpaw-plugin-manager](https://github.com/Ligandlly/qwenpaw-plugin-manager)：

```bash
qpm install ./dist/enable-hub-model-1.0.0.zip --dry-run   # 先看计划
qpm install ./dist/enable-hub-model-1.0.0.zip --yes       # 装到所有运行时
```

### 方式二：直接调用运行时插件 API

```bash
CONTAINER=qwenpaw-runtime-xxxxxxxx
PORT=$(docker inspect -f '{{(index (index .NetworkSettings.Ports "8088/tcp") 0).HostPort}}' "$CONTAINER")
TOKEN=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
  | sed -n 's/^QWENPAW_RUNTIME_INTERNAL_TOKEN=//p')

curl -sS -X POST "http://127.0.0.1:$PORT/api/plugins/upload?force=true" \
  -H "X-QwenPaw-Runtime-Token: $TOKEN" \
  -F "file=@dist/enable-hub-model-1.0.0.zip"
```

### 方式三：控制台

运行时控制台的插件管理页上传 ZIP 即可。ZIP 里只有一个顶层目录
`enable-hub-model/`，其中包含 `plugin.json`。

## 配置

插件读 `config.json` 里 `plugins.enable-hub-model` 的配置项：

```json
{
  "plugins": {
    "enable-hub-model": {
      "enabled": true,
      "force": true
    }
  }
}
```

| 配置项 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `force` | bool | `false` | `true`：每次启动都强制切回 Hub 组织默认模型，即使当前已选中其它可用 Hub 模型；`false`：保留用户已选中的可用 Hub 模型 |

也可以走运行时接口改配置（改完需要重启运行时或重载插件才生效）：

```bash
curl -sS -X PUT "http://127.0.0.1:$PORT/api/plugins/enable-hub-model/config" \
  -H "X-QwenPaw-Runtime-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"config": {"force": true}}'
```

## 验证

```bash
# 运行时视角：默认模型是否变成 hub-managed/<model>
curl -sS -H "X-QwenPaw-Runtime-Token: $TOKEN" \
  "http://127.0.0.1:$PORT/api/providers/active" | python3 -m json.tool

# 插件状态（loaded/status 应为 loaded/enabled）
curl -sS -H "X-QwenPaw-Runtime-Token: $TOKEN" \
  "http://127.0.0.1:$PORT/api/plugins/enable-hub-model" | python3 -m json.tool
```

`GET /api/providers/active` 中应看到：

```json
{
  "active_llm": { "provider_id": "hub-managed", "model": "qwen3-235b" },
  "default_models": { "chat": { "provider_id": "hub-managed", "model": "qwen3-235b" } }
}
```

落盘位置（容器内 `$QWENPAW_SECRET_DIR/providers/`）：

- `hub-model-selection.json`：`{"selected": ["qwen3-235b"], "seen": [...], "hidden": []}`
- `active_model.json`：`{"provider_id": "hub-managed", "model": "qwen3-235b"}`

运行日志：

```bash
docker logs "$CONTAINER" 2>&1 | grep enable-hub-model
# [enable-hub-model] applied model qwen3-235b (Hub default qwen3-235b)
# [enable-hub-model] kept model qwen3-72b (Hub default qwen3-235b)
```

## 边界与容错

| 场景 | 行为 |
| --- | --- |
| 非 Hub 托管运行时 | `skipped: not-a-hub-managed-runtime`，不改配置 |
| Hub 没有给该用户任何模型 | `skipped: no-hub-models`，不改配置 |
| `default_model_id` 未授权给该用户 | 退回目录里第一个模型 |
| Hub 目录暂时不可达 | 最多 3 次尝试（0s / 1s / 3s，单次 10s 超时），失败只记日志，插件保持 loaded |
| 目录请求走代理 | 强制直连（忽略 `HTTP_PROXY`），Hub 监听在容器网段上 |
| 选中的模型被管理员下线 | 下次启动自动切回 Hub 默认模型 |
| 运行中 Hub 改了默认模型 | 下次启动生效（插件不在每轮对话里做网络请求） |

失败不抛给插件框架：安装时 Hub 短暂不可达不会导致安装回滚或插件进入 `failed`，
下一次运行时启动会重试。

## 兼容性（依赖的宿主契约）

| 依赖 | 出处 |
| --- | --- |
| `QWENPAW_HUB_MODEL_URL` / `QWENPAW_HUB_MODEL_TOKEN` | `src/qwenpaw/hub/docker_provisioner.py` `start()` |
| `GET {url}/api/hub/model-runtime/catalog` → `models[]`、`default_model_id` | `src/qwenpaw/hub/model_service/routes.py`、`catalog.member_catalog` |
| provider id `hub-managed` | `src/qwenpaw/providers/hub_managed.py` `PROVIDER_ID` |
| `hub-model-selection.json`（`selected` / `seen` / `hidden`） | `src/qwenpaw/providers/hub_model_selection.py`，位置 `ProviderManager.root_path = SECRET_DIR/providers` |
| 默认 chat 槽：`set_default_model("chat", slot)`（2.3）或 `activate_model()`（2.2） | `src/qwenpaw/providers/provider_manager.py` |
| 启动钩子：`PluginContext.on_activate`（SDK 1.0）或 `PluginApi.register_startup_hook`（2.2） | `src/qwenpaw/plugins/sdk.py`、`src/qwenpaw/plugins/api.py` |

声明的最低宿主版本是 **2.2.2**（`hub_model_selection` 从该版本才有）。插件同时兼容
旧的 `PluginApi`：2.2.x 上走 `register_startup_hook`，2.3+ 上走公共 SDK 的
`on_activate`。宿主升级到 2.3 之后，需要复查上面这几条契约。

## 开发

```bash
make test       # pytest
make compile    # byte-compile 检查
make bundle     # 产出可上传的 dist/enable-hub-model-<version>.zip
```

测试不安装 QwenPaw，也不联网：宿主模块被替换成 stub，HTTP 目录接口用本地
`http.server` 验证路径、Bearer 头与 JSON 解析。

## License

Apache-2.0
