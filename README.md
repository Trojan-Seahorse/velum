# Velum · LLM 隐私防火墙

**透明代理层，在消息到达 LLM 之前自动检测并脱敏个人身份信息（PII），响应返回后还原。**

```
请求：用户 → 客户端 → [Velum] → LLM API
         脱敏引擎拦截 PII，替换为匿名标识符

响应：用户 ← 客户端 ← [Velum] ← LLM API
         匿名标识符还原为原始信息
```

## 为什么需要

LLM API 是黑盒——你的对话内容会被服务商记录、存储、用于模型训练。直接发送含真实姓名、电话、身份证号、地址等信息的消息，等同于把这些数据交给第三方。

Velum 在你的消息离开内网之前拦截 PII，替换为不可逆的匿名标识符（如 `P-00128`、`L-23017`），LLM 永远看不到原始数据。响应返回时自动还原，用户无感知。

## 核心特性

| 特性 | 说明 |
|------|------|
| **透明代理** | OpenAI 兼容 API 端点，客户端只需改 URL |
| **多模式切换** | 消息内 `!pii` 前缀即可切换脱敏策略，无需修改配置 |
| **分类型标识** | 每种实体类型使用不同前缀（P-人名 O-组织 L-地点 T-电话...），LLM 可区分实体类型 |
| **SSE 流式还原** | DeepSeek reasoning_content 也完整还原 |
| **复合地名增强** | 内建 430+ 经济功能区地名（园区/新区/经开区等），弥补 HanLP NER 盲区 |
| **Fail-open** | 脱敏引擎异常时直通原文，不阻断服务 |
| **低依赖** | 仅依赖 argus-redact + FastAPI，单容器运行，内存 < 500MB |

## 环境要求

| 条件 | 说明 |
|------|------|
| Docker | 24+，含 docker compose |
| LLM API | 任意 OpenAI 兼容 API（DMXAPI、OpenAI 等） |
| 内存 | ≥ 1GB（HanLP 模型 ~400MB，Docker 构建时自动下载） |
| 网络 | 容器需能访问上游 LLM API 地址 |

> Python 3.12 仅开发/测试时需要，部署完全用 Docker，不需要在宿主机装 Python。

## 部署

以下两种部署方式选其一。**推荐方式二**（Docker Compose），与 Hermes 网关配合后可通过微信、Web 等 IM 客户端使用。

---

### 方式一：Docker 单容器

适合已有其他客户端（如 CherryStudio 直接连接）的场景。

**第 1 步：克隆仓库**

```bash
git clone https://github.com/Trojan-Seahorse/velum.git
cd velum
```

仓库目录里有这些文件：`Dockerfile`（镜像构建）、`main.py`（代理逻辑）、`requirements.txt`（Python 依赖）、`location_names.txt`（复合地名列表）。

**第 2 步：构建镜像**

```bash
docker build -t velum .
```

构建过程中会自动下载 HanLP 中文 NER 模型（约 400MB）。这一步可能需要几分钟，主要取决于网络速度。

**第 3 步：启动容器**

```bash
docker run -d \
  -p 8000:8000 \
  -e UPSTREAM_URL=https://your-llm-api.com/v1 \
  -e ARGUS_REDACT_PSEUDONYM_SALT=$(openssl rand -hex 16) \
  --name velum \
  velum
```

参数说明：

| 参数 | 含义 |
|------|------|
| `-d` | 后台运行 |
| `-p 8000:8000` | 将容器的 8000 端口映射到宿主机（前面的端口可以改，如 `-p 17829:8000`） |
| `-e UPSTREAM_URL=...` | **必填**，上游 LLM API 地址。任何 OpenAI 兼容 API 均可 |
| `-e ARGUS_REDACT_PSEUDONYM_SALT=...` | **必填**，化名模式的盐值（决定"张伟"每次被换成哪个假名），用随机字符串即可 |
| `--name velum` | 容器名称，方便后续 `docker logs velum` 等操作 |

> 如果你的上游 API 是 HTTP 而非 HTTPS，用 `http://` 即可。Velum 透传所有请求头（含 Authorization），不存储 API Key。

**第 4 步：验证**

```bash
curl http://localhost:8000/health
```

返回 `{"status":"ok","upstream":"https://...","pii":"ok"}` 说明部署成功。如果 `pii` 字段不是 `ok`，见[故障排查](#故障排查)。

---

### 方式二：Docker Compose（配合 Hermes 网关）

适合通过微信等 IM 客户端使用 LLM 的场景。Hermes 是一个多平台 Agent 网关，负责接收微信/Telegram 等渠道的消息，转发给 Velum。

完整的 4 服务架构：`Velum` → `Hermes` → `Dashboard` + `WebUI`。

**第 1 步：准备目录结构**

```bash
mkdir -p ~/hermes_agent
cd ~/hermes_agent

# 克隆 Velum 到子目录
git clone https://github.com/Trojan-Seahorse/velum.git
```

此时目录结构：

```
~/hermes_agent/
└── velum/           # 刚克隆的 Velum 仓库
    ├── Dockerfile
    ├── main.py
    ├── requirements.txt
    └── location_names.txt
```

**第 2 步：创建 `docker-compose.yml`**

在 `~/hermes_agent/` 下创建 `docker-compose.yml`：

```yaml
services:
  velum:
    build: ./velum
    container_name: velum
    ports:
      - "127.0.0.1:17829:8000"
    volumes:
      - ./velum/location_names.txt:/app/location_names.txt:ro
    environment:
      - PYTHONUNBUFFERED=1
      - UPSTREAM_URL=https://www.dmxapi.cn/v1
      - PII_ENABLED=true
      - ARGUS_REDACT_PSEUDONYM_SALT=your-random-salt-here
    restart: unless-stopped
    mem_limit: 1g
    networks:
      - hermes-net

  hermes:
    image: nousresearch/hermes-agent:latest
    container_name: hermes
    command: ["gateway", "run"]
    volumes:
      - ./hermes:/opt/data
    environment:
      - API_SERVER_ENABLED=true
      - API_SERVER_HOST=0.0.0.0
      - API_SERVER_KEY=your-api-key
    restart: unless-stopped
    mem_limit: 512m
    ports:
      - "17834:8642"
    networks:
      - hermes-net
    depends_on:
      - velum

  dashboard:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-dashboard
    command: ["dashboard", "--host", "0.0.0.0", "--no-open", "--insecure"]
    volumes:
      - ./hermes:/opt/data
    ports:
      - "17832:9119"
    restart: unless-stopped
    mem_limit: 256m
    networks:
      - hermes-net
    depends_on:
      - hermes

  webui:
    image: ghcr.io/nesquena/hermes-webui:latest
    container_name: hermes-webui
    volumes:
      - ./hermes:/home/hermeswebui/.hermes
      - ./workspace:/workspace
    ports:
      - "17833:8787"
    restart: unless-stopped
    mem_limit: 256m
    networks:
      - hermes-net
    depends_on:
      - hermes

networks:
  hermes-net:
    driver: bridge
```

需要修改的地方：

| 配置项 | 位置 | 说明 |
|--------|------|------|
| `UPSTREAM_URL` | velum 的 environment | 改成你实际使用的 LLM API 地址 |
| `ARGUS_REDACT_PSEUDONYM_SALT` | velum 的 environment | 改成随机字符串 |
| `API_SERVER_KEY` | hermes 的 environment | 自定义一个 API 密钥 |
| 端口映射 | 各服务 ports | 如宿主机端口冲突，改冒号前面的数字 |

**第 3 步：配置 Hermes 连接 Velum**

Hermes 的 LLM 后端地址不是通过环境变量配置的（这是常见的坑），而是写在两个配置文件中。

先让 Docker 创建 Hermes 数据目录（容器首次启动会自动生成），然后编辑配置：

```bash
# 先启动一次让 Hermes 生成初始配置
docker compose up -d hermes

# 编辑 Hermes 配置文件
```

创建 `./hermes/config.yaml`，内容如下（已存在则修改 `base_url` 行）：

```yaml
base_url: http://velum:8000/v1
model: deepseek-chat
```

创建 `./hermes/auth.json`，内容如下（已存在则修改 `base_url` 行）：

```json
{
  "base_url": "http://velum:8000/v1",
  "api_key": "你的上游 LLM API Key"
}
```

> **关键点**：`base_url` 中的 `velum` 是 Docker 服务名，不是 localhost。同一个 `hermes-net` 网络内的容器通过服务名互相访问。

**第 4 步：启动全部服务**

```bash
cd ~/hermes_agent
docker compose up -d
```

首次启动时 `docker compose build` 会自动构建 Velum 镜像（含 HanLP 模型下载），大约 2-5 分钟。

**第 5 步：验证**

```bash
# Velum 健康检查
curl http://localhost:17829/health
# → {"status":"ok","upstream":"https://www.dmxapi.cn/v1","pii":"ok"}

# 查看 Velum 日志
docker logs -f velum

# 查看 Hermes 日志
docker logs -f hermes
```

**第 6 步：配置 Hermes 接入渠道**

Hermes Dashboard 地址：`http://your-nas-ip:17832`。在 Dashboard 中添加微信、Telegram 等渠道。具体步骤参考 [Hermes 官方文档](https://github.com/NousResearch/hermes-agent)。

---

### 客户端配置

部署完成后，客户端指向 Velum 地址，API Key 用上游 LLM 的 Key（Velum 只做透传，不存储）：

| 客户端 | 配置方式 |
|--------|---------|
| CherryStudio | 设置 → 模型服务 → 添加提供商 → API 地址: `http://your-host:17829/v1` |
| Hermes | `config.yaml`: `base_url: http://velum:8000/v1`（上面已配置） |
| OpenAI SDK | `OpenAI(base_url="http://your-host:8000/v1", api_key="...")` |
| 微信 | 通过 Hermes WeChat adapter（在 Dashboard 中扫码登录） |

## 日常使用

对话开头加 `!pii` 前缀即可**临时**切换脱敏模式。下一条消息不加前缀则自动恢复默认策略。全角 `！` 和半角 `!` 均支持。

| 命令 | 效果 |
|------|------|
| `!pii` | 查看当前防火墙状态和策略配置 |
| `!pii debug <文本>` | 分析指定文本的 PII 检测结果，不调用 LLM |
| `!pii 伪名` / `!pii pseudonym` | 人名替换为逼真假名（其他类型照常脱敏） |
| `!pii org,loc` | 部分放行：保留组织和地名不脱敏 |

### 示例

```
用户: !pii 伪名 帮我查一下张伟的通讯录信息
      → 本条消息以化名模式发送，LLM 看到的是假名
      → LLM 回复后自动还原真实姓名

用户: 再帮我查一下李娜的   ← 不加前缀，自动恢复默认模式
      → 正常脱敏，人名变为 P-NNNNN 标识符
```

```
用户: !pii debug 李明在成都天府新区管委会工作，电话13900001111

原文: 李明在成都天府新区管委会工作，电话13900001111
脱敏: P-47141在P-72185管委会工作，电话T-39281
实体数: 3

检测到的实体:
  [1] person  李明 → P-47141
  [2] person  成都天府新区 → P-72185
  [3] phone   13900001111 → T-39281

用户: 帮我总结一下   ← 下一条消息正常对话
```

> **注意**：Hermes 在网关层拦截所有 `/` 开头的命令。Velum 使用 `!pii` 前缀不受影响。不要使用 `/pii`。

## 脱敏策略

| 实体类型 | 策略 | 示例 |
|---------|------|------|
| 人名 (person) | remove | 李明 → P-00128 |
| 组织 (organization) | remove | 阿里巴巴 → O-09502 |
| 学校 (school) | remove | 浙江大学 → S-14439 |
| 地点 (location) | remove | 北京市 → L-23017 |
| 电话 (phone) | remove | 13900001111 → T-39281 |
| 邮箱 (email) | remove | a@b.com → E-55612 |
| 身份证 (id_number) | remove | 110101... → I-78403 |
| 地址 (address) | remove | 天府大道200号 → A-66194 |
| 银行卡 (bank_card) | mask | 622202... → ****0123 |
| 自称 (self_reference) | keep | 不处理 |
| 日期 (date) | remove | 2024-03-15 → D-33501 |

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|:----:|--------|------|
| `UPSTREAM_URL` | ✅ | — | 上游 LLM API 地址，如 `https://www.dmxapi.cn/v1` |
| `ARGUS_REDACT_PSEUDONYM_SALT` | ✅ | — | 化名模式的盐值，决定假名生成。用随机字符串，不同实例用不同值 |
| `PII_ENABLED` | — | `true` | 设为 `false` 关闭脱敏引擎，所有消息直通上游 |
| `PYTHONUNBUFFERED` | — | — | 设为 `1` 可让 Docker 日志实时输出（推荐） |

## 架构

```
main.py (~630 行)
├── /health                   健康检查
├── /v1/models                模型列表（转发上游）
├── /v1/chat/completions      OpenAI 兼容端点
│   ├── get_last_user_content  提取最后一条用户消息
│   ├── parse_mode_prefix      解析 !pii 模式前缀
│   ├── redact_text            调用 argus-redact 脱敏
│   ├── thinking_inject         为 DeepSeek 模型注入思考模式
│   ├── [上游 LLM 调用]
│   ├── restore_text           还原 LLM 响应中的 PII
│   └── SSE 流式缓冲还原       DeepSeek 流式响应处理
│
└── location_names.txt         复合地名列表（430+ 条目）
```

### PII 处理流程

```
用户消息 → parse_mode_prefix() → 识别模式
         → redact_text() → argus-redact redact()
               ├─ Layer 1: regex（names 参数注入）
               └─ Layer 2: HanLP 级联 NER
         → 脱敏后消息 → 上游 LLM
         → LLM 响应 → restore_text() → 还原 PII → 返回用户
```

### SSE 流式处理

DeepSeek 的 SSE 流会将标识符分散在多个 chunk 中（如 `P-00` + `128`），无法逐 chunk 还原。Velum 采用**缓冲-拼接-还原**策略：将所有 SSE chunk 缓存 → 拼接完整文本 → 还原 PII → 打包为单个 SSE 事件返回。

## argus-redact 引擎详解

Velum 的 PII 检测完全委托给 argus-redact。它采用**三层递进架构**：

| 层级 | 机制 | 覆盖范围 |
|------|------|---------|
| **Layer 1: 正则匹配** | 基于规则的正则表达式，匹配手机号、身份证号、邮箱、银行卡号等格式明确的 PII；同时承载 `names` 参数注入的自定义实体 | 格式明确的 PII + 自定义词典 |
| **Layer 2: 级联 NER** | HanLP 2.x 中文命名实体识别。先识别人名 → 地名 → 再基于此识别机构名（级联依赖） | 人名、地名、机构名、学校、日期等 |
| **Layer 3: 语义/LLM** | 保留接口，用于处理语境依赖的 PII（当前未启用） | — |

### HanLP 模型栈

| 组件 | 说明 |
|------|------|
| **编码器** | ELECTRA-small（12 层 Transformer，~14M 参数 / 0.014B） |
| **NER 解码器** | Biaffine NER（将 NER 作为依存分析任务，自然支持 nested/flat 实体） |
| **训练语料** | MSRA（最大中文 NER 语料库）+ OntoNotes 4.0 中文部分 |
| **实体类型** | 56 类（PER/LOC/ORG/GPE/FAC/VEH/...） |
| **标注规范** | PKU 标注集：人名(nr) → 地名(ns) → 机构名(NT)，级联标注 |

> **关键特性**：Biaffine NER 不假设实体扁平——`[北京/ns 大学/n]NT` 中的"北京"既是独立地名实体又是机构名短语的一部分。这种 span-based 方法天然处理复合实体。

## 复合地名增强

HanLP 中文 NER 模型对"成都天府新区""雄安新区"等非标准行政后缀的经济功能区存在识别盲区——它们既不被分类为 ORG（组织），也不被分类为 LOC（地点），导致直接放行。

Velum 内建了 430+ 复合地名列表（国家级新区、经开区、高新区、自贸区等），通过 argus-redact 的 `names` 参数注入 Layer 1 正则匹配层，强制检测。地名列表以纯文本文件 `location_names.txt` 维护，增删无需改代码。

## 限制与已知问题

1. **`names` 参数实体归为 person 类型**：argus-redact 的 Layer 1 正则匹配不经过 NER 分类管线，默认标记为 person。不影响隐私保护——per-type 前缀已按实体类型区分，`detailed=True` 可获取类型信息。
2. **标准行政地名仍依赖 NER**：海淀区卫健委、江苏省人民医院等标准后缀地名由 HanLP NER 覆盖，不在 `names` 列表中。
3. **短文本 NER 可能失效**：少于 8 个字符的输入，HanLP 分词上下文不足，可能漏检。
4. **money 实体不在范围内**：argus-redact 的 56 类实体目录不含 money，人民币金额不脱敏。
5. **透明代理，非加密信道**：如果你的上游 LLM API 使用 HTTP，消息在网络传输中仍然是明文的。

## 测试

两个测试文件位于 `tests/` 目录，用于验证 argus-redact 的 PII 检测行为。测试文件不随 Docker 镜像分发，需在本地或手动传入容器运行。

```bash
# 本地运行（需安装 argus-redact[zh]）
pip install argus-redact[zh]
python tests/test_custom_dict.py
python tests/test_strategies.py

# 或在 Docker 容器中运行（需先 cp 进容器）
docker cp tests/test_custom_dict.py velum:/app/
docker exec velum python /app/test_custom_dict.py
```

### 测试环境

| 组件 | 版本/说明 |
|------|---------|
| **运行环境** | Docker 24+（已在 Synology DSM 7.x 验证） |
| **Python** | 3.12-slim |
| **argus-redact** | ≥ 0.5.0（含 HanLP Chinese NER） |
| **网关** | Hermes Agent (nousresearch/hermes-agent:latest) |
| **上游 LLM** | DeepSeek V4 Pro（via DMXAPI） |
| **内存占用** | < 500MB（含 HanLP 模型） |

### 适用场景

- ✅ 个人 LLM 使用，通过微信/Telegram/Web 等 IM 网关访问
- ✅ 企业内部 LLM 代理，统一 PII 策略
- ✅ 任何 OpenAI 兼容 API 的上游
- ⚠️ 高并发生产环境需加负载均衡（当前为单实例）
- ❌ 需要完整 SOC2/HIPAA 合规的场景（此为技术工具，非认证合规方案）

## 故障排查

### `/health` 返回 `pii: error`

HanLP 模型下载或加载失败。Docker 构建时已预下载，如果仍然失败通常是网络问题。

```bash
# 手动预热（会触发模型下载）
docker exec velum python -c "from argus_redact import redact; redact('测试', lang='zh')"

# 如果下载失败，检查容器网络
docker exec velum python -c "import urllib.request; print(urllib.request.urlopen('https://pypi.org').status)"
```

### `!pii debug` 显示漏检

短文本（< 8 字符）或非标准行政后缀的地名可能被 NER 漏检，这是 HanLP 模型的已知局限。

- **临时方案**：用 `!pii 伪名` 切换到化名模式
- **永久方案**：在 `location_names.txt` 中添加漏检地名，重建镜像或重启容器使更改生效

### 上游 LLM 连接超时

```bash
# 确认环境变量
docker exec velum env | grep UPSTREAM_URL

# 确认容器能连通上游
docker exec velum python -c "
import httpx
r = httpx.get('你的UPSTREAM_URL/models')
print(r.status_code)
"
```

### Hermes 收不到消息

1. 确认 `./hermes/config.yaml` 和 `./hermes/auth.json` 的 `base_url` 都是 `http://velum:8000/v1`
2. 确认 API Key 正确
3. 查看 Hermes 日志：`docker logs -f hermes`

### 容器内存不足

HanLP 模型约 400MB，构建时预下载到镜像中。运行时建议 ≥ 1GB。Docker Compose 中已在 `velum` 服务配置 `mem_limit: 1g`。如果用 `docker run`：

```bash
docker run --memory=1g ...
```

## 许可证

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
