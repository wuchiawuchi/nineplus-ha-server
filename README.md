# NinePlus Home Assistant Server

把 Home Assistant 中的九号车辆实体转换为 NineBot+ iOS 客户端使用的 HTTP API。

## 一键部署

在已经安装 Docker 的 Linux、NAS 或 macOS 终端执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/wuchiawuchi/nineplus-ha-server/main/install.sh)
```

脚本会询问 HA 地址、长期访问令牌、车辆 SN 和客户端密码，自动生成随机 Bearer Token、拉取镜像、启动容器并进行健康检查。默认安装到当前目录的 `nineplus-ha-server`；可用 `NINEPLUS_DIR=/指定目录` 修改位置。重复运行会保留现有 `.env` 和 `config.json`。

## 前提

1. Home Assistant 已能看到九号车辆实体。这个项目不会绕过九号登录，也不能修复已经失效的九号 HA 组件。
2. 在 HA 个人资料页创建“长期访问令牌”。
3. 运行服务的机器可以访问 HA 的 `8123` 端口。

## 安装 hasscc/ninebot

先在 Home Assistant 安装并成功登录 [hasscc/ninebot](https://github.com/hasscc/ninebot)。确认 HA 中已经出现电量、续航、定位、鸣笛、座桶和车锁等实体；适配器不保存九号账号密码，九号云端登录完全由该 HA 组件负责。

## 配置车辆

复制配置模板：

```bash
cp config.example.json config.json
cp .env.example .env
```

`hasscc/ninebot` 模式只需填写车辆 SN、显示名称和型号：

```json
{
  "vehicles": [{
    "sn": "你的车辆SN",
    "name": "我的九号",
    "model": "Ninebot",
    "integration": "hasscc/ninebot"
  }]
}
```

适配器会按照该组件源码自动读取 `ninebot.<sn>_battery`、`endurance`、`location`、`month_mileage`、`bms_voltage` 等实体，并自动映射控制：

- 寻车：`button.press` → `ninebot.<sn>_bell`
- 开座桶：`button.press` → `ninebot.<sn>_bucket`
- 启动：`lock.unlock` → `ninebot.<sn>_lock`
- 熄火：`lock.lock` → `ninebot.<sn>_lock`

实体 ID 曾被你在 HA 中手工改名时，可以在车辆下增加 `entities` 或 `services` 覆盖自动值；格式参见程序的通用配置兼容逻辑。实体暂时不可用不会拖垮整个仪表盘，相应字段会留空。

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
- 读取 hasscc/ninebot 的电量、续航、充电、上电、锁定、位置、月里程、最近骑行、电池电压/温度/循环数据
- HA 标准实体控制：响铃、开座桶、启动、熄火
- 推送注册兼容响应（本适配器不发送 APNs）

完整历史行程（HA 组件只暴露最近一次与月汇总）、预测模型和 APNs 需要额外数据库/推送基础设施，目前不包含。
