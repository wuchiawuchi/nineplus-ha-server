#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${NINEPLUS_DIR:-$PWD/nineplus-ha-server}"
REPO_RAW="https://raw.githubusercontent.com/wuchiawuchi/nineplus-ha-server/main"

say() { printf '\n%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
prompt() {
  local label="$1" default="${2:-}" answer
  if [[ -n "$default" ]]; then
    read -r -p "$label [$default]: " answer </dev/tty
  else
    read -r -p "$label: " answer </dev/tty
  fi
  printf '%s' "${answer:-$default}"
}
secret() {
  local label="$1" answer
  read -r -s -p "$label: " answer </dev/tty
  printf '\n' >&2
  printf '%s' "$answer"
}
command -v docker >/dev/null 2>&1 || die "没有找到 Docker，请先安装 Docker Desktop 或 Docker Engine。"
docker compose version >/dev/null 2>&1 || die "Docker Compose 不可用，请升级 Docker。"
command -v curl >/dev/null 2>&1 || die "没有找到 curl。"
command -v openssl >/dev/null 2>&1 || die "没有找到 openssl。"
[[ -r /dev/tty ]] || die "当前没有交互终端。请登录 SSH 后直接运行脚本，不要通过后台任务执行。"

mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

say "下载部署文件到 $PROJECT_DIR"
curl -fsSL "$REPO_RAW/compose.yaml" -o compose.yaml

if [[ -f .env || -f config.json ]]; then
  say "检测到旧配置，将保留原文件。删除 .env/config.json 后重跑可重新配置。"
else
  NINEBOT_USERNAME_VALUE="$(prompt '九号出行账号')"
  [[ -n "$NINEBOT_USERNAME_VALUE" ]] || die "九号账号不能为空。"
  NINEBOT_PASSWORD_VALUE="$(secret '九号出行密码')"
  [[ -n "$NINEBOT_PASSWORD_VALUE" ]] || die "九号密码不能为空。"
  APP_ACCOUNT="$(prompt 'NineBot+ 登录账号' 'homeassistant')"
  APP_PASSWORD="$(secret 'NineBot+ 登录密码')"
  [[ -n "$APP_PASSWORD" ]] || die "客户端密码不能为空。"
  BEARER="$(openssl rand -hex 32)"

  umask 077
  printf 'NINEPLUS_BACKEND=direct\nNINEBOT_USERNAME=%s\nNINEBOT_PASSWORD=%s\nNINEPLUS_BEARER_TOKEN=%s\nNINEPLUS_ACCOUNT=%s\nNINEPLUS_PASSWORD=%s\n' \
    "$NINEBOT_USERNAME_VALUE" "$NINEBOT_PASSWORD_VALUE" "$BEARER" "$APP_ACCOUNT" "$APP_PASSWORD" > .env
  printf '{"vehicles": []}\n' > config.json
fi

mkdir -p ninebot-data
say "拉取并启动 NinePlus 九号直连服务"
docker compose pull
docker compose up -d

say "等待服务就绪"
for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:19009/healthz >/dev/null 2>&1; then
    LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    [[ -n "$LOCAL_IP" ]] || LOCAL_IP="这台电脑的局域网IP"
    # shellcheck disable=SC1091
    source .env
    say "部署成功。"
    printf 'iPhone 服务器地址：http://%s:19009\n登录账号：%s\n登录密码：你刚设置的 NineBot+ 密码\nBearer Token：%s\n' \
      "$LOCAL_IP" "$NINEPLUS_ACCOUNT" "$NINEPLUS_BEARER_TOKEN"
    exit 0
  fi
  sleep 2
done

docker compose logs --tail=80 nineplus
die "服务未通过健康检查，请查看上面的日志。"
