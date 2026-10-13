# candle-bot

Bot de trading **seguidor de tendência** para a Binance spot, construído sobre o [Freqtrade](https://www.freqtrade.io). Opera no gráfico **diário**, em 12 criptomoedas, e usa os **candles como confirmação**: só compra um rompimento quando a vela do rompimento é forte e vem com volume.

Ele roda em **modo simulação** (dry-run): opera com uma carteira fictícia de 1.000 USDT, usando preços reais, sem chave de API e sem arriscar dinheiro. Cada entrada e saída é avisada no **Telegram**.

> **Aviso:** resultados passados não garantem resultados futuros. Este projeto é um estudo, não uma recomendação de investimento. Deixe rodar em simulação por semanas e só considere dinheiro real se entender e aceitar o risco.

---

## A estratégia: `TrendBreakoutLiquid`

Arquivo: [`user_data/strategies/TrendBreakoutStrategy.py`](user_data/strategies/TrendBreakoutStrategy.py).

| Regra | Detalhe |
|---|---|
| Universo | BTC, ETH, BNB, XRP, SOL, ADA, DOGE, LTC, LINK, AVAX, TRX e DOT (contra USDT) |
| Liquidez | só opera as **6 moedas mais negociadas** dos últimos 90 dias (ranking refeito todo dia com dados passados) |
| Regime | só compra com o **BTC acima da média de 100 dias** |
| Entrada | fechamento acima da **máxima dos últimos 30 dias** (rompimento) |
| Confirmação por candle | a vela do rompimento precisa ter **corpo maior que 50% da amplitude**, **fechar no terço superior** e ter **volume acima da média de 20 dias** |
| Stop inicial | **2,5 ATR** abaixo do fechamento do rompimento |
| Stop móvel | sobe para a **mínima dos últimos 10 dias** e nunca desce |
| Saída | só pelo stop: a tendência corre enquanto durar |
| Tamanho da posição | arrisca **1,5% do saldo** até o stop inicial, limitado a 1/6 do saldo |
| Vagas | no máximo **6 posições** ao mesmo tempo |

### Resultado no Freqtrade (backtest, taxa de 0,1% por lado)

| Período | Lucro total | Ao ano | Drawdown máx. | Profit factor | Operações | Mercado (comprar e segurar) |
|---|---|---|---|---|---|---|
| set/2019 – dez/2022 | +170% | 34,7% | 21,0% | 3,94 | 68 | +483% |
| 2023 – 2024 | +135% | 53,3% | 18,9% | 4,29 | 56 | +410% |
| 2025 – out/2026 | +5,1% | 2,9% | 12,5% | 1,28 | 36 | −38% |

A estratégia ganha bem menos que comprar e segurar nos mercados de alta, e protege o capital nos de baixa: em 2025–26 as 12 moedas caíram 38% em média, e ela terminou positiva, com drawdown de 12,5%.

---

## Como ela foi encontrada

A pesquisa está toda em [`research/`](research/) e seguiu um protocolo definido **antes** de olhar os resultados:

- **Três períodos:** desenvolvimento (set/2019 a dez/2022), validação (2023–24) e teste final (2025 a out/2026).
- **Aprovação:** retorno anual ≥ 15%, drawdown ≤ 25%, profit factor ≥ 1,3 e ≥ 40 operações nos dois primeiros períodos; resultado positivo com drawdown ≤ 25% no teste final.
- **Robustez:** os vizinhos dos parâmetros (±20% em cada um) também precisam passar.
- **Viés de sobrevivência:** a mesma estratégia, sem mudanças, foi testada num universo de 32 moedas, incluindo as que colapsaram ou saíram da Binance (LUNA, FTT, WAVES, MATIC e outras).

O que foi testado e descartado:

| Família | Resultado |
|---|---|
| Padrões de **reversão** por candle (v1, v2 e variações) | perdem dinheiro em todos os períodos |
| Padrões de candle + **FreqAI** (LightGBM como filtro) | zero a zero, sem vantagem consistente |
| Reversão à média (RSI(2), Bandas de Bollinger, IBS) | positiva, mas fraca demais |
| Rotação por momentum | lucrativa, mas com drawdown de 45–60% |
| Rompimento sem filtro de liquidez | excelente até 2024, negativo em 2025–26 |
| **Rompimento + vela forte + liquidez + risco de 1,5%** | **aprovado em tudo** |

Robustez da estratégia final:

- **84%** dos 243 vizinhos de parâmetros passam em todos os critérios, nos três períodos e nos dois universos.
- **100%** deles são positivos no teste final.
- No universo de 32 moedas, o teste final dá **+13,6% ao ano**.

Ferramentas da pesquisa:

- [`research/lab.py`](research/lab.py), [`research/slots.py`](research/slots.py) e [`research/families.py`](research/families.py): laboratório rápido em pandas que reproduz as regras do Freqtrade (sinais idênticos, retornos próximos).
- [`.github/workflows/research-data.yml`](.github/workflows/research-data.yml): baixa o histórico do repositório público da Binance (data.binance.vision) para a branch `research-data`.
- [`research.sh`](research.sh) e [`Dockerfile.research`](Dockerfile.research): serviço do Railway que roda os backtests de confirmação no Freqtrade.

As estratégias antigas continuam no repositório para referência: [`CandlePatternStrategy.py`](user_data/strategies/CandlePatternStrategy.py), [`CandleProStrategy.py`](user_data/strategies/CandleProStrategy.py) e [`CandleFreqAIStrategy.py`](user_data/strategies/CandleFreqAIStrategy.py).

---

## Rodar no Railway

O `Dockerfile` e o `start.sh` são usados pelo Railway. O comportamento é controlado por variáveis do serviço:

| Variável | Valor | Para quê |
|---|---|---|
| `BOT_MODE` | `trade`, `backtest` ou `pause` | `trade` liga o bot; `backtest` imprime um relatório no log; `pause` deixa o serviço parado sem acessar a Binance |
| `STRATEGY` | padrão `TrendBreakoutLiquid` | estratégia a usar |
| `EXTRA_CONFIGS` | padrão `config-trend.json config-liquid.json` | configurações aplicadas por cima do `config.json` |
| `RAILWAY_RUN_UID` | `0` | necessário para o Freqtrade gravar no volume |
| `FREQTRADE__TELEGRAM__ENABLED` | `true` | liga os avisos no Telegram |
| `FREQTRADE__TELEGRAM__TOKEN` | token do @BotFather | |
| `FREQTRADE__TELEGRAM__CHAT_ID` | seu chat id | |

Pontos importantes:

- **Região fora dos EUA.** A Binance bloqueia acessos vindos dos Estados Unidos. Use Singapura (`asia-southeast1`): em Amsterdã, o IP de saída compartilhado do Railway chegou a ser bloqueado pela Binance por excesso de requisições de outros usuários.
- **Volume em `/data`.** Guarda o histórico e o banco de operações (`tradesv3-trend.sqlite`) entre reinícios.

---

## Rodar no seu computador

Precisa do [Docker](https://docs.docker.com/get-docker/) com Docker Compose.

```bash
git clone <url-deste-repositorio>
cd <pasta-do-repositorio>
cp .env.example .env      # no Windows (PowerShell): copy .env.example .env
```

Para os avisos no Telegram:

1. No **@BotFather**, envie `/newbot` e guarde o token.
2. No **@userinfobot**, descubra o seu chat id.
3. Envie `/start` para o seu bot.
4. Preencha o `.env`.

```env
FREQTRADE__TELEGRAM__ENABLED=true
FREQTRADE__TELEGRAM__TOKEN=123456789:ABCdef...
FREQTRADE__TELEGRAM__CHAT_ID=987654321
```

Backtest:

```bash
docker compose run --rm freqtrade download-data \
  --config /freqtrade/user_data/config.json \
  --config /freqtrade/user_data/config-trend.json \
  --timerange 20190101- -t 1d

docker compose run --rm freqtrade backtesting \
  --config /freqtrade/user_data/config.json \
  --config /freqtrade/user_data/config-trend.json \
  --config /freqtrade/user_data/config-liquid.json \
  --strategy TrendBreakoutLiquid \
  --timerange 20230101-20250101 --fee 0.001 --breakdown year
```

Simulação: `docker compose up -d`. No Telegram, o bot responde a `/status`, `/profit`, `/balance`, `/daily`, `/performance` e `/help`.

---

## Operar com dinheiro real

Só depois de semanas de simulação com resultados coerentes com o backtest:

1. Na Binance, crie uma chave de API com permissão **apenas de leitura e de spot trading**. **Nunca** ative saques, e restrinja a chave ao IP do servidor, se possível.
2. Preencha `FREQTRADE__EXCHANGE__KEY` e `FREQTRADE__EXCHANGE__SECRET` (no `.env` ou nas variáveis do Railway).
3. Em `user_data/config.json`, troque `"dry_run": true` por `"dry_run": false`.
4. Reinicie o bot.

---

## Estrutura

```
.
├── docker-compose.yml               # roda o bot em Docker no seu computador
├── Dockerfile / start.sh            # imagem e inicialização no Railway
├── Dockerfile.research / research.sh  # serviço de pesquisa no Railway
├── research/                        # laboratório de pesquisa (backtester rápido)
├── tools/                           # download do histórico e exportação
└── user_data/
    ├── config.json                  # configuração base (dry-run, Telegram)
    ├── config-trend.json            # gráfico diário e os 12 pares
    ├── config-liquid.json           # 6 vagas
    └── strategies/
        └── TrendBreakoutStrategy.py # a estratégia atual
```

Documentação do Freqtrade: https://www.freqtrade.io/en/stable/
