# 脱敏清单（SANITIZATION）

> 对象：本导出目录（整个会议助手服务的开源导出）。本清单**不抄写任何秘密的原文**，只写位置、类别和处理方式。
> “已脱敏”只表示下表列出的项目已处理，**不等于可以公开**：公开前先看文末“待确认”。
> 逐处改动的代码级对照见 `ADAPTATION.md`；实测数字的口径见 `FACTS.md`。

## 一、硬红线处理总览

| 红线 | 处理 |
|---|---|
| 密钥 / token / 密码 / 证书 | 全仓无密钥入库；唯一例外是翻译程序里硬编码的 RetainPDF 翻译模型 key——已改为只读环境变量 `RETAINPDF_MODEL_API_KEY`（无默认值，配置检查把它列为必填），测试改为注入示例值 |
| 真实数据 | 201 条真实术语表 → 37 条合成词表（列结构、状态取值、替换/识别上下文/识别写法的机制全部保留）；测试里的真实公司名、券商名、股票代码、产品编号、会议日期、财务数字 → 合成值；真实录音、逐字稿、纪要、研报、评测样本、金标准不在仓库（见第三节） |
| 公司标识 | 内部 IP / 端口、容器名、项目名、知识库 ID、人员姓名、内部仓库地址 → 通用占位或移除（逐项见第二节） |

第三方公开产品名（RAGFlow、RetainPDF、腾讯云、企业微信/微信客服、Qwen、Whisper、ffmpeg、PostgreSQL）保留——它们是公开产品或上游开源项目，代码本就是为对接它们写的；内部自研系统的名字（研报库前端、数据看板、资讯服务等）一律改为通用描述。

## 二、逐项清单（按文件）

### 代码与注释（不改变行为）

| 文件 | 类别 | 处理 |
|---|---|---|
| `backend/app/api/minutes_api.py` | 内部 IP:端口 | 模块说明里的部署地址删除，改“私有网络” |
| `backend/app/api/meeting_parse_api.py` | 内部产品名 | “AI 会议页（某前端仓库的 Go 后端）”“原解析服务名”改为通用描述 |
| `backend/app/api/summary_api.py`、`backend/app/postprocess/article_summary.py`、`summary.py`、`minutes.py`、`meeting_result.py`、`services/meeting_parse.py`、`jobs.py` 等处注释 | 内部产品名、评测目录名 | 改通用描述；`study/` 评测目录引用删除 |
| `backend/app/integrations/whisper.py` | 公司 GPU 服务器、客户公司名示例、LAN | 改通用描述与通用示例 |
| `backend/app/postprocess/proofread.py`、`glossary.py` | 客户名、真实术语示例、A/B 记录、评测目录 | 改通用描述/合成示例 |
| `backend/app/postprocess/glossary.csv` | 真实术语表（201 条，含真实出处链接） | **整体替换**为合成词表（37 条“通过”+2 条演示状态；证券投研/医药/半导体AI 三类；机制与列结构不变） |
| `asr_service/api.py`、`engine.py`、`chunks.py`、`qwen.py`、`sentences.py` | 客户名、真实识别数字片段、评测目录、具体日期 | 改通用描述与合成数字 |
| `scripts/e2e_local.py`、`eval_minutes.py`、`eval_summary.py` | 评测目录路径、客户名注释 | 默认输出目录改为本地目录，注释改合成名 |
| `ragflow/rag/svr/translate_worker.py` | RetainPDF 翻译模型 key（硬编码默认值） | 改为只读 `RETAINPDF_MODEL_API_KEY`，无默认值；`main()` 的配置检查把它列为必填；任务请求按次注入，日志与代码均不含 key |
| `ragflow/patches/report-search-dataset-env.patch` | 5 个真实知识库 ID（32 位十六进制） | 替换为占位符（补丁是模式示例；部署方换自己的 ID） |
| `ragflow/patches/asr-title.patch` | 客户名 | 改通用描述 |

### 测试（断言逻辑不变，数据合成）

| 文件 | 处理 |
|---|---|
| `tests/unit/test_article_summary.py` | 用此前单模块导出的合成版替换（真实券商名、上市公司名及代码、报告数字 → “示例证券/示例乳业/000000”等） |
| `tests/unit/test_minutes.py`、`test_minutes_api.py`、`test_meeting_parse.py`、`test_meeting_result.py`、`test_whisper.py`、`test_deps.py`、`test_asr_service_*.py`、`test_glossary.py`、`test_proofread.py`、`tests/integration/test_results.py`、`ragflow/tests/test_audio.py` | 客户公司化名、产品编号、会议日期、真实数字、内网 IP、URL 中的主机名 → 合成值（“示例科技/晨川科技”、XB1002、2026-03-18、10.0.0.5 等）。替换由 `private/sanitize_tests.py`（内部工具，不导出）驱动，每条规则命中数有记录 |
| `tests/unit/test_glossary.py`、`test_proofread.py`、`test_asr_service_api.py` | 断言里引用的真实术语（如某券商名、某生物制品的错写）换成合成词表里对应的条目 |
| `ragflow/tests/test_translate_worker.py` | 固定真实 key 的用例改为注入示例 key；新增“模板本身不含 key”的断言 |
| `tests/unit/test_translate_worker.py` 无关 | — |

### 文档与部署文件

| 文件 | 处理 |
|---|---|
| `ragflow/README.md` | **重写**：保留架构图、文件表、环境变量、接口文档、测试说明；删除部署日记（内部 IP、容器名、知识库 ID、同事姓名缩写、分支名、提交号） |
| `ragflow/ops.sh` | 内部同步/部署脚本（含内部仓库地址、内网 IP、同事账号），**文件已删除** |
| `asr_service/README.md` | 重写：删除服务器 IP、ssh 账号、评测目录引用；客户名轶事改通用描述；补离线替身（`ASR_ENGINE=fake`）一节 |
| `.env.example` | 重写：新增 `STORAGE_BACKEND=cos|local` 与 `LOCAL_STORAGE_*`、`ASR_QWEN_URL/ASR_FUNASR_URL`、`LLM_API=fake`、`MEETING_PARSE_DB`；公司专属表述改通用 |
| `docker-compose.yml`（根） | 改为**离线全栈**（postgres + redis + 离线 ASR + 离线 API，端口只绑 127.0.0.1，密钥走环境变量）；真实模式的 compose 见各子目录 |
| `backend/Dockerfile`、`asr_service/Dockerfile` | 删除内网 pip 镜像源、ssh 部署注释、服务器描述 |
| `asr_service/docker-compose.yml` | 服务器 IP、模型目录硬编码 → `${ASR_MODELS_DIR:-~/models}`；注释通用化 |
| `backend/deploy/docker-compose.yml` | 项目名/容器名、内网 IP、内部前端名 → 通用；ASR 地址走 `${ASR_QWEN_URL}` |
| `asr_service/docker-compose.fake.yml`、`asr_service/Dockerfile.fake` | 新增（离线替身服务的 compose/镜像，全合成） |
| `README.md` | 重写（旗舰仓），实测数字以 `FACTS.md` 口径为准 |
| `AI_会议助手_全链路开发方案_V1.1.md`、`评审问题与修改方案_V1.0.md`、`archive/v1.0/` | **原样保留**：方案文档只用了公开占位（`api.example.com`、张三）和第三方官方文档链接，无内部信息 |
| `schema.sql`、`backend/alembic/` | 原样保留（数据模型本身，无敏感信息） |
| `shared/meeting_protocol/` | 原样保留 |

### 新增文件（导出适配，全部合成，无敏感信息）

`backend/app/integrations/fake_llm.py`（离线替身 LLM）、`backend/app/integrations/local_storage.py` + `backend/app/api/files.py`（本地对象存储与预签名链接）、`asr_service/fake_engine.py`（离线替身识别引擎）、`tests/unit/test_fake_llm.py`、`test_fake_engine.py`、`test_local_storage.py`、`test_files_api.py`、`tests/unit/test_article_summary_rewrite_and_drop.py` 与 `test_article_summary_figure_rules.py`（从单模块导出带回的规则测试）。替身只在 `LLM_API=fake` / `ASR_ENGINE=fake` / `STORAGE_BACKEND=local` 时启用，真实实现仍是主体。

## 三、没有导出的内容及原因

| 内容 | 位置 | 原因 |
|---|---|---|
| AI 会议页等前端页面 | 另一仓库 `claude-code-webui`（前端由另一位同事负责） | 不在本仓库；转达稿第 10 条“前端（如在同仓）”——不在同仓，需要单独授权与脱敏，另行处理 |
| 数据看板后端 | 另一仓库 `data-platform` | 同上，不在本仓库 |
| 资讯采集与去重服务 | 另一仓库 `rss-news` | 同上（转达稿第 7 条“如在同仓”）；本仓库没有该代码 |
| 评测与真实数据（`study/`） | 本机，git 忽略 | 真实会议录音、逐字稿、纪要、金标准、真实研报与资讯样本、术语表旧版——按红线不带出 |
| 会话记录、实习日志与笔记 | 本机 Obsidian / Claude 会话目录 | 含明文 key、内网地址、人名，一律不导出 |
| 内部仓库地址、分支名、提交号、同事姓名 | git 历史与部署文档 | 已脱敏；导出目录**不含 .git** |
| 真实密钥与 .env | 从未入库；本机 `backend/.env` 等 | 不导出；`.env.example` 已更新 |
| `uv.lock`、`scripts/pipeline_minutes.py`、`study/` | 本机，git 忽略 | 本地文件，不进导出 |
| `ragflow/ops.sh` 原内容 | 本仓库 | 内部部署脚本（见第二节） |

## 四、导出验证（部分完成）

2026-11-14 在本机（WSL2、Python 3.12，复用本项目虚拟环境）实测：单元 900 通过；RAGFlow 侧 85 通过、2 跳过；临时 PostgreSQL 16 下全量 1198 通过。验证中修掉 5 个替身相关测试暴露的问题（详见 `FACTS.md` 第十节）。

**未执行**：离线端到端（合成音频 → 替身 ASR → 替身 LLM → 结果页）、断网命名空间复跑、全新虚拟环境安装、敏感残留 grep 扫描——2026-11-14 决定不再展开跑测试。

## 五、待确认

1. **版权与 LICENSE**：代码写于实习期间、来自公司内部仓库；公司已口头确认脱敏后可开源，但发布前建议补书面确认并选一个 LICENSE（当前未附）。
2. 此前单模块导出核对时发现：一次换模型网关 key 的会话记录里有**明文 key**。与本仓库无关（本仓库从不含 key），但建议确认那把 key 已停用/更换。
3. 第二节保留的第三方产品名（RAGFlow、RetainPDF、腾讯云、企业微信）是否都允许出现在公开仓库——目前按“公开产品名保留”处理。
4. `AI_会议助手_全链路开发方案_V1.1.md` 等设计文档里“保底 MVP 用微信/腾讯会议”的目标描述与现状（只做音频→纪要，范围已收窄）不一致，是否要加一段现状说明，由发布者定。
5. `FACTS.md` 中 3 项数字（摘要校验 19 份样本的构成、翻译测试 14→51、资讯去重 26 条/3.4 秒）尚未核准到原始出处，已在该文件标“待复核”。
