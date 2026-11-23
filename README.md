# Irrigação inteligente: servidor + painel

## Rodar
```bash
pip install -r requirements.txt
python app.py            # painel em http://127.0.0.1:5000
python simulador.py      # (outro terminal) simula o sensor, sem hardware
```
O banco `irrigacao.db` é criado sozinho na primeira execução.

## Estrutura
- `app.py`: servidor Flask, banco SQLite, API e lógica de decisão
- `templates/index.html`: painel (umidade atual, gráfico 24 h, controle da bomba, configuração, histórico)
- `simulador.py`: finge ser o ESP32 para testar

## Como o ESP32 conversa com o servidor
A cada 10 a 30 s ele manda um POST:

```
POST http://IP_DO_PC:5000/api/leitura
Header: X-API-Key: troque-essa-chave
Body:   {"raw": 2300}            (leitura bruta do sensor capacitivo)
   ou   {"umidade": 42.5}        (já em %)
        opcional: "temperatura": 27.3
```
Resposta:
```
{"bomba": true, "duracao_s": 30, "motivo": "solo abaixo do mínimo", "umidade": 31.2}
```
Se `bomba` for `true`, o ESP32 liga a bomba por `duracao_s` segundos. Se for `false`, mantém desligada.

## Calibrar o sensor capacitivo
1. Sensor no ar (seco): anote a leitura bruta e coloque em "Sensor seco".
2. Sensor dentro d'água: anote e coloque em "Sensor na água".
3. Salve no painel. O servidor converte para % sozinho.

## Antes de usar de verdade
- Troque `API_KEY` (variável de ambiente `API_KEY` ou no topo do `app.py`).
- O painel não tem login. Use só na sua rede local ou adicione senha antes de expor na internet.
- Desligue `debug=True` no final do `app.py` fora dos testes.

## Parte de IA
Hoje a decisão é por limites + uma previsão de tendência (regressão linear nas últimas 6 h, mostrada como "Chega no mínimo em"). Depois de alguns dias de dados reais no banco, dá para treinar um modelo (por exemplo, prever a umidade da próxima hora com temperatura e horário) e usá-lo na função `decidir()`.
