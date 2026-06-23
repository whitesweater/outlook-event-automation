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
NOTIFY_WEBHOOK_URL=
NOTIFY_WEBHOOK_TOKEN=
HERMES_WEBHOOK_URL=
HERMES_WEBHOOK_SECRET=
OUTLOOK_AGENT_API_TOKEN=
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

## Hermes / LightVela 集成

开启 `config.local.json`：

```json
{
  "notifications": {
    "enabled": true,
    "provider": "webhook",
    "notify_target": "hermes-webhook",
    "hermes_webhook_url_env": "HERMES_WEBHOOK_URL",
    "hermes_webhook_secret_env": "HERMES_WEBHOOK_SECRET",
    "daily_digest_hours": 24,
    "fault_cooldown_minutes": 30
  }
}
```

在 `.env` 中填入自托管 Hermes route：

```text
HERMES_WEBHOOK_URL=https://your-hermes.example/webhooks/outlook-event-agent
HERMES_WEBHOOK_SECRET=replace-with-route-secret
```

预览日报：

```bash
python3 event_agent.py --config config.local.json notify-digest --hours 24 --dry-run
```

发送日报：

```bash
python3 event_agent.py --config config.local.json notify-digest --hours 24
```

健康报告：

```bash
python3 event_agent.py --config config.local.json health-report --dry-run --always
```

常驻服务出现异常时会自动发送 `fault` payload，并用 `fault_cooldown_minutes` 避免刷屏。

LightVela 或其他 agent 可以通过轻量 HTTP API 查询摘要和运行状态：

```bash
python3 event_agent.py --config config.local.json api-server
curl -H "Authorization: Bearer $OUTLOOK_AGENT_API_TOKEN" \
  http://127.0.0.1:8791/digest?hours=24
curl -H "Authorization: Bearer $OUTLOOK_AGENT_API_TOKEN" \
  'http://127.0.0.1:8791/agenda?date=today&limit=50'
curl -H "Authorization: Bearer $OUTLOOK_AGENT_API_TOKEN" \
  'http://127.0.0.1:8791/agenda-range?date=today&days=7&limit=100'
```

日程查询走 Microsoft Graph Calendar，需要 Microsoft delegated scopes 至少包含
`Calendars.Read` 或 `Calendars.ReadWrite`。如果要让 Hermes 每天固定推送当天日程，
推荐使用 Hermes Cron 的 `--no-agent --script` 模式：脚本调用 `/agenda`，
Hermes 负责定时和投递。如果要让 Hermes Agent 回答“最近三天”或“最近一周”的
问题，给 Hermes 配置 `outlook-mail-events` skill，并让 skill 调用 `/agenda-range`。

更多说明：

- `integrations/hermes.md`：自托管 Hermes 的 provider、webhook、skill、Cron、排障和维护手册
- `integrations/lightvela.md`

## systemd

```bash
sudo APP_DIR=/opt/outlook-event-agent bash scripts/install-systemd.sh
sudo systemctl start outlook-event-agent
journalctl -u outlook-event-agent -f
```

每日 08:30 推送日报：

```bash
sudo systemctl enable --now outlook-event-agent-digest.timer
systemctl list-timers outlook-event-agent-digest.timer
```

生产环境请确认 `.env`、`config.local.json`、OAuth token、SQLite 数据库和运行日志都只保存在服务器本地，不提交到 Git。
