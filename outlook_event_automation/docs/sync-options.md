# 可选：Google Calendar 与 Outlook Calendar 同步

同步不是当前主流程。

当前主流程是：

`邮件源 -> 本服务读取邮件 -> AI 抽取活动 -> 写入目标日历`

只有当你还希望 Outlook Calendar 也显示这些活动时，才需要同步层。

## 推荐开源工具

优先看：

- `phw198/OutlookGoogleCalendarSync`

它是专门做 Outlook / Google Calendar 同步的开源项目，支持双向同步。这个领域不要由本服务手写实现，因为删除、更新、重复事件、循环同步、时区和会议邀请状态都很容易出错。

## 低复杂度替代

如果只需要在 Outlook 里“看见”Google Calendar，可以用 Outlook 订阅 Google Calendar 的公开/私有 ICS 链接。但这种方式通常刷新慢，且多为只读，不适合低延迟和双向同步。

## 当前建议

第一阶段不要接同步层，先验收：

- 邮件源能读到目标邮件。
- AI 能稳定抽取活动。
- 目标日历能创建事件。
- systemd 日志能看到每次处理记录。
- SQLite 去重避免重复建事件。

这个闭环稳定后，再决定是否接 OGCS。
