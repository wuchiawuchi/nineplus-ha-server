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

if [[ "${NINEPLUS_RECONFIGURE:-0}" != "1" && ( -f .env || -f config.json ) ]]; then
  say "检测到旧配置，将保留原文件。如需重新输入账号密码，请用 NINEPLUS_RECONFIGURE=1 重跑。"
else
  ADMIN_PASSWORD="$(secret 'NinePlus 后台管理员密码')"
  [[ ${#ADMIN_PASSWORD} -ge 8 ]] || die "管理员密码至少需要 8 位。"
  BEARER="$(openssl rand -hex 32)"
  ADMIN_PASSWORD_B64="$(printf '%s' "$ADMIN_PASSWORD" | base64 | tr -d '\r\n')"

  umask 077
  printf 'NINEPLUS_BACKEND=direct\nNINEPLUS_BEARER_TOKEN=%s\nNINEPLUS_ADMIN_PASSWORD_B64=%s\n' \
    "$BEARER" "$ADMIN_PASSWORD_B64" > .env
  printf '{"vehicles": []}\n' > config.json
fi

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
    printf '后台管理地址：http://%s:19009/admin\niPhone 服务器地址：http://%s:19009\nBearer Token：%s\n' \
      "$LOCAL_IP" "$LOCAL_IP" "$NINEPLUS_BEARER_TOKEN"
    say "请先打开后台页面，新增 NineBot+ 账号及对应的九号出行账号。"
    exit 0
  fi
  sleep 2
done

docker compose logs --tail=80 nineplus
die "服务未通过健康检查，请查看上面的日志。"
