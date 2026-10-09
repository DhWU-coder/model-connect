# Model Connect

通过真实文本生成请求检查 API 的模型连通性，提供中文网页和 CLI。先取得候选模型列表，再向每个模型发送「只回复hi」；收到有效文本才计为成功。

支持 OpenAI、Anthropic、Google Gemini 三种接口格式。API 地址均由用户填写，不预填官方地址；官方服务、中转站和本地服务按其提供的格式选择。用 OpenAI 格式提供 Claude 的服务应选择「OpenAI」。

## 安装

需要 Python 3.11 或更新版本。

如果已安装 uv，可以直接安装为终端命令，无需每次激活环境：

```bash
uv tool install --editable .
model-connect start
```

也可以使用项目虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Windows 激活命令为 `.venv\Scripts\activate`。安装后可在激活环境的任意目录使用 `model-connect`，也可直接执行 `.venv/bin/model-connect`。

## 启动与停止

```bash
model-connect start                 # 后台启动
model-connect restart               # 停止后后台启动，沿用原地址和端口
model-connect run                   # 前台运行，显示日志
model-connect stop                  # 停止前台或后台的受管理服务
model-connect end                   # 与 stop 相同
model-connect status                # 查看地址、PID 和日志位置
model-connect logs --lines 50        # 查看后台日志
model-connect start --port 9000      # 指定监听端口
model-connect start --host 127.0.0.1 # 仅允许本机访问
```

默认监听 **0.0.0.0:8886**，本机访问 **http://127.0.0.1:8886**，同一局域网的设备访问 **http://服务器的局域网IP:8886**，不会自动打开浏览器。前台运行时也可按 `Ctrl+C` 停止。重复 `start` 会显示已有服务地址。监听端口被其他程序占用时会明确报错，不停止其他程序。

旧版本已运行的服务仍沿用原监听地址；使用 `model-connect restart --host 0.0.0.0` 切换为局域网访问。

使用虚拟环境安装后，也可在项目目录直接执行 `.venv/bin/model-connect start`。

页面顶部可切换「跟随系统 / 浅色 / 深色」，默认跟随系统主题。选择会自动记住，刷新后继续沿用；跟随系统模式也会响应系统主题的实时变化。

## 检测步骤

1. 选择 provider，填写 API 根地址和 API Key。
2. 点击「获取模型列表」；不支持列表接口时可直接手动添加名称，或导入 TXT / JSON。
3. 筛选并勾选模型，调整提示词、并发数、超时和输出 Token 上限。
4. 点击「开始检测」，查看实时进度、回复、耗时、HTTP 状态和失败原因。
5. 导出 JSON 或 CSV。服务保留最近 20 个任务；重启后历史清空，请及时导出。

点击「调用成功」或「调用失败」统计区域，可在弹窗中查看对应模型、调用协议、回复或错误，检测过程中列表会实时更新。双协议检测按调用记录分别展示，同时显示去重后的模型数量。

检测结果区域支持独立的模型名称搜索，可选择包含、开头、结尾、精确匹配或通配符，输入多行规则，并选择区分大小写；名称搜索可与状态筛选叠加，检测更新时保留条件。搜索仅影响结果表格，统计、模型列表弹窗以及 JSON / CSV 导出仍包含完整任务。

API 根地址没有路径时自动补 `/v1`，Google 补 `/v1beta`；已有路径原样保留。例如网关使用 `https://example.com/proxy/v1` 时直接填写完整根地址。高级设置可指定相对于根地址的列表路径、调用路径和 JSON 请求头。Google 自定义调用路径可写 `models/{model}:generateContent`。

附加请求头示例：

```json
{"anthropic-workspace-id": "wrkspc_你的工作区"}
```

## 接口格式与模型列表

| 接口格式 | API 根地址 | 获取候选模型 | 检测协议 |
| --- | --- | --- | --- |
| OpenAI | 用户填写 | `GET models` | 默认 Chat Completions，也可选 Responses 或分别检测两者 |
| Anthropic | 用户填写 | `GET models`，自动 after_id 分页 | `POST messages` |
| Google Gemini | 用户填写 | `GET models`，自动 pageToken 分页 | `POST models/{model}:generateContent` |

Google 使用 Gemini Developer API 的 API Key，不包含 Vertex AI 的 IAM 认证。Anthropic 默认带 `x-api-key` 和 `anthropic-version: 2023-06-01`；需要 Bearer 时可在附加请求头配置 `Authorization`。本地无需鉴权的兼容服务可留空 API Key。

模型列表是候选目录，不能证明当前密钥能实际调用。列表获取失败不会用固定模型名称冒充查询结果。Google 模型能力元数据和明确的专用模型名称会用于提示「文本检测不适用」；可手动勾选「允许检测标记为不适用的模型」进行验证。

## 名称筛选

支持包含、开头、结尾、精确匹配和通配符，默认不区分大小写。多行规则按「或」组合。

| 匹配方式 | 示例 | 含义 |
| --- | --- | --- |
| 包含 | `flash` | 名称中包含 flash |
| 开头 | `gemini` | 名称以 gemini 开头 |
| 结尾 | `preview` | 名称以 preview 结尾 |
| 通配符 | `*_flash_*` | 任意位置包含 `_flash_` |
| 通配符 | `gemini*flash*` | 以 gemini 开头，后面含 flash |
| 通配符 | `model-?` | 问号匹配一个字符 |

`**_flash_**` 与 `*_flash_*` 等价。改变筛选规则后自动选择匹配且适用的模型，也可以逐个调整。单次最多 1000 个模型，最多 20 并发。

## 如何理解结果

- 调用成功：HTTP 请求成功，响应符合所选协议，包含非空的用户可见文本。
- 调用失败：分别记录超时、网络、鉴权、权限、额度、限流、模型或接口不存在、参数不支持、拒绝、空文本、格式异常等原因。
- 不适用：嵌入、语音、图像或实时专用模型不能通过普通文本生成请求完整验证。
- 已取消：未完成的检测被用户取消。

选择两种 OpenAI 协议时，每个模型生成两条结果。返回的模型名称也会记录，但无法仅凭 API 响应证明中转站使用了哪个底层模型。成功只表示本次文本请求可用，不代表其他能力、所有地区或持续稳定性。

默认输出上限 64 Token、并发 5、超时 30 秒、不额外重试。不强加 temperature 等模型相关参数。部分思考模型会消耗输出预算却没有可见回复，遇到空文本时可以提高上限。兼容接口明确拒绝 `max_tokens` 时会额外尝试一次 `max_completion_tokens`，请求次数会显示。额外重试仅针对网络、超时、速率限制和 5xx。真实调用与重试可能产生 provider 费用。

## 本地数据与访问

接口格式、URL 和 API Key 保存在当前标签页的 sessionStorage 中，刷新网页后自动恢复，Key 仍以密码形式显示；不做长期保存，普通新标签页不会读取另一标签页的连接信息。切换格式保留 URL 和 Key，手动清空输入会覆盖会话中的旧值。高级连接设置在格式切换或刷新后清空，localStorage 仅保存主题偏好。旧版本的 OpenAI 兼容入口会话和请求标识仍可使用，统一归为 OpenAI 格式。

密钥不写入服务配置、日志或导出。运行任务只在服务内存中持有凭据；历史结果也会对凭据回显脱敏。检测任务 ID 同样在标签页会话中保存，用于刷新后恢复结果。浏览器禁止存储时当前页仍可输入和检测，但无法在刷新后恢复。CSV 对可能被当作公式的单元格转义。

状态文件和日志默认写入：

- macOS：`~/Library/Application Support/model-connect/`
- Linux：`~/.local/state/model-connect/`
- Windows：`%LOCALAPPDATA%\model-connect\`

可通过 `MODEL_CONNECT_STATE_DIR` 指定独立目录；每个目录管理一个服务实例。默认允许局域网访问，并拒绝来自其他网页的跨站请求。使用 `--host 127.0.0.1` 可限制为本机访问，本工具没有账号体系。

## 开发与验证

```bash
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
```

测试使用本地模拟响应验证三家协议、分页、真实调用请求体、筛选、错误分类、取消、导出、密钥脱敏和进程生命周期，不需要真实 API Key，也不会调用付费 provider。

需要手动验证网页时，可运行 `python scripts/mock_provider.py`，并在 UI 填写 `http://127.0.0.1:9876/v1`（Google 使用 `/v1beta`）。模拟服务提供成功、权限不足、限流、空文本、慢响应和嵌入模型样例。

设计与实施记录见 [设计文档](docs/设计文档.md) 和 [实施方案](docs/实施方案.md)。

官方接口依据：[OpenAI 模型列表](https://developers.openai.com/api/reference/resources/models/methods/list)、[Anthropic API](https://platform.claude.com/docs/en/api/overview)、[Google 模型列表](https://ai.google.dev/api/models)、[Google 内容生成](https://ai.google.dev/api/generate-content)。
