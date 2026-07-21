# NinePlus Home Assistant Server

把 Home Assistant 中的九号车辆实体转换为 NineBot+ iOS 客户端使用的 HTTP API。

## 前提

1. Home Assistant 已能看到九号车辆实体。这个项目不会绕过九号登录，也不能修复已经失效的九号 HA 组件。
2. 在 HA 个人资料页创建“长期访问令牌”。
3. 运行服务的机器可以访问 HA 的 `8123` 端口。

## 配置实体

在 HA 的“开发者工具 → 状态”中搜索 `ninebot`，记下实际实体 ID。复制配置模板：

```bash
cp config.example.json config.json
cp .env.example .env
```

编辑 `config.json`，把示例实体 ID 替换为 HA 中真实存在的 ID。不存在的字段可以删除。实体也可读取 attribute：

```json
{"entity_id": "device_tracker.ninebot", "attribute": "latitude"}
```

如果组件没有提供控制服务，请删除 `services` 中对应项目。服务端会返回 501，而不会假装控制成功。

## 本地运行

```bash
export HA_URL=http://192.168.1.10:8123
export HA_TOKEN=你的HA长期访问令牌
export NINEPLUS_BEARER_TOKEN=随机长字符串
export NINEPLUS_ACCOUNT=homeassistant
export NINEPLUS_PASSWORD=客户端登录密码
export NINEPLUS_CONFIG=$PWD/config.json
python3 server.py
```

测试：

```bash
curl http://127.0.0.1:19009/healthz
curl -H "Authorization: Bearer 随机长字符串" http://127.0.0.1:19009/vehicles
```

## Docker Compose

仓库中的 `compose.yaml` 已配置为拉取本项目发布的镜像：

```bash
docker compose up -d
docker compose logs -f nineplus-ha
```

如果服务与 Home Assistant 运行在同一台 Linux 主机且 `homeassistant.local` 无法解析，把 `HA_URL` 改为 HA 的局域网 IP。

## GitHub Actions

将整个目录推送到 GitHub，工作流会：

1. 运行 Python 单元测试。
2. 构建 `linux/amd64` 和 `linux/arm64` 镜像。
3. 推送到 `ghcr.io/<用户名>/nineplus-ha-server:latest`。

首次发布后，到仓库的 **Packages → Package settings** 调整镜像可见性。无需把 HA Token 放进 GitHub Secrets；Token 只在实际部署服务器的 `.env` 中保存。

## iPhone 设置

在 NineBot+ 设置中填写：

- 服务器地址：`http://运行本服务的IP:19009`
- App Bearer Token：`.env` 中的 `NINEPLUS_BEARER_TOKEN`
- 账号与密码：`.env` 中的 `NINEPLUS_ACCOUNT` 和 `NINEPLUS_PASSWORD`

不要把未加密的 `19009` 端口直接暴露到公网。远程使用建议通过 Tailscale/WireGuard，或在前面增加 HTTPS 反向代理。

## 已实现接口

- 健康检查、客户端登录
- 车辆列表、仪表盘、状态、电池
- 空行程列表与基础预测兼容响应
- HA 服务映射：响铃、开座桶、上电、熄火
- 推送注册兼容响应（本适配器不发送 APNs）

历史行程、预测模型和 APNs 需要额外数据库/推送基础设施，目前不包含。
