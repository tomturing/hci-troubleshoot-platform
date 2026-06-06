# Checklist for fixing Prompt Audit Log SQLAlchemy Compile Conflict

- `[x] ` 下沉 `SystemPrompt` 到 `shared/models/system_prompt.py`
- `[x] ` 修改 `shared/models/audit.py` 导入 `SystemPrompt`
- `[x] ` 删除 `conversation-service` 下的 `app/models/system_prompt.py`
- `[x] ` 修改 `conversation-service` 下的 `app/models/__init__.py`
- `[x] ` 运行本地单元测试以确保修改没有带来破坏
- `[x] ` 运行 `make lint` 确保格式和质量合规
