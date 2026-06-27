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
- `extraction.auto_ignore_keywords`: 自动忽略的噪声活动关键词，例如消防演练、发电机负载测试
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

在线常驻运行，默认每轮只读取最新 1 封邮件：

```bash
python3 event_agent.py --config config.local.json serve --write
```

离线补处理用于服务中断后回扫漏掉的邮件，可以一次读取多封，并保持每批 20 封交给模型：

```bash
python3 event_agent.py --config config.local.json run \
  --source outlook --sink outlook --limit 50 --batch-size 20 --write
```

## 状态说明

- `ignored`: 明确不是活动，或命中噪声规则，例如 `Daily Event Alert`。
- `needs_review`: 像活动但不够确定，例如取消、撤回、多活动、缺时间。
- `dry_run`: 测试模式下的可写候选。
- `created`: 已写入目标日历。

## 自动判断流程

常驻服务不是通过 Hermes skill 来自动读邮件和写日历；Hermes 主要负责微信/QQ 推送、日程查询和手动加日程。自动流程在 `event_agent.py` 里：

1. 读取 Outlook/Gmail 最近邮件。
2. 跳过本地 SQLite 中已经处理过的邮件。
3. 先跑固定规则过滤，例如 `Daily Event Alert` 和 `extraction.auto_ignore_keywords`。
4. 剩余邮件按批次交给 Responses API，要求模型输出严格 JSON。
5. Python 再检查置信度、开始/结束时间、取消/撤回、多活动等安全条件。
6. 只有 `is_event=true`、时间完整、置信度达标且不需要复核的事件才会写入日历。

`auto_ignore_keywords` 会在 AI 前后各检查一次：命中时记录为 `ignored`，不会写日历，也不会触发新事件微信提醒。适合放你不希望进入个人日历的运营类通知，例如消防演练、发电机负载测试。

在线和离线两种运行方式是分开的：`serve --write` 是常驻在线模式，推荐
`service.max_messages_per_poll=1`，来一封处理一封；`run --limit N --batch-size 20 --write`
是离线补处理模式，用于服务中断后回扫历史邮件。

当前生产实现仍是短间隔在线扫描，不是 Microsoft Graph webhook 事件驱动。Graph webhook 可以作为下一阶段优化，但需要公网 HTTPS 回调、订阅续期和失败重放机制。

## 自托管 Hermes 集成

开启 `config.local.json`：

```json
{
  "notifications": {
    "enabled": true,
    "provider": "webhook",
    "notify_target": "hermes-webhook",
    "hermes_webhook_url_env": "HERMES_WEBHOOK_URL",
    "hermes_webhook_secret_env": "HERMES_WEBHOOK_SECRET",
    "new_event_alerts": true,
    "new_event_alert_statuses": ["created", "needs_review"],
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

健康报告会检查最近一次运行时间、最近一次动作统计、AI 抽取失败数量，以及本服务主动发送 webhook 时记录到
`data/notification_state.json` 的最近通知失败。注意：如果日报是由 Hermes Cron 直接投递，投递错误会记录在 Hermes Cron 状态里，需要用 `hermes cron list/status` 查看。

常驻服务出现异常时会自动发送 `fault` payload，并用 `fault_cooldown_minutes` 避免刷屏。
常驻服务每次从新邮件里记录出新的 `created` 或 `needs_review` 事件时，也会发送实时提醒：
`created` 表示已写入目标日历，`needs_review` 表示像活动但需要人工确认。重复邮件、已处理邮件、
dry run 和 `Daily Event Alert` 不会触发实时提醒。可以用
`notifications.new_event_alert_statuses` 控制哪些状态需要推送。
如果微信通道短时间限流，优先在 Hermes `config.yaml` 配置发送重试，或者临时把 Hermes Cron 的 `--deliver`
切到已经绑定的 QQ 目标；不要修改 Hermes 源码。

Hermes 或其他 agent 可以通过轻量 HTTP API 查询摘要和运行状态：

```bash
python3 event_agent.py --config config.local.json api-server
curl -H "Authorization: Bearer $OUTLOOK_AGENT_API_TOKEN" \
  http://127.0.0.1:8791/digest?hours=24
curl -H "Authorization: Bearer $OUTLOOK_AGENT_API_TOKEN" \
  'http://127.0.0.1:8791/agenda?date=today&limit=50'
curl -H "Authorization: Bearer $OUTLOOK_AGENT_API_TOKEN" \
  'http://127.0.0.1:8791/agenda-range?date=today&days=7&limit=100'
```

用户确认后新增 Outlook 日程：

```bash
curl -X POST -H "Authorization: Bearer $OUTLOOK_AGENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8791/calendar-events \
  -d '{
    "confirmed": true,
    "title": "字节跳动面试",
    "start_time": "2026-06-25T11:00:00+08:00",
    "end_time": "2026-06-25T12:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "description": "通过 Hermes 手动添加。"
  }'
```

真实写入要求 `api.allow_write_actions=true`。可以先加 `"dry_run": true`
验证解析结果；dry run 不写日历，也不要求开启写入开关。

日程查询走 Microsoft Graph Calendar，需要 Microsoft delegated scopes 至少包含
`Calendars.Read` 或 `Calendars.ReadWrite`。如果要让 Hermes 每天固定推送当天日程，
推荐使用 Hermes Cron 的 `--no-agent --script` 模式：脚本调用 `/agenda`，
Hermes 负责定时和投递。如果要让 Hermes Agent 回答“最近三天”或“最近一周”的
问题，给 Hermes 配置 `outlook-mail-events` skill，并让 skill 调用 `/agenda-range`。
如果要支持“添加日程”，同一个 skill 可以在用户确认后调用 `/calendar-events`。

更多说明：

- `integrations/hermes.md`：自托管 Hermes 的 provider、webhook、skill、Cron、排障和维护手册
- `integrations/lightvela.md`

## systemd

```bash
sudo APP_DIR=/opt/outlook-event-agent bash scripts/install-systemd.sh
sudo systemctl start outlook-event-agent
journalctl -u outlook-event-agent -f
```

如果不用 Hermes Cron，也可以启用本项目自带的 systemd timer 做备用日报推送：

```bash
sudo systemctl enable --now outlook-event-agent-digest.timer
systemctl list-timers outlook-event-agent-digest.timer
```

当前推荐生产路径仍是 Hermes Cron：每天 10:00 执行 `daily-outlook-agenda.sh`，脚本调用
`outlook-mail-events agenda today 50`，Hermes 负责投递到微信或 QQ。完整配置和排障见 `integrations/hermes.md`。

生产环境请确认 `.env`、`config.local.json`、OAuth token、SQLite 数据库和运行日志都只保存在服务器本地，不提交到 Git。
