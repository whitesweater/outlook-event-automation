# 私人 Outlook 从头配置

目标：

`私人 Outlook mailbox -> 本服务读取 /me/messages -> AI 抽取活动 -> 写入日历`

这条路线不依赖学校管理员审批。不要复用学校 quickstart 创建的单租户 app。

## 1. 新建 Microsoft App Registration

进入 Microsoft Entra admin center：

`App registrations -> New registration`

填写：

- Name: `outlook-event-agent-personal`
- Supported account types:
  - 选择 `Accounts in any organizational directory and personal Microsoft accounts`
  - 中文界面通常类似：`任何组织目录中的帐户和个人 Microsoft 帐户`
- Redirect URI:
  - Platform: `Web`
  - URI: `http://localhost:5000/getAToken`

创建后复制：

- `Application (client) ID`

## 2. 创建 Client Secret

进入：

`Certificates & secrets -> Client secrets -> New client secret`

复制 **Value**，不是 Secret ID。

注意：Value 只显示一次。

## 3. 添加 Graph Delegated Permissions

进入：

`API permissions -> Add a permission -> Microsoft Graph -> Delegated permissions`

添加：

- `Mail.Read`
- `offline_access`
- `User.Read`

不要添加 Application permissions。私人 Outlook delegated flow 不需要学校管理员同意。

## 4. 写入服务器配置

服务器上的部署路径示例：

`/opt/outlook-event-agent`

`config.local.json` 里的 Microsoft 配置应为：

```json
"microsoft": {
  "tenant": "consumers",
  "auth_mode": "auth_code",
  "client_id": "NEW_APPLICATION_CLIENT_ID",
  "redirect_port": 5000,
  "redirect_path": "/getAToken",
  "redirect_uri": "http://localhost:5000/getAToken",
  "scopes": [
    "offline_access",
    "Mail.Read",
    "User.Read"
  ]
}
```

`.env` 里写：

```bash
MICROSOFT_CLIENT_SECRET=NEW_CLIENT_SECRET_VALUE
```

## 5. 授权私人 Outlook

如果使用本地回调授权，在本机开 SSH tunnel：

```bash
ssh -L 5000:127.0.0.1:5000 your-server
```

服务器上运行：

```bash
cd /opt/outlook-event-agent
sudo -u outlook-agent PYTHONUNBUFFERED=1 python3 event_agent.py --config config.local.json auth-microsoft-web
```

打开命令输出的 Microsoft 登录链接，选择私人 Outlook / Microsoft 账号。

## 6. 测试读取

```bash
sudo -u outlook-agent python3 event_agent.py --config config.local.json run --source outlook --sink none --limit 5 --force
```

如果能看到邮件标题，私人 Outlook 读取链路就通了。

## 常见错误

### AADSTS16000 / live.com user does not exist in tenant

现象：

`User account from identity provider 'live.com' does not exist in tenant ...`

原因：

应用仍然是组织租户账号应用，不允许个人 Microsoft / Outlook.com 账号登录。

修法二选一：

- 重新创建 App Registration，并在 Supported account types 选择：
  `Accounts in any organizational directory and personal Microsoft accounts`
- 或进入现有 app 的 `Manifest`，确认：

```json
"signInAudience": "AzureADandPersonalMicrosoftAccount"
```

然后保存。还要确认 redirect URI 仍然是：

```text
http://localhost:5000/getAToken
```
