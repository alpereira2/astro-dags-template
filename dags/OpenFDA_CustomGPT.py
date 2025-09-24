# DAG: Coletar eventos OpenFDA (sildenafil) do mês anterior e salvar no Postgres
# Airflow 3.0 (func-style DAG)

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Dict

import pandas as pd
import requests

from airflow.decorators import dag, task
from airflow.hooks.postgres_hook import PostgresHook
from airflow.models.taskinstance import TaskInstance


DESCRIPTION = (
    "Coletar, no 1º dia de cada mês, os eventos OpenFDA do mês anterior desde 2020 e salvar em Postgres"
)


@dag(
    description=DESCRIPTION,
    start_date=datetime(2020, 1, 1),
    schedule="@monthly",
    catchup=True,
    tags=["openfda", "postgres", "monthly"],
)
def openfda_sildenafil_monthly():
    @task(task_id="fetch_openfda")
    def fetch_openfda(**context) -> List[Dict]:
        """
        Buscar dados do mês anterior na OpenFDA, montar DataFrame e enviar via XCom (dict records).
        - Endpoint e query-modelo com count=receivedate
        - Intervalo dinâmico: receivedate:[YYYYMMDD TO YYYYMMDD] do mês imediatamente anterior
        """
        # Em agendamentos mensais do Airflow, o intervalo de dados é o mês anterior:
        # data_interval_start = 1º dia do mês anterior, data_interval_end = 1º dia do mês corrente.
        data_interval_start: datetime = context["data_interval_start"]
        data_interval_end: datetime = context["data_interval_end"]

        # Início = 1º dia do mês anterior (YYYYMMDD)
        inicio = data_interval_start.strftime("%Y%m%d")
        # Fim = último dia do mês anterior = (data_interval_end - 1 dia)
        last_day_prev_month = (data_interval_end - timedelta(days=1)).strftime("%Y%m%d")

        # Monta a busca para o medicamento e intervalo de receivedate
        # Exemplo de padrão:
        # https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:"sildenafil citrate"
        #   AND receivedate:[{inicio} TO {fim}]&count=receivedate
        base_url = "https://api.fda.gov/drug/event.json"
        search_query = (
            'patient.drug.medicinalproduct:"sildenafil citrate" '
            f'AND receivedate:[{inicio} TO {last_day_prev_month}]'
        )
        params = {
            "search": search_query,
            "count": "receivedate",
        }

        resp = requests.get(base_url, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()

        # A resposta com count=receivedate costuma vir como [{"time": "YYYYMMDD", "count": N}, ...]
        results = payload.get("results", [])
        if not isinstance(results, list):
            results = []

        # Converte para DataFrame com colunas ['receivedate', 'count']
        df = pd.DataFrame(results)
        if not df.empty:
            # Renomeia 'time' -> 'receivedate' se necessário
            if "time" in df.columns and "receivedate" not in df.columns:
                df = df.rename(columns={"time": "receivedate"})
            # Garante ordem e tipos
            df = df[["receivedate", "count"]].copy()
            df["receivedate"] = df["receivedate"].astype(str)
            df["count"] = pd.to_numeric(df["count"], errors="coerce").fillna(0).astype(int)
        else:
            df = pd.DataFrame(columns=["receivedate", "count"])

        # Envia via XCom como lista de dicts (records)
        return df.to_dict(orient="records")

    @task(task_id="save_to_postgres")
    def save_to_postgres(records: List[Dict]):
        """
        Ler o DataFrame do XCom e gravar no Postgres na tabela defaultdb.openfda_events_sildenafil
        - Conexão Airflow id=postgres
        - if_exists=append, chunksize=5000
        """
        df = pd.DataFrame.from_records(records, columns=["receivedate", "count"])
        # Mesmo vazio, vamos garantir o schema correto
        if df.empty:
            df = pd.DataFrame(columns=["receivedate", "count"])

        hook = PostgresHook(postgres_conn_id="postgres")
        engine = hook.get_sqlalchemy_engine()
        with engine.begin() as conn:
            df.to_sql(
                name="openfda_events_sildenafil",
                con=conn,
                schema="defaultdb",
                if_exists="append",
                index=False,
                chunksize=5000,
                method=None,
            )

    # Orquestração: fetch_openfda >> save_to_postgres
    fetched = fetch_openfda()
    save_to_postgres(fetched)


dag = openfda_sildenafil_monthly()
