#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${NINEPLUS_DIR:-$PWD/nineplus-ha-server}"
REPO_RAW="https://raw.githubusercontent.com/wuchiawuchi/nineplus-ha-server/main"

say() { printf '\n%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
prompt() {
  local label="$1" default="${2:-}" answer
  if [[ -n "$default" ]]; then read -r -p "$label [$default]: " answer; else read -r -p "$label: " answer; fi
  printf '%s' "${answer:-$default}"
}
secret() {
  local label="$1" answer
  read -r -s -p "$label: " answer
  printf '\n' >&2
  printf '%s' "$answer"
}
json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

command -v docker >/dev/null 2>&1 || die "没有找到 Docker，请先安装 Docker Desktop 或 Docker Engine。"
docker compose version >/dev/null 2>&1 || die "Docker Compose 不可用，请升级 Docker。"
command -v curl >/dev/null 2>&1 || die "没有找到 curl。"
command -v openssl >/dev/null 2>&1 || die "没有找到 openssl。"

mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

say "下载部署文件到 $PROJECT_DIR"
curl -fsSL "$REPO_RAW/compose.yaml" -o compose.yaml

if [[ -f .env || -f config.json ]]; then
  say "检测到旧配置，将保留原文件。删除 .env/config.json 后重跑可重新配置。"
else
  HA_URL_VALUE="$(prompt 'Home Assistant 地址' 'http://homeassistant.local:8123')"
  HA_TOKEN_VALUE="$(secret 'Home Assistant 长期访问令牌')"
  [[ -n "$HA_TOKEN_VALUE" ]] || die "HA Token 不能为空。"
  VEHICLE_SN="$(prompt '车辆 SN')"
  [[ -n "$VEHICLE_SN" ]] || die "车辆 SN 不能为空。"
  VEHICLE_NAME="$(prompt '车辆显示名称' '我的九号')"
  VEHICLE_MODEL="$(prompt '车辆型号' 'Ninebot')"
  APP_ACCOUNT="$(prompt 'NineBot+ 登录账号' 'homeassistant')"
  APP_PASSWORD="$(secret 'NineBot+ 登录密码')"
  [[ -n "$APP_PASSWORD" ]] || die "客户端密码不能为空。"
  BEARER="$(openssl rand -hex 32)"
  VEHICLE_SN_JSON="$(json_escape "$VEHICLE_SN")"
  VEHICLE_NAME_JSON="$(json_escape "$VEHICLE_NAME")"
  VEHICLE_MODEL_JSON="$(json_escape "$VEHICLE_MODEL")"

  umask 077
  printf 'HA_URL=%s\nHA_TOKEN=%s\nNINEPLUS_BEARER_TOKEN=%s\nNINEPLUS_ACCOUNT=%s\nNINEPLUS_PASSWORD=%s\n' \
    "$HA_URL_VALUE" "$HA_TOKEN_VALUE" "$BEARER" "$APP_ACCOUNT" "$APP_PASSWORD" > .env
  printf '{\n  "vehicles": [\n    {\n      "sn": "%s",\n      "name": "%s",\n      "model": "%s",\n      "integration": "hasscc/ninebot"\n    }\n  ]\n}\n' \
    "$VEHICLE_SN_JSON" "$VEHICLE_NAME_JSON" "$VEHICLE_MODEL_JSON" > config.json
fi

say "拉取并启动 NinePlus Home Assistant 服务"
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
    printf 'iPhone 服务器地址：http://%s:19009\n登录账号：%s\nBearer Token：%s\n' \
      "$LOCAL_IP" "$NINEPLUS_ACCOUNT" "$NINEPLUS_BEARER_TOKEN"
    exit 0
  fi
  sleep 2
done

docker compose logs --tail=80 nineplus-ha
die "服务未通过健康检查，请查看上面的日志。"
