# AI 会议助手 V1.0 技术方案交付包

- `AI_会议助手_全链路开发方案_V1.0.md`：完整技术调研、平台权限、接口设计、数据流、开发步骤、风险和参考来源。
- `schema.sql`：PostgreSQL 16 数据模型初稿（供 V1 开发参考，生产应使用 Alembic migrations）。
- `docker-compose.yml`：本地 PostgreSQL + Redis，端口只绑定到 `127.0.0.1`。
- `.env.example`：环境变量模板，不含真实密钥。

**不包含**完整 FastAPI 服务或完整 Windows 腾讯会议参会 Worker；目前是技术方案/起步基础设施，不是可直接上线的完成品。

本地启动与 SQL 初始化请看主文档第 9 章。外部账号认证、录音授权和 Windows 参会可行性尚未完成真实环境验收。
