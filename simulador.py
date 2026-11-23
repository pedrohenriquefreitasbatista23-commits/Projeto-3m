"""
Simula o ESP32 + sensor de umidade para você testar o painel sem hardware.

Com o servidor rodando (python app.py), abra outro terminal e rode:
  python simulador.py

O solo "seca" aos poucos; quando o servidor manda ligar a bomba, a umidade sobe.
"""
import json
import random
import time
import urllib.request

URL = "http://127.0.0.1:5000/api/leitura"
CHAVE = "troque-essa-chave"  # mesma do servidor (API_KEY)
INTERVALO_S = 2              # tempo entre leituras (no ESP32 real use 10 a 30 s)

umidade = 60.0
bomba_ate = 0.0

while True:
    agora = time.time()
    if agora < bomba_ate:
        umidade += 2.5                       # bomba ligada: solo ganha água
    else:
        umidade -= random.uniform(0.3, 0.9)  # solo secando
    umidade = max(0.0, min(100.0, umidade))

    corpo = json.dumps({
        "umidade": round(umidade, 1),
        "temperatura": round(random.uniform(24, 29), 1),
    }).encode()
    req = urllib.request.Request(
        URL, data=corpo,
        headers={"Content-Type": "application/json", "X-API-Key": CHAVE},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.load(r)
    except Exception as e:
        print("erro ao enviar:", e)
        time.sleep(3)
        continue

    if resp.get("bomba"):
        # na simulação a "bomba" fica ligada só alguns segundos, para o teste ser rápido
        bomba_ate = agora + min(resp.get("duracao_s", 10), 10)
    print(f"umidade={umidade:5.1f}%  bomba={'LIGADA' if resp.get('bomba') else 'desligada'}  ({resp.get('motivo')})")
    time.sleep(INTERVALO_S)
