# Outlook Event Automation 命令手册

这里是实际运行服务的 Python 目录。更完整的项目说明见仓库根目录 `README.md`，GitHub Pages 主页见 `../docs/index.html`。

## 一次性配置

```bash
cp config.example.json config.local.json
cp .env.example .env
```

编辑 `config.local.json`：

- `source`: 选择 `outlook` 或 `gmail`
- `calendar.sink`: 选择 `outlook`、`google` 或 `none`
- `extraction.openai_model`: Responses API 模型名
- `extraction.batch_size`: 默认每批 20 封
- `calendar.include_source_email_body`: 是否把原始邮件正文附在日历事件里

编辑 `.env`：

```text
OPENAI_API_KEY=replace-with-openai-compatible-api-key
MICROSOFT_CLIENT_SECRET=replace-with-client-secret
MICROSOFT_USER_ID=replace-with-mailbox-upn
```

## 授权

Microsoft 设备码授权：

```bash
python3 event_agent.py --config config.local.json auth-microsoft
```

Microsoft 本地回调授权：

```bash
python3 event_agent.py --config config.local.json auth-microsoft-web
```

Google OAuth 授权：

```bash
python3 event_agent.py --config config.local.json auth-google
```

## 扫描与写入

不写日历，只看提取结果：

```bash
python3 event_agent.py --config config.local.json run \
  --source outlook --sink none --limit 20 --force
```

写入 Outlook Calendar：

```bash
python3 event_agent.py --config config.local.json run \
  --source outlook --sink outlook --limit 20 --write
```

写入 Google Calendar：

```bash
python3 event_agent.py --config config.local.json run \
  --source gmail --sink google --limit 20 --write
```

常驻运行：

```bash
python3 event_agent.py --config config.local.json serve --write
```

## 状态说明

- `ignored`: 明确不是活动，或命中噪声规则，例如 `Daily Event Alert`。
- `needs_review`: 像活动但不够确定，例如取消、撤回、多活动、缺时间。
- `dry_run`: 测试模式下的可写候选。
- `created`: 已写入目标日历。

## systemd

```bash
sudo APP_DIR=/opt/outlook-event-agent bash scripts/install-systemd.sh
sudo systemctl start outlook-event-agent
journalctl -u outlook-event-agent -f
```

生产环境请确认 `.env`、`config.local.json`、OAuth token、SQLite 数据库和运行日志都只保存在服务器本地，不提交到 Git。
