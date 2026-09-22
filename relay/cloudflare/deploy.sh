#!/usr/bin/env bash
# 1회: Cloudflare 무료 계정 필요. 실행 후 출력되는 URL과 토큰을 install-hooks.sh 와 지휘자에게 준다.
set -eu; cd "$(dirname "$0")"
command -v npx >/dev/null || { echo "node/npx 필요"; exit 1; }
TOKEN=$(openssl rand -hex 24); [ -f "$HOME/.tmux-agents/relay.env" ] && . "$HOME/.tmux-agents/relay.env" && [ -n "${RELAY_TOKEN:-}" ] && TOKEN=$RELAY_TOKEN
npx --yes wrangler@latest whoami >/dev/null 2>&1 || npx --yes wrangler@latest login
printf '%s' "$TOKEN" | npx --yes wrangler@latest secret put RELAY_TOKEN
OUT=$(npx --yes wrangler@latest deploy 2>&1 | tee /dev/stderr)
URL=$(printf '%s' "$OUT" | grep -oE 'https://[a-z0-9.-]+(workers\.dev|\.[a-z]{2,})' | grep -v 'workers.dev/' | head -1)
CUSTOM=$(grep -oE 'pattern *= *"[^"]+"' wrangler.toml | head -1 | sed 's/.*"\(.*\)"/\1/'); [ -n "$CUSTOM" ] && URL="https://$CUSTOM"
[ -n "$URL" ] || { echo; echo "배포 실패: workers.dev 서브도메인이 없습니다. https://dash.cloudflare.com 에서 Workers & Pages 메뉴를 한 번 열면 자동 생성됩니다. 그 후 이 스크립트를 다시 실행하세요."; exit 1; }
echo; echo "RELAY_URL=$URL"; echo "RELAY_TOKEN=$TOKEN"
printf 'RELAY_URL=%s\nRELAY_TOKEN=%s\n' "$URL" "$TOKEN" > "$HOME/.tmux-agents/relay.env"; chmod 600 "$HOME/.tmux-agents/relay.env"
echo "저장: ~/.tmux-agents/relay.env  → 이제 bash ~/workspace/woon/orchestration/install-hooks.sh 를 다시 실행"
