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
TIMEFRAME="${TIMEFRAME:-4h}"
TIMERANGE="${BACKTEST_TIMERANGE:-20220101-}"
STRATEGY="${STRATEGY:-CandlePatternStrategy}"

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
  --datadir "$DATA_DIR/data/binance"
)

case "$MODE" in
  backtest)
    echo "== Baixando histórico ($TIMEFRAME, $TIMERANGE) =="
    freqtrade download-data "${COMMON[@]}" --timerange "$TIMERANGE" -t "$TIMEFRAME"

    echo "== Rodando backtest da estratégia $STRATEGY =="
    freqtrade backtesting "${COMMON[@]}" \
      --strategy "$STRATEGY" \
      --timerange "$TIMERANGE" \
      --breakdown month \
      --export none

    echo "== Backtest concluído. O resultado está acima neste log. =="
    echo "== Para ligar o bot, mude BOT_MODE para trade. =="
    # Mantém o container parado sem reiniciar o backtest em loop.
    sleep infinity
    ;;

  trade)
    exec freqtrade trade "${COMMON[@]}" \
      --strategy "$STRATEGY" \
      --db-url "sqlite:///$DATA_DIR/tradesv3.sqlite"
    ;;

  *)
    echo "BOT_MODE inválido: '$MODE'. Use 'backtest' ou 'trade'." >&2
    exit 1
    ;;
esac
