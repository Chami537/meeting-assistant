# 适配清单（ADAPTATION）

> 为独立运行所做的每一处改动：原文件 → 导出文件 → 改了什么 → 为什么。只列**行为改动**；仅注释/文案脱敏的改动汇总在第二节，逐处对照在 `SANITIZATION.md` 第二节（那里不写原值，原文件在公司仓库）。新增文件全部为合成内容。

## 一、新增文件

| 文件 | 是什么 | 为什么 |
|---|---|---|
| `backend/app/integrations/fake_llm.py` | 离线替身模型（`LLM_API=fake`）：不联网不要 key，按真实提示词/校验规则从逐字稿抽取式作答，每份输出带“【离线替身】”标记 | 没有模型 key 时跑通全链路；真实模型实现仍是主体 |
| `backend/app/integrations/local_storage.py` | 本地对象存储（`STORAGE_BACKEND=local`）：对象=目录下文件，预签名链接用 HMAC 签名，PUT 绑定承诺的 sha256 | 没有云存储桶时跑通上传链路的存储抽象 |
| `backend/app/api/files.py` | `/files/{key}` 路由：本地存储预签名链接的 GET/PUT | 预签名链接要有个服务端 |
| `asr_service/fake_engine.py` | 离线替身识别引擎（`ASR_ENGINE=fake`）：只读音频时长（WAV 头或 ffprobe），按合成剧本生成分段（说话人、数字经真实转换、逐句时间） | 没有 GPU/模型时跑通识别→纪要链路 |
| `asr_service/Dockerfile.fake`、`asr_service/docker-compose.fake.yml` | 离线 ASR 的镜像与 compose（python:slim，无 GPU 依赖） | 离线模式一键起服务 |
| `tests/unit/test_fake_llm.py`、`test_fake_engine.py`、`test_local_storage.py`、`test_files_api.py` | 新增模块的测试（含“替身引擎下队列排满回 429”的过载用例） | 导出适配必须有测试 |
| `tests/unit/test_article_summary_rewrite_and_drop.py`、`test_article_summary_figure_rules.py` | 从单模块导出带回的规则测试（重写/删句边界、数字匹配规则，全合成数据） | 之前单模块导出新增、原仓库没有的测试，一并纳入 |
| `SANITIZATION.md`、`FACTS.md`、`ADAPTATION.md` | 脱敏清单、数字口径、本清单 | 转达稿交付物 |

## 二、修改文件（行为改动）

| 原文件 → 导出文件 | 改动 | 为什么 |
|---|---|---|
| `backend/app/config.py` | `llm_api` 增加 `"fake"`；新增 `storage_backend=cos\|local` 与 `local_storage_dir/url/secret` | 离线模式与本地存储的开关，全走环境变量 |
| `backend/app/deps.py` | `llm_client()` 支持 fake；`_object_storage()` 按 `storage_backend` 选 COS 或本地；新增 `local_storage_or_none()` | 同一依赖入口，真实/离线/本地三态切换 |
| `backend/app/main.py` | 挂载 `files` 路由 | 本地存储的预签名链接由 API 服务 |
| `asr_service/api.py` | `ASR_ENGINE` 增加 `"fake"`（延迟加载 fake 引擎，不 import torch）；健康检查 model 显示 `offline-fake-asr` | 离线识别服务；真实 Qwen/Fun-ASR 路径不变 |
| `ragflow/rag/svr/translate_worker.py` | 翻译模型 key 改为只读 `RETAINPDF_MODEL_API_KEY`（删除硬编码默认值）；`main()` 配置检查把它列为必填；`JOB` 模板不再含 key，提交请求时按次合并注入 | 红线：密钥不进代码。行为上唯一变化是没配 key 时程序空等（原本会带一把内网 key 工作） |
| `ragflow/tests/test_translate_worker.py` | 固定 key 的用例改为注入示例 key；新增“模板本身不含 key”的断言；提交断言按“按次注入”的合并方式写 | 跟随上面的改动 |
| `tests/unit/test_article_summary.py` | 用单模块导出的合成版替换（import 路径改回 `app.postprocess`） | 数据脱敏 |
| 其余 `tests/unit/*`、`tests/integration/test_results.py`、`ragflow/tests/test_audio.py` | 真实名称/编号/日期/IP 换成合成值（替换脚本在内部目录，每条规则有命中数记录） | 数据脱敏 |
| `docker-compose.yml`（根） | 从“仅 PG+Redis”改为离线全栈（+ 离线 ASR + 离线 API），端口只绑 127.0.0.1 | 转达稿“离线模式一键启动” |
| `backend/deploy/docker-compose.yml` | 项目/容器名通用化；`ASR_QWEN_URL` 走 `${ASR_QWEN_URL}`，默认 127.0.0.1:8020；注释去内部信息 | 脱敏 + 可配置 |
| `asr_service/docker-compose.yml` | 模型目录 `${ASR_MODELS_DIR:-~/models}`；注释去 IP | 脱敏 + 可配置 |
| `backend/Dockerfile`、`asr_service/Dockerfile` | 删内网 pip 镜像与 ssh 部署注释 | 公开构建 |
| `.env.example` | 新增 STORAGE_BACKEND/LOCAL_*/ASR_QWEN_URL/ASR_FUNASR_URL/LLM_API=fake/MEETING_PARSE_DB 及离线快捷说明 | 新配置项必须有模板 |
| `scripts/e2e_local.py`、`eval_minutes.py`、`eval_summary.py` | 默认输出目录从内部评测目录改为本地目录；e2e 文档说明补离线模式用法 | 不依赖仓库外目录 |
| `ragflow/README.md`、`asr_service/README.md` | 重写（去内部信息，补离线模式说明） | 脱敏 |
| `ragflow/ops.sh` | 已删除 | 内部部署脚本，无法通用化 |
| `ragflow/patches/report-search-dataset-env.patch` | 默认 KB ID 换占位符 | 红线：真实文档 ID |
| `ragflow/patches/asr-title.patch` | 客户名改通用描述 | 红线 |
| `backend/app/postprocess/glossary.csv` | 真实 201 条 → 合成 37+2 条（列结构与机制不变） | 红线：真实术语表内容 |

## 三、未导出的模块及原因

| 模块 | 原因 |
|---|---|
| AI 会议页等前端页面（claude-code-webui 仓库） | 不在本仓库；前端归另一位同事维护；需单独授权与脱敏。会议页的**后端契约**已导出（`meeting_parse_api.py`、`services/meeting_parse.py`、`postprocess/meeting_result.py`） |
| 数据看板后端（data-platform 仓库） | 不在本仓库 |
| 资讯采集与去重服务（rss-news 仓库） | 不在本仓库（转达稿第 7 条“如在同仓”）；FACTS.md 记录了它的口径 |
| `study/` 评测数据（录音、逐字稿、金标准、报告） | 真实数据，红线 |
| 企业微信/腾讯会议的**真机联调与部署配置** | 部署配置（服务器 env、密钥）从不入库；代码本身（`integrations/wecom/*`、回调、上传/结果页）已导出 |
| git 历史、内部仓库地址、同事姓名 | 脱敏；导出不含 .git |

## 四、未完成事项

1. **开源版验证只做了一部分**（2026-11-14）：单元 900、RAGFlow 侧 85（2 跳过）、临时 PostgreSQL 全量 1198 均通过；**离线端到端、断网复跑、新虚拟环境安装、敏感残留扫描未执行**（用户决定不再展开跑测试）。结果见 `FACTS.md` 第十节。
2. `FACTS.md` 三项“待复核”数字（摘要 19 份构成、翻译测试 14→51、去重 26 条/3.4 秒）待核准到原始出处后更新 README。
3. LICENSE 待定（发布前，见 SANITIZATION.md 待确认第 1 条）。
4. 真实模式冒烟（付费服务）未做：如要跑，会调用真实 ASR/LLM，需要先确认（转达稿：“真实模式冒烟如需调用付费服务，先在清单里标注，等我确认”——此条即标注）。
5. docker-compose 离线全栈未在本机验证（本机无 Docker）。
