#!/usr/bin/env bash
# Проверка почтового контура GrosterHit (host-network mailer).
# Запуск на сервере из корня проекта:  bash deploy/check-mail.sh
# Exit 0 = всё ОК, иначе код >0 и список FAIL.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail=0
ok()   { echo "OK   $*"; }
warn() { echo "WARN $*"; }
bad()  { echo "FAIL $*"; fail=$((fail + 1)); }

echo "=== grosterhit mail check ==="
echo "cwd=$ROOT"
echo "git=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') $(git log -1 --oneline 2>/dev/null || true)"
echo

# 1) compose: mailer на host
if grep -q 'network_mode: host' docker-compose.yml; then
  ok "docker-compose.yml: mailer network_mode: host"
else
  bad "docker-compose.yml без network_mode: host — подтяните ветку sendmail / обновите код"
fi

if grep -q 'host.docker.internal:8080' docker-compose.yml; then
  ok "compose MAILER_SERVICE_URL → host.docker.internal:8080"
else
  bad "compose всё ещё шлёт на http://mailer:8080"
fi

# 2) .env: MAILER_HOST_IP
dup=$(grep -c '^MAILER_HOST_IP=' .env 2>/dev/null || true)
dup=${dup:-0}
if [[ "$dup" -gt 1 ]]; then
  warn "MAILER_HOST_IP задан $dup раз в .env — оставьте одну строку"
fi
host_ip=$(grep '^MAILER_HOST_IP=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
br_ip=$(ip -br a 2>/dev/null | awk '/^br-/{print $3}' | head -1 | cut -d/ -f1 || true)
if [[ -z "$host_ip" ]]; then
  bad "MAILER_HOST_IP не задан в .env (нужен IP br-*, обычно 172.18.0.1)"
elif [[ -n "$br_ip" && "$host_ip" != "$br_ip" ]]; then
  warn "MAILER_HOST_IP=$host_ip, а br-* сейчас $br_ip — проверьте extra_hosts"
else
  ok "MAILER_HOST_IP=$host_ip${br_ip:+ (br=$br_ip)}"
fi

api_port=$(grep '^API_HOST_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
api_port=${api_port:-8000}
ok "API_HOST_PORT=$api_port"

# 3) контейнеры
mailer_id=$(docker compose ps -q mailer 2>/dev/null || true)
api_id=$(docker compose ps -q api 2>/dev/null || true)
if [[ -z "$mailer_id" ]]; then
  bad "контейнер mailer не запущен"
else
  net=$(docker inspect "$mailer_id" --format '{{.HostConfig.NetworkMode}}' 2>/dev/null || echo '?')
  if [[ "$net" == "host" ]]; then
    ok "mailer NetworkMode=host"
  else
    bad "mailer NetworkMode=$net (нужен host) — docker compose up -d --force-recreate mailer"
  fi
fi

# 4) env внутри контейнеров
if [[ -n "$api_id" ]]; then
  url=$(docker compose exec -T api sh -c 'echo -n "$MAILER_SERVICE_URL"' 2>/dev/null || true)
  if [[ "$url" == "http://host.docker.internal:8080" ]]; then
    ok "api MAILER_SERVICE_URL=$url"
  elif [[ "$url" == *"mailer"* ]]; then
    bad "api MAILER_SERVICE_URL=$url — нужен http://host.docker.internal:8080 (--force-recreate api workers)"
  else
    bad "api MAILER_SERVICE_URL='$url'"
  fi
  hdi=$(docker compose exec -T api getent hosts host.docker.internal 2>/dev/null | awk '{print $1}' || true)
  if [[ -n "$hdi" ]]; then
    ok "api host.docker.internal → $hdi"
  else
    bad "api не резолвит host.docker.internal (MAILER_HOST_IP / extra_hosts)"
  fi
else
  bad "контейнер api не запущен"
fi

if [[ -n "$mailer_id" ]]; then
  cb=$(docker compose exec -T mailer sh -c 'echo -n "$CALLBACK_URL"' 2>/dev/null || true)
  expect_cb="http://127.0.0.1:${api_port}/esp/webhook"
  if [[ "$cb" == "$expect_cb" ]]; then
    ok "mailer CALLBACK_URL=$cb"
  else
    warn "mailer CALLBACK_URL='$cb' (ожидали $expect_cb)"
  fi
fi

# 5) порты на хосте
if ss -tln 2>/dev/null | grep -q ":${api_port} " || ss -tln 2>/dev/null | grep -q ":${api_port}\$"; then
  ok "хост слушает :$api_port (api)"
else
  # ss формат может отличаться
  if curl -sf -m 3 "http://127.0.0.1:${api_port}/health" >/dev/null 2>&1; then
    ok "api health на 127.0.0.1:$api_port"
  else
    bad "api не отвечает на 127.0.0.1:$api_port — callback из mailer получит Connection refused"
  fi
fi

if curl -sf -m 3 "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
  ok "mailer health на 127.0.0.1:8080"
else
  bad "mailer не отвечает на 127.0.0.1:8080"
fi

# 6) api → mailer
if [[ -n "$api_id" ]]; then
  if docker compose exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://host.docker.internal:8080/health', timeout=5).read()" \
    >/dev/null 2>&1; then
    ok "api → host.docker.internal:8080/health"
  else
    bad "api не достучался до mailer (ufw: allow from 172.16.0.0/12 to 8080; MAILER_HOST_IP=IP br-*)"
  fi
fi

# 7) SMTP из mailer
if [[ -n "$mailer_id" ]]; then
  if docker compose exec -T mailer python -c \
    "import socket; socket.create_connection(('smtp.yandex.ru',465),5).close(); print('ok')" \
    >/dev/null 2>&1; then
    ok "mailer → smtp.yandex.ru:465"
  else
    bad "mailer не достучался до smtp.yandex.ru:465 (нужен host-network; bridge = ENETUNREACH)"
  fi
fi

# 8) deep diagnostics (если токен не обязателен / пуст)
if curl -sf -m 8 "http://127.0.0.1:8080/v1/diagnostics" >/tmp/gh-mail-diag.json 2>/dev/null; then
  ok "GET /v1/diagnostics"
  python3 -c "
import json
d=json.load(open('/tmp/gh-mail-diag.json'))
print('     provider=', d.get('provider'), 'ok=', d.get('ok'))
print('     smtp=', d.get('smtp'))
print('     callback=', d.get('callback'))
print('     outbox=', d.get('outbox'))
" 2>/dev/null || cat /tmp/gh-mail-diag.json
else
  warn "/v1/diagnostics недоступен без токена или mailer старый — после деплоя появится"
fi

# 9) ufw hint
if command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -q '8080'; then
    ok "ufw: есть правило на 8080"
  else
    warn "ufw: нет явного allow на 8080 с docker-моста — при Connection refused: ufw allow from 172.16.0.0/12 to any port 8080"
  fi
fi

echo
if [[ "$fail" -eq 0 ]]; then
  echo "=== ALL OK ==="
  exit 0
fi
echo "=== FAILURES: $fail ==="
echo "Типовой ремонт:"
echo "  git fetch && git checkout cursor/sendmail-mail-transport-f71b && git pull"
echo "  BR=\$(ip -br a | awk '/^br-/{print \$3}' | head -1 | cut -d/ -f1)"
echo "  grep -q MAILER_HOST_IP .env && sed -i \"s/^MAILER_HOST_IP=.*/MAILER_HOST_IP=\$BR/\" .env || echo MAILER_HOST_IP=\$BR >> .env"
echo "  ufw allow from 172.16.0.0/12 to any port 8080 proto tcp"
echo "  docker compose up -d --build --force-recreate api workers mailer"
exit 1
