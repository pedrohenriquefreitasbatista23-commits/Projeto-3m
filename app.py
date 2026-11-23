"""
Irrigação inteligente - servidor Flask + banco SQLite + painel web.

Fluxo:
  ESP32 (sensor de umidade) --POST /api/leitura--> servidor
  servidor salva a leitura, decide se liga a bomba e responde para o ESP32
  painel web (/) mostra tudo e permite configurar / acionar manualmente

Rodar:
  pip install -r requirements.txt
  python app.py
  abrir http://127.0.0.1:5000
"""
import hmac
import os
import sqlite3
import time

from flask import Flask, g, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "irrigacao.db")

# Chave que o ESP32 manda no cabeçalho X-API-Key. Troque por uma sua!
# Dá para definir sem mexer no código:  export API_KEY="minha-chave"
API_KEY = os.environ.get("API_KEY", "troque-essa-chave")

# Valores padrão das configurações (o tipo do padrão define o tipo salvo)
PADROES = {
    "modo": "auto",            # auto = servidor decide | manual = só pelo painel
    "umidade_min": 35.0,       # abaixo disso, liga a bomba (%)
    "umidade_max": 65.0,       # faixa ideal termina aqui (%)
    "duracao_s": 30.0,         # quanto tempo a bomba fica ligada por irrigação
    "intervalo_min_s": 600.0,  # espera mínima entre irrigações (a água demora a infiltrar)
    "vazao_l_min": 2.0,        # vazão da bomba, para estimar litros gastos
    "raw_seco": 3200.0,        # leitura bruta do sensor com ele no ar/solo seco
    "raw_molhado": 1400.0,     # leitura bruta do sensor dentro d'água
}

app = Flask(__name__)


# ----------------------------------------------------------------------------
# Banco de dados
# ----------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def fechar_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS leituras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            umidade REAL NOT NULL,
            raw INTEGER,
            temperatura REAL
        );
        CREATE INDEX IF NOT EXISTS idx_leituras_ts ON leituras(ts);

        CREATE TABLE IF NOT EXISTS irrigacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            duracao_s INTEGER NOT NULL,
            origem TEXT NOT NULL,
            umidade_antes REAL
        );

        CREATE TABLE IF NOT EXISTS config (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
        """
    )
    for chave, valor in PADROES.items():
        db.execute("INSERT OR IGNORE INTO config VALUES (?, ?)", (chave, str(valor)))
    db.execute("INSERT OR IGNORE INTO config VALUES ('comando', '')")
    db.commit()
    db.close()


def carregar_config(db):
    linhas = {r["chave"]: r["valor"] for r in db.execute("SELECT chave, valor FROM config")}
    cfg = {}
    for chave, padrao in PADROES.items():
        valor = linhas.get(chave, str(padrao))
        cfg[chave] = valor if isinstance(padrao, str) else float(valor)
    return cfg


def comando_pendente(db):
    linha = db.execute("SELECT valor FROM config WHERE chave = 'comando'").fetchone()
    return linha["valor"] if linha else ""


def definir_comando(db, valor):
    db.execute("INSERT OR REPLACE INTO config VALUES ('comando', ?)", (valor,))


# ----------------------------------------------------------------------------
# Lógica de irrigação
# ----------------------------------------------------------------------------
def raw_para_pct(raw, cfg):
    """Converte a leitura bruta do sensor capacitivo em % de umidade."""
    seco, molhado = cfg["raw_seco"], cfg["raw_molhado"]
    if seco == molhado:
        return 0.0
    pct = (seco - raw) / (seco - molhado) * 100
    return max(0.0, min(100.0, pct))


def fim_ultima_irrigacao(db):
    linha = db.execute("SELECT MAX(ts + duracao_s) AS fim FROM irrigacoes").fetchone()
    return linha["fim"] or 0


def registrar_irrigacao(db, agora, duracao_s, origem, umidade):
    db.execute(
        "INSERT INTO irrigacoes (ts, duracao_s, origem, umidade_antes) VALUES (?, ?, ?, ?)",
        (agora, duracao_s, origem, umidade),
    )


def decidir(db, cfg, umidade, agora):
    """Decide se a bomba liga. A resposta vai direto para o ESP32."""
    duracao = int(cfg["duracao_s"])

    # 1) comando vindo do painel tem prioridade
    comando = comando_pendente(db)
    if comando:
        definir_comando(db, "")
        if comando == "ligar":
            registrar_irrigacao(db, agora, duracao, "manual", umidade)
            return {"bomba": True, "duracao_s": duracao, "motivo": "comando manual"}
        return {"bomba": False, "duracao_s": 0, "motivo": "parada pelo painel"}

    # 2) modo manual: o servidor não decide nada sozinho
    if cfg["modo"] != "auto":
        return {"bomba": False, "duracao_s": 0, "motivo": "modo manual"}

    # 3) modo automático
    if umidade < cfg["umidade_min"]:
        if agora - fim_ultima_irrigacao(db) < cfg["intervalo_min_s"]:
            return {"bomba": False, "duracao_s": 0, "motivo": "aguardando a água infiltrar"}
        registrar_irrigacao(db, agora, duracao, "automatica", umidade)
        return {"bomba": True, "duracao_s": duracao, "motivo": "solo abaixo do mínimo"}

    return {"bomba": False, "duracao_s": 0, "motivo": "solo úmido o suficiente"}


def prever_horas_ate_minimo(db, cfg, agora):
    """
    Previsão simples de tendência: regressão linear nas leituras das últimas
    6 horas (depois da última irrigação) para estimar quando o solo chega no
    mínimo. Retorna horas (float) ou None se não há tendência de secagem.
    Quando você tiver dados reais, dá para trocar isso por um modelo de
    machine learning (ver README).
    """
    inicio = max(agora - 6 * 3600, fim_ultima_irrigacao(db) + 300)
    pts = db.execute(
        "SELECT ts, umidade FROM leituras WHERE ts >= ? ORDER BY ts", (inicio,)
    ).fetchall()
    if len(pts) < 5 or pts[-1]["ts"] - pts[0]["ts"] < 600:
        return None

    xs = [(p["ts"] - pts[0]["ts"]) / 3600 for p in pts]  # horas
    ys = [p["umidade"] for p in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var == 0:
        return None
    inclinacao = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var  # % por hora
    if inclinacao > -0.05:
        return None

    atual = ys[-1]
    if atual <= cfg["umidade_min"]:
        return 0.0
    return (atual - cfg["umidade_min"]) / -inclinacao


# ----------------------------------------------------------------------------
# API para o ESP32
# ----------------------------------------------------------------------------
def erro(mensagem, codigo=400):
    return jsonify(erro=mensagem), codigo


@app.post("/api/leitura")
def receber_leitura():
    chave = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(chave, API_KEY):
        return erro("chave de API inválida", 401)

    dados = request.get_json(silent=True) or {}
    db = get_db()
    cfg = carregar_config(db)

    raw = None
    try:
        if "umidade" in dados:
            umidade = max(0.0, min(100.0, float(dados["umidade"])))
        elif "raw" in dados:
            raw = int(dados["raw"])
            umidade = raw_para_pct(raw, cfg)
        else:
            return erro("envie 'umidade' (%) ou 'raw' (leitura bruta)")
        temperatura = float(dados["temperatura"]) if "temperatura" in dados else None
    except (TypeError, ValueError):
        return erro("valores numéricos inválidos")

    agora = int(time.time())
    db.execute(
        "INSERT INTO leituras (ts, umidade, raw, temperatura) VALUES (?, ?, ?, ?)",
        (agora, umidade, raw, temperatura),
    )
    resposta = decidir(db, cfg, umidade, agora)
    db.commit()
    resposta["umidade"] = round(umidade, 1)
    return jsonify(resposta)


# ----------------------------------------------------------------------------
# API para o painel
# ----------------------------------------------------------------------------
@app.get("/api/status")
def status():
    db = get_db()
    cfg = carregar_config(db)
    agora = int(time.time())

    ultima = db.execute("SELECT * FROM leituras ORDER BY ts DESC LIMIT 1").fetchone()
    ultima_irr = db.execute("SELECT * FROM irrigacoes ORDER BY ts DESC LIMIT 1").fetchone()
    segundos_24h = db.execute(
        "SELECT COALESCE(SUM(duracao_s), 0) AS s FROM irrigacoes WHERE ts >= ?",
        (agora - 86400,),
    ).fetchone()["s"]

    return jsonify(
        agora=agora,
        leitura=dict(ultima) if ultima else None,
        online=bool(ultima and agora - ultima["ts"] < 120),
        config=cfg,
        comando=comando_pendente(db),
        previsao_horas=prever_horas_ate_minimo(db, cfg, agora),
        ultima_irrigacao=dict(ultima_irr) if ultima_irr else None,
        irrigacoes_24h=db.execute(
            "SELECT COUNT(*) AS n FROM irrigacoes WHERE ts >= ?", (agora - 86400,)
        ).fetchone()["n"],
        litros_24h=round(segundos_24h / 60 * cfg["vazao_l_min"], 2),
    )


@app.get("/api/leituras")
def leituras():
    try:
        horas = max(1, min(int(request.args.get("horas", 24)), 24 * 30))
    except ValueError:
        return erro("horas inválido")
    desde = int(time.time()) - horas * 3600
    linhas = get_db().execute(
        "SELECT ts, umidade, temperatura FROM leituras WHERE ts >= ? ORDER BY ts", (desde,)
    ).fetchall()
    passo = max(1, len(linhas) // 600)  # no máximo ~600 pontos no gráfico
    return jsonify([dict(l) for l in linhas[::passo]])


@app.get("/api/irrigacoes")
def irrigacoes():
    linhas = get_db().execute(
        "SELECT ts, duracao_s, origem, umidade_antes FROM irrigacoes ORDER BY ts DESC LIMIT 20"
    ).fetchall()
    return jsonify([dict(l) for l in linhas])


@app.post("/api/comando")
def comando():
    acao = (request.get_json(silent=True) or {}).get("acao")
    if acao not in ("ligar", "desligar"):
        return erro("acao deve ser 'ligar' ou 'desligar'")
    db = get_db()
    definir_comando(db, acao)
    db.commit()
    return jsonify(ok=True, comando=acao)


@app.post("/api/config")
def salvar_config():
    dados = request.get_json(silent=True) or {}
    db = get_db()
    novo = carregar_config(db)

    for chave in PADROES:
        if chave not in dados:
            continue
        if chave == "modo":
            if dados[chave] not in ("auto", "manual"):
                return erro("modo deve ser 'auto' ou 'manual'")
            novo[chave] = dados[chave]
        else:
            try:
                novo[chave] = float(dados[chave])
            except (TypeError, ValueError):
                return erro(f"valor inválido para {chave}")

    if not (0 <= novo["umidade_min"] < novo["umidade_max"] <= 100):
        return erro("a umidade mínima deve ser menor que a máxima (0 a 100)")
    if not (1 <= novo["duracao_s"] <= 600):
        return erro("duração deve ficar entre 1 e 600 segundos")
    if novo["intervalo_min_s"] < 0 or novo["vazao_l_min"] <= 0:
        return erro("intervalo e vazão precisam ser positivos")
    if novo["raw_seco"] == novo["raw_molhado"]:
        return erro("calibração: raw seco e raw molhado não podem ser iguais")

    for chave, valor in novo.items():
        db.execute("INSERT OR REPLACE INTO config VALUES (?, ?)", (chave, str(valor)))
    db.commit()
    return jsonify(ok=True, config=novo)


@app.get("/")
def painel():
    return render_template("index.html")


init_db()

if __name__ == "__main__":
    # host 0.0.0.0 deixa o ESP32 (na mesma rede Wi-Fi) acessar pelo IP do seu PC
    app.run(host="0.0.0.0", port=5000, debug=True)
