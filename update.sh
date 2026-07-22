#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${NINEPLUS_DIR:-$PWD}"
REPO_RAW="https://raw.githubusercontent.com/wuchiawuchi/nineplus-ha-server/main"

say() { printf '\n%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "没有找到 Docker。"
docker compose version >/dev/null 2>&1 || die "Docker Compose 不可用。"
command -v curl >/dev/null 2>&1 || die "没有找到 curl。"

mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

say "下载最新部署配置"
tmp_compose="compose.yaml.tmp.$$"
trap 'rm -f "$tmp_compose"' EXIT
curl -fsSL "$REPO_RAW/compose.yaml" -o "$tmp_compose"
docker compose -f "$tmp_compose" config -q
mv "$tmp_compose" compose.yaml
trap - EXIT

say "拉取最新镜像"
docker compose pull

say "重建并启动服务（保留现有数据卷）"
docker compose up -d --force-recreate --remove-orphans

say "等待服务就绪"
for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:19009/healthz >/dev/null 2>&1; then
    say "更新成功。"
    docker compose ps
    exit 0
  fi
  sleep 2
done

docker compose ps >&2 || true
docker compose logs --tail=80 nineplus >&2 || true
die "服务未通过健康检查。"
