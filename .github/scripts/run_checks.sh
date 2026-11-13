#!/usr/bin/env bash
set -euo pipefail

mode="${1:?choose followup, browser, or nightly}"
shift
python_cmd="${RAG_TEST_PYTHON:-python}"
api_port="${RAG_TEST_API_PORT:-18000}"
ui_port="${RAG_TEST_UI_PORT:-18501}"
tmp_dir="$(mktemp -d)"
children=()
cleanup() {
  if [[ ${children[0]+started} ]]; then
    for pid in "${children[@]}"; do kill "$pid" 2>/dev/null || true; done
    for pid in "${children[@]}"; do wait "$pid" 2>/dev/null || true; done
  fi
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

export PYTHONDONTWRITEBYTECODE=1 RAG_DATA_DIR="$tmp_dir/state"
export RAG_LANGSMITH_EXPORT=false RAG_SANDBOX_SOCKET= RAG_GATEWAY_KEY= RAG_DEPLOYMENT=local RAG_BIRD_ROOT=
export LANGSMITH_TRACING=false

wait_ready() {
  local url="$1" log="$2"
  for _ in {1..30}; do
    if curl --noproxy '*' --max-time 2 --fail --silent "$url" >/dev/null; then return 0; fi
    sleep 1
  done
  cat "$log" >&2
  return 1
}

case "$mode" in
  followup)
    "$python_cmd" -m evaluation.run_nightly_conversations \
      --replay evaluation/splits/conversation_model_responses.json --output "$tmp_dir/conversations.json" "$@"
    exit
    ;;
  browser) export LLM_API_KEY= LLM_BASE_URL= ;;
  nightly) ;;
  *) echo "Unknown test mode: $mode" >&2; exit 2 ;;
esac

"$python_cmd" -m uvicorn app.product_api:app --host 127.0.0.1 --port "$api_port" --no-access-log >"$tmp_dir/api.log" 2>&1 &
children+=("$!")
export RAG_API_URL="http://127.0.0.1:$api_port"
wait_ready "$RAG_API_URL/healthz" "$tmp_dir/api.log"

case "$mode" in
  browser)
    "$python_cmd" -m streamlit run app/product_ui.py --server.address=127.0.0.1 --server.port="$ui_port" \
      --server.headless=true --browser.gatherUsageStats=false >"$tmp_dir/ui.log" 2>&1 &
    children+=("$!")
    export RAG_TEST_UI_URL="http://127.0.0.1:$ui_port"
    wait_ready "$RAG_TEST_UI_URL/_stcore/health" "$tmp_dir/ui.log"
    "$python_cmd" .github/scripts/browser_smoke.py "$@"
    ;;
  nightly) "$python_cmd" -m evaluation.run_nightly_conversations --base-url "$RAG_API_URL" "$@" ;;
esac
