import os
import json
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path
from requests.exceptions import RequestException
from dotenv import load_dotenv
import duckdb

# ============================
# CONFIG
# ============================
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_URL = "https://v3.football.api-sports.io/fixtures"

HEADERS = {
    "x-rapidapi-key": API_KEY,
    "x-rapidapi-host": "v3.football.api-sports.io"
}

# ============================
# DATA (D-1)
# ============================
hoje = datetime.now(timezone.utc)
dia_anterior = hoje - timedelta(days=1)

date_str = dia_anterior.strftime("%Y-%m-%d")
ano = dia_anterior.strftime("%Y")
mes = dia_anterior.strftime("%m")
dia = dia_anterior.strftime("%d")

print(f"\n📅 Processando data: {date_str}")

# ============================
# PATHS
# ============================
BRONZE_DIR = Path(f"01-bronze/football/ano={ano}/mes={mes}/dia={dia}")
SILVER_DIR = Path(f"02-silver/football/ano={ano}/mes={mes}/dia={dia}")
GOLD_DIR   = Path(f"03-gold/football/ano={ano}/mes={mes}/dia={dia}")

# idempotência simples (arquivos)
if GOLD_DIR.exists():
    print("⚠️ Gold já existe para esse dia. Abortando pipeline.")
    exit(0)

BRONZE_DIR.mkdir(parents=True, exist_ok=True)
SILVER_DIR.mkdir(parents=True, exist_ok=True)
GOLD_DIR.mkdir(parents=True, exist_ok=True)

# ============================
# RESILIÊNCIA API
# ============================
def fetch_api(retries=3):
    for tentativa in range(1, retries + 1):
        try:
            print(f"🌐 API tentativa {tentativa}/{retries}")
            r = requests.get(
                API_URL,
                headers=HEADERS,
                params={"date": date_str},
                timeout=15
            )
            if r.status_code == 200:
                return r.json()
            print(f"⚠️ Status {r.status_code}")
        except RequestException as e:
            print(f"❌ Erro: {e}")
    return None

data = fetch_api()

if data is None or not data.get("response"):
    print("⚠️ Nenhum jogo encontrado. Pipeline encerrado.")
    exit(0)

# ============================
# BRONZE
# ============================
with open(BRONZE_DIR / "fixtures.json", "w") as f:
    json.dump(data, f, indent=2)

df_bronze = pd.json_normalize(data["response"])
df_bronze.to_parquet(BRONZE_DIR / "fixtures.parquet", index=False)

print("🥉 Bronze OK")

# ============================
# SILVER (1 JOGO = 1 LINHA)
# ============================
df_silver = pd.DataFrame({
    "fixture_id": df_bronze["fixture.id"],
    "date": pd.to_datetime(df_bronze["fixture.date"]),
    "league_name": df_bronze["league.name"],
    "league_country": df_bronze["league.country"],
    "team_home": df_bronze["teams.home.name"],
    "team_away": df_bronze["teams.away.name"],
    "goals_home": df_bronze["goals.home"].fillna(0).astype(int),
    "goals_away": df_bronze["goals.away"].fillna(0).astype(int),
    "year": int(ano),
    "month": int(mes),
    "day": int(dia),
})

df_silver.to_parquet(SILVER_DIR / "silver.parquet", index=False)
print("🥈 Silver OK")

# ============================
# GOLD (SEM AGREGAÇÃO)
# ============================
df_gold = df_silver[
    [
        "league_name",
        "league_country",
        "team_home",
        "team_away",
        "goals_home",
        "goals_away",
    ]
].copy()

df_gold["total_goals"] = df_gold["goals_home"] + df_gold["goals_away"]

df_gold.to_parquet(GOLD_DIR / "gold.parquet", index=False)
print("🥇 Gold OK")

# ============================
# DUCKDB (CAMADA FINAL)
# ============================
DUCKDB_PATH = "football_analytics.duckdb"
con = duckdb.connect(DUCKDB_PATH)

con.execute("""
CREATE TABLE IF NOT EXISTS gold_matches (
    league_name VARCHAR,
    league_country VARCHAR,
    team_home VARCHAR,
    team_away VARCHAR,
    goals_home INTEGER,
    goals_away INTEGER,
    total_goals INTEGER,
    ano INTEGER,
    mes INTEGER,
    dia INTEGER,
    data_partida DATE
);
""")

# idempotência no banco: não insere se o dia já existe
check = con.execute(f"""
    SELECT COUNT(*) 
    FROM gold_matches
    WHERE ano = {ano}
      AND mes = {int(mes)}
      AND dia = {int(dia)}
""").fetchone()[0]

if check > 0:
    print("⚠️ Dados já existem no DuckDB para esse dia. Pulando insert.")
else:
    con.execute(f"""
        INSERT INTO gold_matches
        SELECT
            league_name,
            league_country,
            team_home,
            team_away,
            goals_home,
            goals_away,
            total_goals,
            {ano} AS ano,
            {int(mes)} AS mes,
            {int(dia)} AS dia,
            DATE '{date_str}' AS data_partida
        FROM read_parquet('{GOLD_DIR / "gold.parquet"}')
    """)
    print("🦆 DuckDB atualizado com sucesso")

con.close()

print("✅ PIPELINE FINALIZADO COM SUCESSO")