# RAGFlow 接入模块

会议助手接进 [RAGFlow](https://github.com/infiniflow/ragflow)（v0.25.1）时改过的文件，都在这个目录里存一份：在这里改、在这里测，再同步到 RAGFlow 的源码树里（原部署用一个内部同步脚本，已随内部基础设施一起移除，见 SANITIZATION.md）。文件按 RAGFlow 的代码风格写，不归本仓库的 ruff 管，也不打进纪要服务的镜像。

`submit_result` 与入库、建索引在另一个入库服务里（不在本仓库）；纪要和摘要的写法在本仓库的 `backend/app/postprocess/`（`minutes.py`、`article_summary.py`），由纪要/摘要服务提供。

## 做了什么

```
录音上传到 RAGFlow ── 解析（rag/app/audio.py）
   ├─ 调转录服务，文件名作为上下文（patches/asr-title.patch）
   ├─ submit_result(asr_transcript)        ← 识别原文：整份保存，不建索引
   ├─ 调纪要服务 /v1/minutes
   └─ submit_result(meeting_minutes)       ← 纪要：整份 JSON 保存，只用 markdown 建索引；解析本身不产 chunk

PDF 解析完 ── 摘要后台程序（rag/svr/summary_worker.py，每 5 分钟一轮）
   ├─ chunk_list 按页码读出原文 chunk
   ├─ 调纪要服务 /v1/summary
   └─ submit_result(summary)               ← 摘要：整份 JSON 保存（含提示词版本、整篇/分组、字数），只用 summary 建索引，和原文 chunk 共存

英文 PDF 解析完 ── 翻译后台程序（rag/svr/translate_worker.py，每分钟一轮）
   ├─ 上传原 PDF 到 RetainPDF，建任务，等它做完 OCR、翻译、排版
   ├─ 下载中文 PDF
   └─ submit_result(translation)           ← 译稿：PDF 作为附件只存不建索引，检索不变

研报类前端和 PDF 预览页 ── 取附加内容
   ├─ POST /api/v1/reports/enrichments     ← 一页搜索结果的摘要、纪要、有没有译稿（API key）
   ├─ GET  /api/v1/datasets/<库>/documents/<文档>/translation   ← 译稿 PDF（API key）
   └─ GET  /api/v1/chunk/pdf-preview/translation[/file]        ← 预览页的原文/译文切换（免登录，同 /chunk/pdf-preview）
```

## 文件

| 这里 | RAGFlow 里 | 说明 |
|---|---|---|
| `rag/app/audio.py` | 同路径，整个文件 | 录音解析：识别 → 存识别原文 → 写纪要 → 存纪要。纪要失败或入库失败，解析就失败 |
| `rag/svr/summary_worker.py` | 同路径，新文件 | 摘要后台程序，见下 |
| `rag/svr/translate_worker.py` | 同路径，新文件 | 翻译后台程序，见下 |
| `api/apps/services/enrichment_lookup_service.py` | 同路径，新文件 | 查文档当前的摘要、纪要、译稿，见下“附加内容接口” |
| `api/apps/sdk/enrichment.py` | 同路径，新文件 | `/reports/enrichments`、`/datasets/<库>/documents/<文档>/translation`（API key） |
| `api/apps/restful_apis/chunk_pdf_translation_api.py` | 同路径，新文件 | `/chunk/pdf-preview/translation`、`/translation/file`（免登录） |
| `patches/report-search-dataset-env.patch` | `api/db/services/report_search_service.py` | 研报库搜索的 5 个业务库 ID 改为读环境变量（和入库程序同名：`RAGFLOW_CNINFO_DATASET_ID`、`ZSXQ_RAGFLOW_DATASET_ID`、`RAGFLOW_COMPANY_DATASET_ID`、`RAGFLOW_INDUSTRY_DATASET_ID`、`RAGFLOW_GENERAL_DATASET_ID`），没配就用默认 ID；值不是 32 位十六进制直接报错（补丁里的默认 ID 是占位符，部署方要换成自己的） |
| `patches/pdf-preview-translation.patch` | `web/src/pages/pdf-evidence-preview/index.tsx`、`index.module.less` | 预览页右上角的原文/译文切换，有译文才显示 |
| `patches/asr-title.patch` | `rag/llm/sequence2txt_model.py`、`api/db/services/llm_service.py` | 把文件名作为上下文传给 Qwen3-ASR（只对名字含 qwen 的模型发） |
| `patches/summary-worker.patch` | `entrypoint.sh`、`deploy.sh` | 容器启动时拉起摘要后台程序；`deploy.sh` 重启时带上它 |
| `patches/translate-worker.patch` | `entrypoint.sh`、`deploy.sh` | 同上，翻译后台程序；要在 `summary-worker.patch` 之后打。改动特意离摘要那几行远一点，两个补丁的上下文互不相碰，打过以后都还认得出来 |
| `tests/` | — | 把 RAGFlow 换成替身的单元测试 |

补丁按上面的顺序打：前两个补丁是相对 RAGFlow 原始版本的 `git diff`，`translate-worker.patch` 是相对打过前两个的状态，`pdf-preview-translation.patch` 只动前端那两个文件，和其他补丁不相干。

## 摘要后台程序

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SUMMARY_KB_IDS` | 空 | 要做摘要的知识库 id，逗号分隔；写 `all` 就是所有在用的知识库，以后新建的也包括在内。**空就是关**：程序只空等，不退出（退出的话 `entrypoint.sh` 会每秒把它拉起来一次） |
| `SUMMARY_URL` | 空 | 摘要接口（纪要服务的 `/v1/summary`）。空的话每份都按失败算 |
| `SUMMARY_INTERVAL` / `SUMMARY_BATCH` | 300 / 10 | 每轮间隔秒数、每轮最多几份 |
| `SUMMARY_SERVICE_VERSION` | `1.0.0` | 提交时的 `service_version`，是程序的版本；提示词版本在 payload 的 `prompt_version` 里 |

- 挑文档：这些知识库（没被删除的）里解析完成（`run=3`、`progress=1`）、有 chunk、还没有摘要或摘要已过期/失败的 PDF，按创建时间先后。
- 失败：同一份最多试 3 次（记在内存里，重启清零）；1 小时内提交过的跳过（入库是后台慢慢做的）；纪要服务连不上或返回 503 时，这一轮直接结束。
- 长期开着要两件事：`SUMMARY_KB_IDS` 写进 RAGFlow 的环境（`entrypoint.sh` 读的那份），以及容器重启一次让 `entrypoint.sh` 的改动生效。在那之前只能手动起，下次 `deploy.sh` 会把它停掉。

## 翻译后台程序

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TRANSLATE_KB_IDS` | 空 | 同 `SUMMARY_KB_IDS`，可写 `all` |
| `TRANSLATE_SINCE` | 空 | 只翻这之后上传的，ISO 时间，带时区：`2026-10-27T10:00:00+08:00`。往前改就能补翻存量 |
| `RETAINPDF_URL` | 空 | RetainPDF 的入口地址 |
| `RETAINPDF_MODEL_API_KEY` | 空 | 建任务时给 RetainPDF 翻译模型的 `api_key`（由维护方随部署给出，**没有默认值**，代码里不放任何密钥） |
| `TRANSLATE_INTERVAL` | 60 | 每轮间隔秒数 |

四个都配了才干活，缺一个就只空等。

- 挑文档：这些知识库里 `TRANSLATE_SINCE` 之后上传、解析完成、还没有译稿或译稿已过期/失败的 PDF，按上传先后。前 20 个原文 chunk（不算摘要、纪要块；只取按页排序的前 128 块，一次请求）里汉字不到字母的 10% 才算英文；中文的记进放弃记录（见下，按文档 id 和 `content_hash`），内容变了才再看；读不到 chunk 算一次失败。上传前核对读到的文件和 `content_hash`（xxh128）一致，刚被替换的这一轮先不交。过期只在原文件内容变了时发生（`content_hash`），重新解析不会让它重翻。
- RetainPDF 是共用的、一次一个任务：我们最多一个任务在那边，只在它空闲（`/health` 的 `queue_depth`、`running_jobs` 都是 0）时提交，等满 2 小时就照常提交。计时只在交成功、没有待交的文档、或我们自己有任务在跑时清零：那次没交成、被拒收换下一份，都接着计。
- 不记本地账：上传的文件名是 `ragflow-<文档 id>-<content_hash 前 8 位>-<原名>`，每轮读 RetainPDF 的任务列表（只看 `workflow=book`，不看 `-ocr` 子任务；列表按 `updated_at` 倒序，每轮先给候选判断语言（每份每种内容只判断一次），再读到比最早上传的那份英文候选更早更新的那页为止，留 1 小时时钟误差；超过 5000 条还没读完，这一轮就不交，免得漏看任务重复付费）认出自己的任务。文件名的原名部分会截短，整个名字不超过 240 字节（RetainPDF 的译稿叫 `<原名>-translated.pdf`，也得在 255 字节以内）。建任务超时、进程重启都不会重复提交，因为下一轮会在列表里看到它。这只在同一时间只有一个进程提交时成立：提交的进程拿着 `/tmp/ragflow_translate_worker.lock`，守护进程等它，守护进程在跑时 `--once` 拿不到就退出。
- 失败：RetainPDF 上同一份失败（含取消）2 次就不再交；下载、入库失败在本进程里记，3 次放弃（重启清零）；超过 300 页、原文件超过 64MB（入库上限，译稿不会比原文小）或被 RetainPDF 拒收的不交；`/health`、任务列表连不上或返回 429/5xx 时这一轮直接结束；上传返回的 503 只有 `PDF repair timed out`（这份 PDF 修不好）记在文档头上、3 次后放弃它，免得一份坏文件挡住后面的；其他 503（处理能力满、磁盘不足等）和 429 都是服务的问题，结束这一轮、不记。本地失败按文档和内容记，成功就清零。
- 放弃记录：重试也没用的判断（不是英文、超过 300 页、文件太大、被 RetainPDF 拒收、RetainPDF 上失败 2 次）写进 RAGFlow 库里程序自己建的表 `translate_worker_skip`（文档 id 主键、`content_hash`、原因、时间；程序启动时 `CREATE TABLE IF NOT EXISTS`，`--dry-run` 不建也不写）。每次部署都会重启翻译程序，有了这张表重启后不再把上线以来的 PDF 重判一遍；文件换了（`content_hash` 变）会重新判断。程序自己连续 3 次出错（读切块、下载、入库）只记在内存，重启后再试。想让某篇重来就删掉它那一行，比如改了 `MAX_PAGES`、`MAX_CJK_SHARE` 以后：`DELETE FROM translate_worker_skip WHERE reason LIKE '%pages%'`（RetainPDF 上失败过 2 次的，只要那两次还在它的任务列表里，删了下一轮又会记回来）。文件在同一个文档下被替换、又没有重新解析时，程序按旧切块判断语言，解析完要删掉那一行。表如果和 `document` 表连不上（比如排序规则不同），程序启动时会报错并退回只记内存，不会每轮失败。
- 入库前再查一次原文件的 `content_hash`，变了就不交。入库服务在后台落库时才记下文档当时的内容哈希，如果原文件恰好在这几秒里被换掉，旧内容的译稿会被当成新内容的；要彻底避免，需要入库服务接受“这份译稿对应哪个 content_hash”（待和入库服务商量）。
- 下载译稿：按文档取用 `GET /api/v1/datasets/<库>/documents/<文档>/translation`（见下）；按 `result_id` 取的老接口也还在：网页登录态 `GET /v1/document/enrichment/<result_id>?role=pdf`，SDK `GET /api/v1/datasets/<kb>/documents/<doc>/enrichments/<result_id>?role=pdf`。
- 配置不全或 `TRANSLATE_SINCE` 不是 ISO 时间时，守护进程只空等（不然 `entrypoint.sh` 每秒重启它），`--once`、`--dry-run` 直接退出、返回 1。
- 试跑：`TRANSLATE_KB_IDS=<库> TRANSLATE_SINCE=<时间> RETAINPDF_URL=<地址> RETAINPDF_MODEL_API_KEY=<key> python rag/svr/translate_worker.py --dry-run`（或 `--once`）。时间要写成带 `T` 和时区的样子。

## 附加内容接口

“当前的”附加内容：`document_enrichment` 里这份文档这一类的槽位是 `succeeded` 且有 `current_result_id`（同 `DocumentEnrichmentService.get_current_result`）。原文件内容变了槽位会被标过期、清掉当前结果，所以过期的摘要不会露出来。

**`POST /api/v1/reports/enrichments`**（API key，研报类前端的后端带着它调）

```json
{"documents": [{"dataset_id": "...", "document_id": "..."}], "types": ["summary", "meeting_minutes", "translation"]}
```

- `documents` 1～100 个，`types` 可省（默认三种都要）。返回 `{"documents": [...]}`，和请求一一对应、顺序相同。
- 每项：`summary` 是 `{text, result_id, generated_at}`；`meeting_minutes` 是 `{markdown, summary, meeting_type, meeting_date, result_id, generated_at}`；`translation` 是 `{url, result_id, generated_at}`，`url` 指向下面的译稿接口。没有的、或这个 key 读不到的（数据集没加入、文档不在这个数据集里）都是 `null`，不区分是哪种。
- 摘要和纪要正文从对象存储读（8 个并发），一份读坏了只那一份是 `null`。

**`GET /api/v1/datasets/<库>/documents/<文档>/translation`**（API key）：当前译稿 PDF，`inline`。没有就是 HTTP 404、出错是 500——研报类前端的代理只要 200 就把正文当文件往浏览器流，所以错误不能是 200。

**`GET /api/v1/chunk/pdf-preview/translation`、`/translation/file`**（免登录）：参数和校验同 `/chunk/pdf-preview`（`dataset_id`、`document_id`、`chunk_id`，chunk 必须属于这份文档），前者回 `{"available": true/false}`，后者给译稿 PDF。能看原文的人才能看译文，不多露。

预览页（`/preview/pdf`）有译文时右上角出现“原文 / 译文”切换。RetainPDF 的译稿保留原版面（页数、页面大小一样），所以证据高亮照原文坐标画在译文上也落在同一段；切换后定位回当前那处高亮。

## 测试

```bash
.venv/bin/python -m pytest ragflow/tests
```

测试把 RAGFlow 的模块换成替身，不连数据库、不调接口；不放进主测试套件，因为替身会替换全局模块（比如 `requests`）。挑文档的查询要在 RAGFlow 环境里用 `--dry-run` 对真实数据库试。

## 历史

这一模块在内部环境里的部署经过（上线时间、部署命令、知识库配置等）都涉及内部基础设施，已从本文件移除，处理清单见仓库根的 SANITIZATION.md。代码本身的历史在 git 里（本仓库导出时不含 .git）。
