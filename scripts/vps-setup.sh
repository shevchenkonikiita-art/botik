#!/usr/bin/env bash
# Ubuntu 22.04/24.04 на Oracle Cloud Always Free (або інший VPS)
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-plugin
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER" || true
  echo "Вийди з SSH і зайди знову (група docker), потім запусти скрипт ще раз."
  exit 0
fi

if [ ! -f .env ]; then
  echo "Створи .env з BOT_TOKEN (скопіюй з .env.example)"
  exit 1
fi

docker compose build
docker compose up -d
docker compose logs -f --tail=50
