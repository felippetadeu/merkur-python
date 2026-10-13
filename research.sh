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
OOS_RANGE="${OOS_RANGE:-20240101-20261010}"
RULE_STRATEGIES="${RULE_STRATEGIES:-CandleV2Context CandleV2Regime CandleV2Trail CandleV2Wide}"
DAILY_STRATEGY="${DAILY_STRATEGY:-CandleV2Daily}"
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

exit_reasons() {
  # Imprime, para cada estratégia do relatório, a tabela de motivos de saída
  local file="$1"
  awk '/Result for strategy/{print} /EXIT REASON STATS/{p=1} p{print} /^└/{p=0}' "$file"
}

fail() {
  echo "!! Falhou: $1. Últimas linhas do log:" >&2
  tail -n 40 "$2" >&2
}

if [ "${RESEARCH_MODE:-backtest}" = "export" ]; then
  # Baixa o histórico e imprime no log (em blocos) para análise fora do container
  EXPORT_PAIRS="${EXPORT_PAIRS:-BTC/USDT ETH/USDT BNB/USDT XRP/USDT SOL/USDT ADA/USDT DOGE/USDT LTC/USDT LINK/USDT AVAX/USDT}"
  EXPORT_TF="${EXPORT_TF:-4h}"
  echo "== Exportando $EXPORT_TF de: $EXPORT_PAIRS =="
  # shellcheck disable=SC2086
  if freqtrade download-data "${BASE[@]}" --pairs $EXPORT_PAIRS --timerange 20190101- -t "$EXPORT_TF" \
      > "$OUT/download.log" 2>&1; then
    # shellcheck disable=SC2086
    python /freqtrade/tools/export_data.py "$DATA_DIR/data/binance" "$EXPORT_TF" $EXPORT_PAIRS
  else
    fail "download" "$OUT/download.log"
  fi
  echo "== EXPORTAÇÃO CONCLUÍDA =="
  sleep infinity
fi

if [ "${RESEARCH_MODE:-backtest}" = "intraday" ]; then
  # Confirmação no Freqtrade das estratégias intradiárias (4h e 1h, 12 pares)
  INTRA_RUNS="${INTRA_RUNS:-Breakout4h:4h Breakout1h:1h}"
  INTRA_PERIODS="${INTRA_PERIODS:-P1:20190901-20230101 P2:20230101-20250101 P3:20250101-20261011}"
  TREND=(--config /freqtrade/user_data/config-trend.json)
  echo "== Baixando histórico (1h, 4h e 1d) dos 12 pares =="
  if ! freqtrade download-data "${BASE[@]}" "${TREND[@]}" --timerange 20190101- -t 1h 4h 1d > "$OUT/download.log" 2>&1; then
    fail "download" "$OUT/download.log"; sleep infinity
  fi
  for run in $INTRA_RUNS; do
    strat="${run%%:*}"; tf="${run#*:}"
    for item in $INTRA_PERIODS; do
      label="${item%%:*}"; range="${item#*:}"
      echo
      echo "================ $strat ($tf) - $label $range ================"
      if freqtrade backtesting "${BASE[@]}" "${TREND[@]}" --config "/freqtrade/user_data/config-intraday-$tf.json" \
          --strategy "$strat" --timerange "$range" --fee 0.001 --export none --cache none \
          > "$OUT/${strat}_$label.txt" 2> "$OUT/${strat}_$label.log"; then
        awk '/STRATEGY SUMMARY/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/${strat}_$label.txt"
        awk '/SUMMARY METRICS/{p=1} p&&/(CAGR|Max % of account underwater|Profit factor|Total profit %|Total\/Daily Avg Trades|Market change)/{print} /^└/{p=0}' "$OUT/${strat}_$label.txt"
      else
        fail "backtest $strat ($label)" "$OUT/${strat}_$label.log"
      fi
    done
  done
  echo "== INTRADIÁRIO CONCLUÍDO =="
  sleep infinity
fi

if [ "${RESEARCH_MODE:-backtest}" = "trend" ]; then
  # Confirmação no Freqtrade das estratégias de tendência (gráfico diário, 12 pares)
  TREND=(--config /freqtrade/user_data/config-trend.json)
  if [ -n "${TREND_EXTRA_CONFIG:-}" ]; then TREND+=(--config "/freqtrade/user_data/$TREND_EXTRA_CONFIG"); fi
  TREND_STRATEGIES="${TREND_STRATEGIES:-TrendBreakout TrendBreakoutCandle}"
  TREND_PERIODS="${TREND_PERIODS:-P1:20190901-20230101 P2:20230101-20250101}"
  echo "== Baixando histórico diário dos 12 pares =="
  if ! freqtrade download-data "${BASE[@]}" "${TREND[@]}" --timerange 20190101- -t 1d > "$OUT/download.log" 2>&1; then
    fail "download" "$OUT/download.log"; sleep infinity
  fi
  for item in $TREND_PERIODS; do
    label="${item%%:*}"; range="${item#*:}"
    echo
    echo "================ TENDÊNCIA - $label $range ================"
    # shellcheck disable=SC2086
    if freqtrade backtesting "${BASE[@]}" "${TREND[@]}" --strategy-list $TREND_STRATEGIES \
        --timerange "$range" --fee 0.001 --export none --cache none --breakdown year \
        > "$OUT/trend_$label.txt" 2> "$OUT/trend_$label.log"; then
      summary "$OUT/trend_$label.txt"
      exit_reasons "$OUT/trend_$label.txt"
      awk '/Result for strategy/{print} /SUMMARY METRICS/{p=1} p&&/(CAGR|Max % of account underwater|Profit factor|Total profit %|Total\/Daily Avg Trades)/{print} /^└/{p=0}' "$OUT/trend_$label.txt"
    else
      fail "backtest de tendência ($label)" "$OUT/trend_$label.log"
    fi
  done
  echo "== TENDÊNCIA CONCLUÍDA =="
  sleep infinity
fi

echo "== Baixando histórico (4h, 1d e 1w, desde 2019) =="
if ! freqtrade download-data "${BASE[@]}" --timerange 20190101- -t 4h 1d 1w > "$OUT/download.log" 2>&1; then
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
    exit_reasons "$OUT/rules_$label.txt"
  else
    fail "backtest de regras ($label)" "$OUT/rules_$label.log"
  fi

  echo
  echo "================ DIÁRIO ($DAILY_STRATEGY) - $title ================"
  if freqtrade backtesting "${BASE[@]}" \
      --strategy "$DAILY_STRATEGY" --timeframe 1d \
      --timerange "$range" --fee 0.001 --export none --cache none \
      > "$OUT/daily_$label.txt" 2> "$OUT/daily_$label.log"; then
    awk '/STRATEGY SUMMARY/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/daily_$label.txt"
    awk '/EXIT REASON STATS/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/daily_$label.txt"
    grep -m1 "Market change" "$OUT/daily_$label.txt" || true
  else
    fail "backtest diário ($label)" "$OUT/daily_$label.log"
  fi
done

if [ "$RUN_FREQAI" = "true" ]; then
  echo
  echo "================ FREQAI - 20210301-20261010 (retreino a cada 60 dias) ================"
  echo "(pode levar vários minutos: treina um modelo por par a cada janela)"
  if freqtrade backtesting "${BASE[@]}" \
      --config /freqtrade/user_data/config-freqai.json \
      --strategy CandleFreqAIStrategy --freqaimodel LightGBMClassifier \
      --timerange 20210301-20261010 --fee 0.001 --export none --cache none --breakdown year \
      > "$OUT/freqai.txt" 2> "$OUT/freqai.log"; then
    awk '/BACKTESTING REPORT/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/freqai.txt"
    awk '/YEAR BREAKDOWN/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/freqai.txt"
    awk '/ENTER TAG STATS/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/freqai.txt"
    awk '/EXIT REASON STATS/{p=1} p{print} /^└/{if(p){exit}}' "$OUT/freqai.txt"
    summary "$OUT/freqai.txt"
  else
    fail "backtest FreqAI" "$OUT/freqai.log"
  fi
fi

echo
echo "== PESQUISA CONCLUÍDA =="
sleep infinity
