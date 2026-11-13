#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
mode="${1:-start}"
if [[ $# -gt 1 ]]; then
    printf '用法：bash scripts/demo.sh [start|live|stop]\n' >&2
    exit 2
fi

env_file=/dev/null
case "$mode" in
    start|stop) export LLM_API_KEY="" ;;
    live)
        if [[ ! -f .env ]]; then
            printf '请先在项目 .env 中配置模型地址和密钥。\n' >&2
            exit 1
        fi
        env_file=.env
        ;;
    *)
        printf '用法：bash scripts/demo.sh [start|live|stop]\n' >&2
        exit 2
        ;;
esac

export RAG_API_PORT="${RAG_DEMO_API_PORT:-18000}"
export RAG_UI_PORT="${RAG_DEMO_UI_PORT:-18501}"
export RAG_SANDBOX_IMAGE=rag-demo-worker:local
export RAG_POSTGRES_HOST="" RAG_LANGSMITH_EXPORT=false LANGSMITH_API_KEY=""
compose=(docker compose --project-name rag-demo --env-file "$env_file" -f compose.yaml)

if [[ "$mode" == "stop" ]]; then
    "${compose[@]}" stop
    exit
fi

if [[ "$mode" == "live" ]] && ! "${compose[@]}" config --environment | awk '
    /^LLM_API_KEY=[[:space:]]*[^[:space:]]/ { configured = 1 }
    END { exit !configured }
'; then
    printf '请先在项目 .env 中填写 LLM_API_KEY。\n' >&2
    exit 1
fi

"${compose[@]}" up -d --build --wait
printf '演示界面 http://127.0.0.1:%s\n演示 API http://127.0.0.1:%s\n' "$RAG_UI_PORT" "$RAG_API_PORT"
