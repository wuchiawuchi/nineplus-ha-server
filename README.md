# NinePlus Server

为 NineBot+ iOS 客户端提供九号云端 API。服务直接使用与 `hasscc/ninebot` 相同的 `ninecli` 登录九号，不依赖 Home Assistant。

## 一键部署

在已经安装 Docker 的 Linux、NAS 或 macOS 终端执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/wuchiawuchi/nineplus-ha-server/main/install.sh)
```

脚本会生成随机 Bearer Token、拉取镜像并启动容器。第一次访问 `http://服务器IP:19009/admin` 时，页面会让你设置后台管理员密码；之后在后台新增 NineBot+ 账号及对应的九号出行账号。默认安装到当前目录的 `nineplus-ha-server`；可用 `NINEPLUS_DIR=/指定目录` 修改位置。

> 九号账号密码只传给你自己运行的容器，用于首次获取令牌，不会提交到 GitHub。九号云端接口可能随官方变更而失效。

## 前提（默认直连模式）

1. 安装 Docker Desktop 或 Docker Engine（带 Compose）。
2. 准备需要绑定的九号出行账号。
3. 运行服务器可以访问九号云端。

不需要安装 Home Assistant，不需要 HA Token，也不需要手工填写车辆 SN。

## 工作方式

容器内安装 `ninecli==0.1.7`，与 [hasscc/ninebot](https://github.com/hasscc/ninebot) 当前使用的版本相同。每个 NineBot+ 账号分配独立的九号令牌目录，车辆、行程和控制请求都按登录会话隔离。`accounts.json` 只保存 NineBot+ 密码的 PBKDF2 哈希和九号账号名；九号密码仅用于新增账号时换取令牌，不落盘。

- 自动发现车辆
- 车辆状态、电量、续航、位置
- 电池详情与骑行记录
- 寻车鸣笛、开座桶、启动和熄火

## 手工配置

```bash
export NINEPLUS_BACKEND=direct
export NINEPLUS_BEARER_TOKEN=随机长字符串
export NINEPLUS_ADMIN_PASSWORD=后台管理员密码
python3 server.py
```

测试：

```bash
curl http://127.0.0.1:19009/healthz
curl -H "Authorization: Bearer 随机长字符串" http://127.0.0.1:19009/vehicles
curl -H "Authorization: Bearer 随机长字符串" \
  http://127.0.0.1:19009/vehicles/车辆SN/travel/行程ID
```

行程详情接口调用 `ninecli travel SN --detail ID --json`，并原样返回九号云端的
`trail` 轨迹点（经纬度、速度及点间距离），供 NineBot+ 绘制地图路径和速度曲线。

## 后台增加账号

打开 `http://服务器IP:19009/admin`，用 `NINEPLUS_ADMIN_PASSWORD` 登录，然后填写：

- NineBot+ 登录账号和密码；
- 该用户对应的九号出行账号和密码。

后台会先验证九号登录并发现车辆，成功后才会创建账号。

## Docker Compose

仓库中的 `compose.yaml` 已配置为拉取本项目发布的镜像：

```bash
docker compose up -d
docker compose logs -f nineplus
```

## GitHub Actions

将整个目录推送到 GitHub，工作流会：

1. 运行 Python 单元测试。
2. 构建 `linux/amd64` 和 `linux/arm64` 镜像。
3. 推送到 `ghcr.io/<用户名>/nineplus-ha-server:latest`。

首次发布后，到仓库的 **Packages → Package settings** 调整镜像可见性。九号账号密码只放在实际部署服务器的 `.env` 中，不要添加到 GitHub Secrets。

## iPhone 设置

在 NineBot+ 设置中填写：

- 服务器地址：`http://运行本服务的IP:19009`
- App Bearer Token：`.env` 中的 `NINEPLUS_BEARER_TOKEN`
- 账号与密码：后台中新增的 NineBot+ 账号和密码

不要把未加密的 `19009` 端口直接暴露到公网。远程使用建议通过 Tailscale/WireGuard，或在前面增加 HTTPS 反向代理。

## 已实现接口

- 健康检查、客户端登录
- 车辆列表、仪表盘、状态、电池
- 直连九号云端读取状态、电池和行程
- 直连控制：响铃、开座桶、启动、熄火
- 推送注册兼容响应（本适配器不发送 APNs）

预测模型和 APNs 需要额外数据库/推送基础设施，目前不包含。
