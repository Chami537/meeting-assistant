# 自建语音识别服务

部署在一台 A6000 服务器上的语音识别服务，默认用 **Qwen3-ASR-0.6B**，也可以换成 Fun-ASR-Nano，离线模式还能用内置的合成替身引擎（见文末）。接口和一个 faster-whisper 服务完全一样，后端只要改配置就能切换：

```
ASR_PROVIDER=qwen
ASR_QWEN_URL=http://asr-host:8020
```

三个模型（Whisper、Qwen3-ASR、Fun-ASR）的对比实验记录不在本仓库。

## 结构：两个容器

```
后端 ──任务接口──▶ asr-service（端口 8020，本目录的代码）
                     切分、说话人、数字转换、排队、重试
                     │ 每块音频一个 HTTP 请求，同时发 8 个
                     ▼
                  qwen3-asr（只在内部网络上，不对外开放端口）
                     Qwen 官方镜像的 qwen-asr-serve（vLLM），预留 10% 显存
```

显存合计约 7GB：Qwen 实测 6.2GB，切分和说话人模型不到 1GB。对齐模型（最多 2.5GB）只在要逐句时间的任务里临时加载，用完就释放。

## 识别流程（Qwen，默认）

```
上传的音频（m4a 等）
  → ffmpeg 转成 16kHz 单声道
  → fsmn-vad 按停顿切分；超过 25 秒的长段，在它最后 5 秒里音量最低的地方再切开（chunks.py）
  → cam++ 批量提取说话人特征，聚类，按出场顺序标成"发言人1、发言人2……"
  → 同一说话人的相邻片段合成块，每块最长 25 秒（chunks.py）
  → 每块转成 WAV 发给 Qwen，同时发 8 个，去掉 "language Chinese<asr_text>" 前缀（qwen.py）
  → 汉字数字转阿拉伯数字（itn.py），计算重复度
  → 返回分段：每段是一个说话人的一次发言，带起止时间
```

**为什么要自己切块**：Qwen 自带的服务对长音频每 30 秒硬切一刀，会把数字切断。一场业绩会上"3529 万"被切成"三千五百二。十九万"，纪要每次都写成错的数。这里的切点要么是说话停顿，要么是长段末尾 5 秒里最安静的地方；每块都不超过 25 秒，所以 Qwen 不会再切。

切分模型 fsmn-vad 自己也有一个"单段最长"的上限，到点就切，不管是不是在说话中间。之前设成 28 秒时，一场 43 分钟会议里有 11 刀切在了说话中间。所以现在把它放宽到 60 秒（很少碰到），25 秒以上的长段改由我们自己找安静的地方切。

**为什么要数字转换**：Qwen 和 Fun-ASR 都会把数字写成汉字（"幺零零九""百分之八十九"），不转换的话，纪要里的产品编号会写错。转换规则是"拿不准就保留原文"，约数、成语、"这一块"这类都不转。

**说话人标签用"发言人3"，不用纯数字"3"**：纪要提示词里每一行的格式是"00:30:13 3: 进口占65%以上"，会议里到处是数字，纯数字标签会和正文混在一起。同一份逐字稿只改标签写法，纪要的关键事实平均从 31.8 分升到 37.2 分（满分 41）。

**会议标题作为上下文**：后端把会议标题（上传的文件名去掉扩展名）放在 `prompt` 里传过来，服务随每一段音频一起交给 Qwen。公司名、产品名这类专有名词因此能识别对。一场业绩会上：不带标题时，公司名一次也没识别出来（写成读音相近的别的字），纪要 7 次里有 6 次把公司名写错；带上标题后，公司名识别出 7 次，纪要 7 次都写对，平均分从 32.7 升到 34.0，识别耗时不变。所以服务调用 Qwen 的对话接口（`/v1/chat/completions`，system 消息放标题，user 消息放 base64 编码的音频），而不是转写接口：转写接口会直接丢掉 prompt。

**逐句时间（可选，`sentences=1`）**：识别出的每块文字（数字转换之前的原文），再交给 Qwen 官方配套的对齐模型 Qwen3-ForcedAligner-0.6B，得到每个字的起止时间（连续的字母、数字算一个单位，标点不算）。按句号、问号、叹号、分号切成句子，每句的时间取第一个字的开始和最后一个字的结束，然后每句单独做数字转换（sentences.py）。这时每个分段会多一个 `sentences: [{start, end, text}]`，给会议页"点一句、从这句开始播放"用。一场 43 分钟的会：188 段切成 466 句，每句中位 4.3 秒；按对齐出的时间把句子切出来再识别一遍，87% 的句首、88% 的句尾都对得上（按字数平均分配只有 65%、47%）。代价是显存最多多 2.5GB，43 分钟多花约 44 秒，所以只有要的请求才做，批量导入和后端都不要。显卡通常是多人共用的，所以对齐模型不常驻：要逐句时间的任务开始对齐时才加载（约 5 秒），加载前先看空闲显存，不到 3GB 就跳过（这个任务的分段不带句子，调用方用整段的时间），用完马上释放。对齐模型放在 Docker 卷 `qwen3-aligner` 里（服务器根分区快满时不要把大模型放系统盘），卷里没有模型或者加载失败时，服务照常运行，只是不出句子时间。

**插队（`priority=1`）**：排队的任务按先来后到处理，带 `priority` 的排到所有普通任务前面。会议页上有人在等结果（网页后端最多等 35 分钟），而 RAGFlow 批量导入时一次能排二十多个录音。

另一个引擎 Fun-ASR（`ASR_ENGINE=funasr`）逐段识别，可以开多个工作进程并行（`ASR_WORKERS`，每份约 4.1GB）。单份时 43 分钟的录音要约 6 分钟。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health/ready` | `{status, model, device, queue_available, sentences}`，status 为 ready 才接任务；`sentences` 表示对齐模型是否已加载 |
| POST | `/v1/audio/transcriptions/jobs` | 表单上传 `file`。Qwen 用 `prompt` 作上下文（后端和 RAGFlow 只传会议标题，最多保留 200 字；服务自己在后面加上术语表里标了“识别上下文”的词，总长最多 500 字）；Fun-ASR 用 `language`（zh/en/ja）和 `hotwords`（逗号分隔）；`sentences=1` 要逐句时间，`priority=1` 插队。返回 202 `{id, status: queued}` |
| GET | `/v1/audio/transcriptions/jobs/{id}` | `{status: queued｜running｜completed｜failed, segments, error}` |
| DELETE | `/v1/audio/transcriptions/jobs/{id}` | 删除任务和识别结果 |

每个分段的格式：`{start, end, text, speaker, compression_ratio}`，其中 `start` 和 `end` 的单位是秒。要了逐句时间的任务，每个分段还有 `sentences: [{start, end, text}]`（秒，录音里的绝对时间）；某一块对齐失败时它是空列表，调用方用整段的时间。

几种特殊情况：
- **返回 429**（带 `Retry-After: 30`）：队列满。后端和 RAGFlow 都会等一会儿再交。RAGFlow 的客户端只对 429 重试，回 503 它就直接让文档解析失败。
- **返回 503**：模型还在加载，或者 Qwen 服务连不上（这时健康检查显示 `upstream unavailable`）。后端会当作"忙"，稍后再试，不算失败；RAGFlow 会直接失败。
- **返回 404**：任务不存在（比如服务重启过）。后端会重新提交。

服务一次只处理一个任务，其余的排队，队列默认最多 32 个（RAGFlow 4 个 task_executor × 每个 6 个 chunk builder，最多 24 个录音同时在等识别，再给后端留几个）。任务信息只保存在内存里：音频在识别完成后马上删除；识别结果在后端取走并删除前一直保留，最多保留 24 小时。

显卡是多人共用的，所以服务会自己处理偶发问题：
- **模型加载失败，或者 Qwen 服务还没起来**：每 30 秒重试一次，期间健康检查显示 `failed`。
- **Qwen 服务连不上或暂时忙不过来**（连接失败、502/503/504）：已经在排队的任务每 30 秒再试一次，一直等到 Qwen 恢复，**不算失败次数**。任务一旦标记为失败，后端就会让整场会议失败，所以不能因为 Qwen 停了一会儿就判死。
- **识别过程中出现其他意外错误**（比如显存不够、Qwen 返回 500）：依次在 30 秒、2 分钟、8 分钟后重试，3 次都失败才把任务标记为失败。
- **日志**：只记录错误类型和代码位置，不记录错误消息，因为消息里可能带着会议内容。

服务**没有鉴权**，只能在内部网络使用。

## 部署

模型需要预先下载好：Qwen 在 `~/models/Qwen3-ASR-0.6B`，切分、说话人和 Fun-ASR 模型在 `~/models/modelscope`（路径按你的机器改）。

```bash
# 在仓库根目录下：把代码传到服务器（术语表只有一份，在后端目录里，构建时复制进镜像，所以要一起传）
tar --exclude=__pycache__ -cf - asr_service backend/app/postprocess/glossary.csv | ssh user@asr-host 'tar -xf - -C ~/asr-service-src'

# 在服务器上：构建并启动两个容器（Qwen 健康检查通过后，asr-service 才会启动）
cd ~/asr-service-src && docker compose -f asr_service/docker-compose.yml up -d --build

docker compose -f asr_service/docker-compose.yml ps
docker logs -f asr-service              # Qwen 就绪后才会变成 ready，第一次启动约 2～3 分钟
curl http://localhost:8020/health/ready  # model 应该是 qwen3-asr-0.6b
```

后端切换后，可以运行 `python -m app.tools.smoke` 检查。如果后端配置的是 qwen，但 8020 端口上实际跑的不是 Qwen（比如部署只做了一半），它会报出来。

**更新代码**：传代码后重新执行 `up -d --build`。注意：正在排队或识别中的任务会丢失，后端会自动重新提交。

可以调整的环境变量（写在 docker-compose.yml 的 `environment` 里）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASR_ENGINE` | qwen | `qwen`、`funasr` 或 `fake`（离线替身，见下） |
| `ASR_QWEN_URL` | http://qwen3-asr:80 | Qwen 服务的地址 |
| `ASR_QWEN_CONCURRENCY` | 8 | 同时发给 Qwen 的请求数 |
| `ASR_WORKERS` | 1 | 仅 Fun-ASR：同时识别的模型份数，每份约 4.1GB 显存 |
| `ASR_SPK_DIR` | cam++ 模型目录 | 设为空字符串就关闭说话人区分 |
| `ASR_ALIGNER_DIR` | /models/aligner/Qwen3-ForcedAligner-0.6B | 对齐模型目录，不存在就不出句子时间 |
| `ASR_QUEUE` | 32 | 排队任务数上限（排着的音频存在容器里，单个最大 1GB） |
| `ASR_KEEP_HOURS` | 24 | 识别结果最多保留多久（小时） |
| `ASR_MAX_MB` | 1024 | 上传文件大小上限（MB） |
| `ASR_DEVICE` | cuda:0 | 使用哪块显卡 |
| `ASR_FAKE_DELAY` | 0 | 仅 fake 引擎：每个任务假装识别这么久（秒），用于手动观察排队和 429 |

Qwen 的显存在 `qwen3-asr` 的启动参数 `--gpu-memory-utilization 0.1` 里设置。

## 离线替身（ASR_ENGINE=fake）

没有显卡、没有模型时，用合成引擎跑同一套任务接口：音频只读时长（WAV 头或 ffprobe），按固定剧本生成分段（两位发言人、带数字和计划/风险句、逐句时间），数字照走真的转换代码。它不识别任何内容，只是让整条链路（上传 → 排队 → 纪要）离线跑通，也用来手动看队列排满后返回 429。直接起：

```bash
ASR_ENGINE=fake uvicorn asr_service.api:app --host 0.0.0.0 --port 8020
```

## 测试

本地测试不需要显卡，识别引擎用假对象代替：

```bash
.venv/bin/python -m pytest tests/unit/test_asr_service_*.py
```

在服务器上直接对一个文件跑一遍，看每一步的耗时（要接在 compose 的网络上，才能连到 Qwen）：

```bash
cd ~/asr-service-src && docker compose -f asr_service/docker-compose.yml run --rm -v $HOME/asr-test:/data \
  asr-service python3 -m asr_service.engine /data/会议.flac /data/out.json
```
