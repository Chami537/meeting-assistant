# AI 文档与会议处理服务

一套 AI 文档与会议处理服务：**会议纪要**（录音 → 转写 → 结构化纪要）、**文章摘要与引用/数字校验**（PDF → 逐句标出处的摘要，代码校验每个数字）、**文档翻译**（英文 PDF → 保留版面的中文版）、以及批量导入时的**过载治理**。资讯采集与去重是另一个仓库，不在这里。

一个仓库、四个可独立运行的进程，全部通过 HTTP 对接：

```
┌─────────────────────────── 录音 ───────────────────────────┐
│                                                            │
▼                                                            ▼
asr_service/（转录服务，GPU）                    backend/（FastAPI）
  Qwen3-ASR / Fun-ASR / 离线替身                      ├─ POST /v1/minutes       写纪要（+术语校对）
  · 按停顿切分、说话人聚类、数字转换                  ├─ POST /v1/summary       写摘要（逐句引用+数字校验）
  · 任务队列（容量 32，满则 429+Retry-After）         ├─ POST /meeting/parse    会议页任务（逐句时间戳）
  · 逐句时间戳（可选对齐模型）                        ├─ /files                本地存储的预签名链接
  · 插队、重试、24 小时保留                          ├─ /healthz /readyz
                                                     └─ 结果页/上传页/微信回调（首版链路）
ragflow/（RAGFlow 对接模块，放到上游源码树里）
  · 录音解析：转写→存识别原文→纪要→建索引
  · 摘要后台程序：每 5 分钟一轮，只挑还没摘要的 PDF
  · 翻译后台程序：每 1 分钟一轮，英文 PDF 交给 RetainPDF
  · 附加内容接口：一页搜索结果的摘要/纪要/译稿
```

## 模块地图

| 目录 | 内容 |
|---|---|
| `asr_service/` | 自建转录服务：Qwen3-ASR / Fun-ASR 引擎、切分与说话人、数字转换、逐句时间、**任务队列与过载治理（429）**、离线替身引擎（`ASR_ENGINE=fake`） |
| `backend/app/api/` | FastAPI 接口层：纪要、摘要、会议页任务、结果页、上传页、微信回调、Worker 协议、本地存储链接（422/502/503/429 状态码映射、并发名额） |
| `backend/app/postprocess/` | 纪要写作与校验（`minutes.py`、写作规范 `skills/meeting-minutes/SKILL.md`）、**逐句引用与数字校验**（`article_summary.py` + `skills/article-summary/SKILL.md`）、**术语表校对**（`proofread.py`）、作业执行与重试（`jobs.py`）、会议页结果（`meeting_result.py`） |
| `backend/app/runner.py` | 后台调度：每 5 秒一轮（调度→对账→收件箱→副作用→发件箱→清理），advisory lock 单实例，作业租约/重试 |
| `backend/app/domain/` | 任务状态机（纯函数，16 状态 26 触发器） |
| `backend/app/integrations/` | 真实模型客户端：Claude SDK、OpenAI 兼容、腾讯云 ASR/COS、微信、自建转录服务的客户端；离线替身（fake LLM）与本地对象存储 |
| `backend/app/services/` | 微信收发与对话规则、上传、结果、清理、会议页任务（SQLite，重启接着做） |
| `backend/alembic/`、`schema.sql` | PostgreSQL 16 数据模型与迁移 |
| `shared/meeting_protocol/` | 后端与 Worker 共用的协议（枚举、规则、接口模型） |
| `ragflow/` | RAGFlow 对接模块：录音解析、摘要/翻译后台程序、附加内容接口、预览页补丁（放到 RAGFlow v0.25.1 源码树，见其中 README） |
| `scripts/` | 本地端到端驱动（`e2e_local.py`，也是离线模式驱动）、纪要/摘要评测脚本 |
| `tests/`、`ragflow/tests/` | 单元 + 集成（临时 PostgreSQL）+ RAGFlow 侧替身测试 |

## 实测结果

数字口径与出处见 `FACTS.md`（那里标了哪些有真实验证、哪些只有单测）。

- **会议纪要链路**：2026-10-10 首次全链路（43 分钟业绩会录音 → 2 分 23 秒出纪要，ASR 费用约 0.6 元）；之后换成自建 Qwen3-ASR 转录服务（43 分钟约 50 秒识别、带说话人）+ Haiku 5.5 写纪要；部署后一次实测 42 秒出纪要、按 41 项金标准得 36 分（单次结果，见 FACTS 口径）。
- **ASR 队列 4→32 与 429+Retry-After**：批量导入最多 24 个录音并发，队列满从“503 直接失败”改为“429 + Retry-After: 30”，默认容量 32（含执行中的 1 个）。
- **术语校对**：201 条术语表（证券投研/医药/半导体AI，每条带出处），识别上下文短名单 24 条；25 份对照组纪要离线回放：误改 0、已知错写漏改 3，每份多约 10 秒。
- **引用校验**：摘要每句必须标原文出处，代码校验句中每个数字在原文出现，引用不准或超长重写一次，两稿取优，删句超 1/3 整份拒收；试点 13 份全部入库、逐句核对 12 份无问题。（“19 份样本”口径待复核，见 FACTS.md 第四节。）
- **PDF 摘要故障修复**：模型网关限流把 96 份没摘要的 PDF 判成失败、3 次放弃；改为“服务故障回 503、不记文档失败”，修复后第一轮 10 份 0 失败，进度 127/223→137/223。
- **翻译**：英文 PDF 交 RetainPDF 出保留版面的中文版，防重复提交靠文档 ID + 内容哈希 + 任务列表对账，放弃判断存表跨重启；测试环境端到端验证通过。（测试数 14→51 的增长口径待复核。）
- **资讯去重**（另一仓库，不在本导出）：字二元组/向量相似 + 数字一致性判重；首轮 100 条判 26 重复（口径待复核）。

## 快速开始

依赖：Python 3.12+、ffmpeg/ffprobe（处理录音）、PostgreSQL 16（完整后端；只用纪要/摘要接口可不要）。

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests/unit     # 单元测试，不需要数据库
```

### 离线模式（没有 GPU、没有模型 key，跑通全链路）

```bash
cp .env.example .env    # 生成 POSTGRES_PASSWORD / LOCAL_STORAGE_SECRET / DATA_ENCRYPTION_KEYS
# 1) 数据库（或用根目录 docker compose：docker compose up -d postgres redis）
docker compose up -d postgres redis
# 2) 离线替身转录服务（端口 8020）
ASR_ENGINE=fake .venv/bin/python -m uvicorn asr_service.api:app --port 8020 &
# 3) 后端用替身模型 + 本地存储（.env 里 LLM_API=fake、ASR_PROVIDER=qwen、ASR_QWEN_URL=http://127.0.0.1:8020、STORAGE_BACKEND=local）
cd backend && ../.venv/bin/python ../scripts/e2e_local.py 一段录音.m4a --clip 30
#     → 合成识别 → 替身模型写纪要（带【离线替身】标记）→ 打开结果页
```

或者一条命令起全部（离线全栈 compose，端口只绑 127.0.0.1）：`docker compose up -d --build`，然后按根 compose 顶部注释里的 curl 直接调 `/v1/minutes`。替换引擎生成的纪要每份都带“【离线替身】”标记，**不要配置到生产**。

### 真实模式（配 key / GPU）

1. 转录服务：按 `asr_service/README.md` 部署（Qwen3-ASR 需要 NVIDIA GPU，约 7GB 显存；模型放 `ASR_MODELS_DIR`）。
2. 纪要/摘要服务：`cp backend/deploy/docker-compose.yml` 到服务器，写 `minutes.env`（`LLM_API=anthropic` 时：`LLM_MODEL=claude-haiku-5-5`、`LLM_EFFORT=medium`、`LLM_MAX_OUTPUT_TOKENS=16000`、`ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY`；或 `LLM_API=openai` 配任意兼容端点），`ASR_QWEN_URL` 指到转录服务，`docker compose up -d`。
3. 直接调接口：`POST /v1/minutes`（`{"title": ..., "segments": [{start, end, text, speaker}]}`）、`POST /v1/summary`（`{"title": ..., "chunks": [{text, page}]}`）；错误码：413 输入过长 / 422 无正文 / 502 这份请求失败 / 503 模型服务故障（可稍后再试）。
4. 完整后端（企业微信入口、任务状态机、结果页）：起 PostgreSQL、`alembic upgrade head`，配齐 `.env`（微信、存储、ASR、LLM、加密密钥），跑 `python -m app.runner` + uvicorn；设计文档 `AI_会议助手_全链路开发方案_V1.1.md` 第 9 章有逐步说明。

## 开源版验证

2026-11-14 在本机（WSL2、Python 3.12，复用本项目虚拟环境，**未新建干净 venv**）实测：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest -q tests/unit` | 900 passed |
| `cd ragflow && ../.venv/bin/python -m pytest -q tests` | 85 passed, 2 skipped |
| 临时 PostgreSQL 16 + `.venv/bin/python -m pytest -q tests`（单元+集成） | 1198 passed |

验证中修掉 5 个替身相关测试暴露的问题（本地存储 key 校验、`/files` 403/404 预期、替身 ASR 测试音频、替身 LLM 分组阈值），细节记在 `FACTS.md` 第十节。**离线端到端与断网复跑未执行**（2026-11-14 决定不再展开），离线链路由单元测试覆盖。

发布前复核（2026-11-14）：在**全新虚拟环境**中按上面的快速开始从零安装后复跑，三项结果与上表一致——单元 900 passed、RAGFlow 侧 85 passed / 2 skipped、临时 PostgreSQL 16 全量（单元+集成）1198 passed。复核中顺带修正：`anthropic` 依赖限定为 `<2`（2.x 改了异常类名，尚未适配）、ruff 检查清零。

## 测试

```bash
.venv/bin/python -m pytest tests/unit          # 单元
cd ragflow && ../.venv/bin/python -m pytest tests   # RAGFlow 侧（替身，不连数据库）
# 集成测试需要 PostgreSQL 16（TEST_DATABASE_URL；docker 起临时库的脚本见 tests/integration/conftest.py 说明）
.venv/bin/python -m pytest tests               # 全部（每会话自动建/删测试库）
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## 文档

- `FACTS.md` — 实测数字的口径与出处（哪些有真实验证、哪些只有单测；三项“待复核”已标注）。
- `SANITIZATION.md` — 脱敏清单（替换/移除的位置与类别，不写原值）。
- `ADAPTATION.md` — 为独立运行所做的每一处改动的对照，以及未导出模块的原因和未完成事项。
- `AI_会议助手_全链路开发方案_V1.1.md` / `archive/v1.0/` — 最初的设计与评审文档（微信/腾讯会议全链路方案，现状已收窄为音频→纪要，见 SANITIZATION.md 待确认第 5 条）。
- `ragflow/README.md`、`asr_service/README.md` — 两个模块各自的部署与配置说明。
