#!/usr/bin/env bash
# Pesquisa de estratégias: compara as variantes no mesmo histórico e imprime só os resumos.
#
#   - 4 pares (ETH, BTC, BNB, XRP) para ter amostra e evitar conclusões tiradas de um só ativo
#   - período de DESENVOLVIMENTO (in-sample): mar/2021 a dez/2023
#   - período de VALIDAÇÃO (out-of-sample): jan/2024 até hoje
#   - taxa de 0,1% por operação (taxa padrão da Binance spot)
#
# Uma variante só merece ir para a simulação se for bem nos DOIS períodos.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/tmp/research}"
OUT="$DATA_DIR/out"
mkdir -p "$DATA_DIR/data/binance" "$OUT"

IS_RANGE="${IS_RANGE:-20210301-20240101}"
OOS_RANGE="${OOS_RANGE:-20240101-}"
RULE_STRATEGIES="${RULE_STRATEGIES:-CandlePatternStrategy CandleV2Context CandleV2Confirm CandleV2Regime CandleV2Trail}"
RUN_FREQAI="${RUN_FREQAI:-true}"

BASE=(
  --userdir /freqtrade/user_data
  --config /freqtrade/user_data/config.json
  --config /freqtrade/user_data/config-research.json
  --datadir "$DATA_DIR/data/binance"
)

summary() {
  # Imprime a tabela STRATEGY SUMMARY e a variação do mercado de um relatório
  local file="$1"
  awk '/STRATEGY SUMMARY/{p=1} p{print} /^└/{if(p){exit}}' "$file"
  grep -m1 "Market change" "$file" || true
}

fail() {
  echo "!! Falhou: $1. Últimas linhas do log:" >&2
  tail -n 40 "$2" >&2
}

echo "== Baixando histórico (4h e 1d, desde 2019) =="
if ! freqtrade download-data "${BASE[@]}" --timerange 20190101- -t 4h 1d > "$OUT/download.log" 2>&1; then
  fail "download" "$OUT/download.log"
  sleep infinity
fi
echo "ok"

for label in IS OOS; do
  if [ "$label" = "IS" ]; then range="$IS_RANGE"; title="DESENVOLVIMENTO $IS_RANGE"; else range="$OOS_RANGE"; title="VALIDAÇÃO $OOS_RANGE"; fi
  echo
  echo "================ REGRAS - $title ================"
  # shellcheck disable=SC2086
  if freqtrade backtesting "${BASE[@]}" \
      --strategy-list $RULE_STRATEGIES \
      --timerange "$range" --fee 0.001 --export none --cache none \
      > "$OUT/rules_$label.txt" 2> "$OUT/rules_$label.log"; then
    summary "$OUT/rules_$label.txt"
  else
    fail "backtest de regras ($label)" "$OUT/rules_$label.log"
  fi
done

if [ "$RUN_FREQAI" = "true" ]; then
  echo
  echo "================ FREQAI - 20210301- (retreino a cada 60 dias) ================"
  echo "(pode levar vários minutos: treina um modelo por par a cada janela)"
  if freqtrade backtesting "${BASE[@]}" \
      --config /freqtrade/user_data/config-freqai.json \
      --strategy CandleFreqAIStrategy --freqaimodel LightGBMClassifier \
      --timerange 20210301- --fee 0.001 --export none --cache none --breakdown year \
      > "$OUT/freqai.txt" 2> "$OUT/freqai.log"; then
    awk '/BACKTESTING REPORT/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/freqai.txt"
    awk '/YEAR BREAKDOWN/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/freqai.txt"
    awk '/ENTER TAG STATS/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/freqai.txt"
    summary "$OUT/freqai.txt"
  else
    fail "backtest FreqAI" "$OUT/freqai.log"
  fi
fi

echo
echo "== PESQUISA CONCLUÍDA =="
sleep infinity
