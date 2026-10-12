# candle-bot

Bot de trading para **ETH/USDT na Binance**, no gráfico de **4 horas**, baseado em **padrões de candle** (velas japonesas) e construído sobre o [Freqtrade](https://www.freqtrade.io).

Ele começa em **modo simulação** (dry-run): opera com uma carteira fictícia de 1.000 USDT, usando preços reais, sem precisar de chave de API e sem arriscar dinheiro. Cada entrada e saída é avisada no **Telegram**, com o nome do padrão que gerou o sinal.

> **Aviso:** padrões de candle indicam probabilidades, não certezas. Este projeto é um estudo. Não é recomendação de investimento. Faça backtest, deixe rodar em simulação por semanas e só considere dinheiro real se entender e aceitar o risco.

---

## Como a estratégia funciona

A estratégia fica em [`user_data/strategies/CandlePatternStrategy.py`](user_data/strategies/CandlePatternStrategy.py) e usa a TA-Lib para detectar os padrões. O Freqtrade só analisa **velas fechadas**, então nenhum sinal é gerado por uma vela ainda em formação.

**Compra** quando aparecem ao mesmo tempo:

| Condição | Detalhe |
|---|---|
| Padrão de reversão de alta | martelo, engolfo de alta, estrela da manhã ou linha de perfuração |
| Contexto de queda | RSI(14) abaixo de 45 |
| Volume confirmando | volume da vela acima da média das últimas 20 velas |

**Vende** quando acontece qualquer um destes:

| Condição | Detalhe |
|---|---|
| Padrão de reversão de baixa | estrela cadente, engolfo de baixa, estrela da tarde, nuvem negra ou enforcado, com RSI acima de 55 |
| Sobrecompra extrema | RSI acima de 78 |
| Stoploss | perda de 6% |
| Trailing stop | depois de 4% de lucro, protege o ganho a 2% do topo |
| ROI mínimo | 12% de lucro a qualquer momento; o alvo cai para 6% após 2 dias, 3% após 4 dias e zero a zero após 8 dias |

Todos esses números são pontos de partida. Ajuste-os depois de ver o resultado do backtest.

---

## Requisitos

- [Docker](https://docs.docker.com/get-docker/) com Docker Compose (no Windows e no Mac, o Docker Desktop já inclui os dois).

Não é preciso instalar Python nem TA-Lib: a imagem oficial do Freqtrade já traz tudo.

---

## Instalação

```bash
git clone <url-deste-repositorio>
cd <pasta-do-repositorio>
cp .env.example .env
```

No Windows (PowerShell), use `copy .env.example .env` no lugar do `cp`.

### Configurar o Telegram

1. No Telegram, abra o **@BotFather**, envie `/newbot` e siga as instruções. Ele vai te dar um **token**.
2. Abra o **@userinfobot** e envie qualquer mensagem. Ele responde com o seu **chat id** (um número).
3. Abra uma conversa com o bot que você acabou de criar e envie `/start`, para que ele possa te mandar mensagens.
4. Preencha o `.env`:

```env
FREQTRADE__TELEGRAM__ENABLED=true
FREQTRADE__TELEGRAM__TOKEN=123456789:ABCdef...
FREQTRADE__TELEGRAM__CHAT_ID=987654321
```

O token fica só no `.env`, que está no `.gitignore` e nunca vai para o GitHub.

---

## 1. Baixar o histórico

```bash
docker compose run --rm freqtrade download-data \
  --config /freqtrade/user_data/config.json \
  --timerange 20220101- \
  -t 4h
```

Isso baixa as velas de 4h do ETH/USDT desde janeiro de 2022 para `user_data/data/`.

## 2. Rodar o backtest

```bash
docker compose run --rm freqtrade backtesting \
  --config /freqtrade/user_data/config.json \
  --strategy CandlePatternStrategy \
  --timerange 20220101- \
  --breakdown month
```

O relatório mostra o lucro total, a taxa de acerto, o drawdown máximo e, na tabela **Enter Tag**, o resultado separado por padrão (martelo, engolfo de alta etc.). É ali que você descobre quais padrões realmente funcionam no ETH.

Compare sempre com o resultado de simplesmente comprar e segurar no mesmo período (linha *Market change* do relatório).

## 3. Rodar em simulação

```bash
docker compose up -d
```

O bot fica rodando em segundo plano e reinicia sozinho se o computador ou o Docker reiniciar. Você deve receber uma mensagem de início no Telegram.

Comandos úteis:

```bash
docker compose logs -f     # acompanhar o log
docker compose down        # parar o bot
docker compose pull        # atualizar o Freqtrade (depois rode "docker compose up -d")
```

No Telegram, o bot responde a comandos como `/status`, `/profit`, `/balance`, `/daily`, `/performance` e `/help`.

---

## Otimizar os parâmetros (opcional)

Os limites de RSI e o fator de volume podem ser otimizados automaticamente com o hyperopt:

```bash
docker compose run --rm freqtrade hyperopt \
  --config /freqtrade/user_data/config.json \
  --strategy CandlePatternStrategy \
  --hyperopt-loss SharpeHyperOptLoss \
  --spaces buy sell \
  --timerange 20220101-20250101 \
  -e 300
```

Otimize num período e confira o resultado com um backtest em **outro** período (por exemplo, de 2025 em diante). Parâmetros que só funcionam no período em que foram otimizados não valem nada no mercado real.

---

## Operar com dinheiro real

Só depois de semanas de simulação com resultados que façam sentido:

1. Na Binance, crie uma chave de API com permissão **apenas de leitura e de spot trading**. **Nunca** ative a permissão de saque e, se possível, restrinja a chave ao IP da sua máquina.
2. Preencha `FREQTRADE__EXCHANGE__KEY` e `FREQTRADE__EXCHANGE__SECRET` no `.env`.
3. Em `user_data/config.json`, troque `"dry_run": true` por `"dry_run": false` e ajuste o `stake_amount` para o valor que você aceita arriscar.
4. Reinicie com `docker compose up -d`.

---

## Estrutura

```
.
├── docker-compose.yml                  # roda o Freqtrade em Docker
├── .env.example                        # modelo dos segredos (Telegram e Binance)
└── user_data/
    ├── config.json                     # configuração do bot (par, tempo gráfico, dry-run)
    └── strategies/
        └── CandlePatternStrategy.py    # a estratégia de padrões de candle
```

Documentação do Freqtrade: https://www.freqtrade.io/en/stable/
