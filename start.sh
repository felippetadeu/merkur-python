#!/usr/bin/env bash
# Inicializa o bot conforme a variável BOT_MODE:
#   BOT_MODE=backtest -> baixa o histórico e roda o backtest; o resultado sai no log
#   BOT_MODE=trade    -> roda o bot (em simulação enquanto "dry_run" for true no config.json)
#
# Dados persistentes (histórico de velas e banco de operações) ficam em DATA_DIR,
# que no Railway é o volume montado em /data.
set -euo pipefail

MODE="${BOT_MODE:-trade}"
DATA_DIR="${DATA_DIR:-/data}"
TIMEFRAME="${TIMEFRAME:-1d}"
TIMERANGE="${BACKTEST_TIMERANGE:-20220101-}"
STRATEGY="${STRATEGY:-TrendBreakoutLiquid}"
# Configurações adicionais (em user_data/), aplicadas por cima do config.json
EXTRA_CONFIGS="${EXTRA_CONFIGS:-config-trend.json config-liquid.json}"
DB_NAME="${DB_NAME:-tradesv3-trend.sqlite}"

# No Railway o container roda como root (RAILWAY_RUN_UID=0) para poder gravar no volume.
# A imagem do Freqtrade instala os pacotes na pasta do usuário ftuser, então o root
# precisa ser apontado para ela; sem isso o Python não encontra o módulo freqtrade.
if [ "$(id -u)" = "0" ] && [ -d /home/ftuser/.local ]; then
  export PYTHONUSERBASE=/home/ftuser/.local
  export PATH="/home/ftuser/.local/bin:$PATH"
fi

mkdir -p "$DATA_DIR/data"

COMMON=(
  --userdir /freqtrade/user_data
  --config /freqtrade/user_data/config.json
)
for cfg in $EXTRA_CONFIGS; do
  COMMON+=(--config "/freqtrade/user_data/$cfg")
done
COMMON+=(--datadir "$DATA_DIR/data/binance")

run_backtest() {
  echo "== Baixando histórico ($TIMEFRAME, $TIMERANGE) =="
  freqtrade download-data "${COMMON[@]}" --timerange "$TIMERANGE" -t "$TIMEFRAME" || return 1

  echo "== Rodando backtest da estratégia $STRATEGY =="
  freqtrade backtesting "${COMMON[@]}" \
    --strategy "$STRATEGY" \
    --timerange "$TIMERANGE" \
    --breakdown month \
    --export none || return 1
}

case "$MODE" in
  backtest)
    if run_backtest; then
      echo "== Backtest concluído. O resultado está acima neste log. =="
      echo "== Para ligar o bot, mude BOT_MODE para trade. =="
    else
      echo "== O backtest falhou. Veja o erro acima. ==" >&2
      echo "== O container fica parado para não repetir requisições à Binance. ==" >&2
    fi
    # Nunca sai: assim o Railway não reinicia o backtest em loop.
    sleep infinity
    ;;

  pause)
    echo "== BOT_MODE=pause: nada é executado e nenhuma requisição é feita à Binance. =="
    sleep infinity
    ;;

  trade)
    exec freqtrade trade "${COMMON[@]}" \
      --strategy "$STRATEGY" \
      --db-url "sqlite:///$DATA_DIR/$DB_NAME"
    ;;

  *)
    echo "BOT_MODE inválido: '$MODE'. Use 'backtest', 'trade' ou 'pause'." >&2
    exit 1
    ;;
esac
