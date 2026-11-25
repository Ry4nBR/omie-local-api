import sqlite3
from datetime import datetime
from typing import Any, Dict, List
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Omie Finanças API - Mock Local")
DB_NAME = "omie_local.db"


# --- 1. MODELO DE REQUISIÇÃO PADRÃO OMIE ---
class OmieRequest(BaseModel):
    call: str
    app_key: str
    app_secret: str
    param: List[Dict[str, Any]] = [{}]


# --- 2. BANCO DE DADOS SQLITE ---
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Tabela de Contas Correntes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS contas_correntes (
            nCodCC INTEGER PRIMARY KEY AUTOINCREMENT,
            descricao TEXT NOT NULL,
            banco TEXT,
            saldo_inicial REAL DEFAULT 0
        )
    """)

    # Tabela Unificada de Títulos / Movimentos
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lancamentos (
            codigo_lancamento_omie INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL, -- 'RECEBER' ou 'PAGAR'
            nCodCC INTEGER,
            cliente_fornecedor TEXT,
            valor_documento REAL NOT NULL,
            data_vencimento TEXT NOT NULL, -- DD/MM/YYYY
            data_pagamento TEXT,           -- DD/MM/YYYY (se liquidado)
            status_titulo TEXT DEFAULT 'ABERTO', -- 'ABERTO' ou 'LIQUIDADO'
            FOREIGN KEY (nCodCC) REFERENCES contas_correntes(nCodCC)
        )
    """)

    # Popula dados iniciais se o banco estiver vazio
    cur.execute("SELECT COUNT(*) FROM contas_correntes")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO contas_correntes (descricao, banco, saldo_inicial) VALUES (?, ?, ?)",
            [
                ("Conta Principal Itaú", "341", 15000.00),
                ("Conta Reserva Nubank", "260", 8500.50),
            ],
        )
        cur.executemany(
            """INSERT INTO lancamentos 
               (tipo, nCodCC, cliente_fornecedor, valor_documento, data_vencimento, data_pagamento, status_titulo) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("RECEBER", 1, "Cliente Alpha Ltda", 4500.00, "10/11/2026", "10/11/2026", "LIQUIDADO"),
                ("PAGAR", 1, "Fornecedor AWS", 1200.00, "15/11/2026", "15/11/2026", "LIQUIDADO"),
                ("RECEBER", 1, "Cliente Beta S/A", 7800.00, "30/11/2026", None, "ABERTO"),
                ("PAGAR", 2, "Aluguel Escritório", 3200.00, "05/12/2026", None, "ABERTO"),
            ],
        )
    conn.commit()
    conn.close()


@app.on_event("startup")
def startup_event():
    init_db()


# --- HELPER DE ERRO PADRÃO OMIE ---
def omie_error(faultstring: str, faultcode: str = "SOAP-ENV:Client-101"):
    return JSONResponse(
        status_code=500,
        content={"faultstring": faultstring, "faultcode": faultcode},
    )


# --- 3. ENDPOINT: CONTAS CORRENTES ---
@app.post("/api/v1/geral/contacorrente/")
def endpoint_conta_corrente(req: OmieRequest):
    conn = get_db()
    cur = conn.cursor()

    if req.call == "ListarContasCorrentes":
        cur.execute("SELECT * FROM contas_correntes")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return {
            "pagina": 1,
            "total_de_paginas": 1,
            "registros": len(rows),
            "total_de_registros": len(rows),
            "ListarContasCorrentes": rows,
        }

    elif req.call == "IncluirContaCorrente":
        p = req.param[0]
        cur.execute(
            "INSERT INTO contas_correntes (descricao, banco, saldo_inicial) VALUES (?, ?, ?)",
            (p.get("descricao", "Nova Conta"), p.get("banco", "000"), p.get("saldo_inicial", 0.0)),
        )
        conn.commit()
        new_id = cur.lastrowid
        conn.close()
        return {"nCodCC": new_id, "cStatus": "0", "cDescricao": "Conta incluída com sucesso"}

    conn.close()
    return omie_error(f"Método '{req.call}' não suportado em contacorrente.")


# --- 4. ENDPOINT: EXTRATO MULTI-CONTAS ---
@app.post("/api/v1/financas/extrato/")
def endpoint_extrato(req: OmieRequest):
    if req.call != "ListarExtrato":
        return omie_error(f"Método '{req.call}' não suportado em extrato.")

    p = req.param[0]
    nCodCC = p.get("nCodCC")
    if not nCodCC:
        return omie_error("Parâmetro nCodCC obrigatório.")

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM contas_correntes WHERE nCodCC = ?", (nCodCC,))
    conta = cur.fetchone()
    if not conta:
        conn.close()
        return omie_error("Conta corrente não encontrada.")

    cur.execute(
        "SELECT * FROM lancamentos WHERE nCodCC = ? AND status_titulo = 'LIQUIDADO'",
        (nCodCC,),
    )
    movs = cur.fetchall()
    conn.close()

    saldo = conta["saldo_inicial"]
    lista_movimentos = []
    for m in movs:
        valor = m["valor_documento"] if m["tipo"] == "RECEBER" else -m["valor_documento"]
        saldo += valor
        lista_movimentos.append({
            "nCodLancamento": m["codigo_lancamento_omie"],
            "dDataLancamento": m["data_pagamento"],
            "cDesCliente": m["cliente_fornecedor"],
            "nValorDocumento": valor,
            "nSaldo": round(saldo, 2),
        })

    return {
        "nCodCC": nCodCC,
        "cDescricao": conta["descricao"],
        "nSaldoAnterior": conta["saldo_inicial"],
        "nSaldoAtual": round(saldo, 2),
        "listaMovimentos": lista_movimentos,
    }


# --- 5. ENDPOINT: CONTAS A RECEBER ---
@app.post("/api/v1/financas/contareceber/")
def endpoint_conta_receber(req: OmieRequest):
    return _gerenciar_titulos(req, tipo="RECEBER", chave_lista="conta_receber_cadastro")


# --- 6. ENDPOINT: CONTAS A PAGAR ---
@app.post("/api/v1/financas/contapagar/")
def endpoint_conta_pagar(req: OmieRequest):
    return _gerenciar_titulos(req, tipo="PAGAR", chave_lista="conta_pagar_cadastro")


def _gerenciar_titulos(req: OmieRequest, tipo: str, chave_lista: str):
    conn = get_db()
    cur = conn.cursor()
    p = req.param[0] if req.param else {}

    if req.call in ["ListarContasReceber", "ListarContasPagar"]:
        cur.execute("SELECT * FROM lancamentos WHERE tipo = ?", (tipo,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return {
            "pagina": 1,
            "total_de_paginas": 1,
            "registros": len(rows),
            "total_de_registros": len(rows),
            chave_lista: rows,
        }

    elif req.call in ["IncluirContaReceber", "IncluirContaPagar"]:
        cur.execute(
            """INSERT INTO lancamentos 
               (tipo, nCodCC, cliente_fornecedor, valor_documento, data_vencimento, status_titulo) 
               VALUES (?, ?, ?, ?, ?, 'ABERTO')""",
            (
                tipo,
                p.get("nCodCC", 1),
                p.get("cliente_fornecedor", "Diversos"),
                p.get("valor_documento", 0.0),
                p.get("data_vencimento", datetime.now().strftime("%d/%m/%Y")),
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
        conn.close()
        return {
            "codigo_lancamento_omie": new_id,
            "codigo_status": "0",
            "descricao_status": "Título incluído com sucesso!",
        }

    elif req.call in ["LancarRecebimento", "LancarPagamento"]:
        cod = p.get("codigo_lancamento_omie")
        data_baixa = p.get("data", datetime.now().strftime("%d/%m/%Y"))
        cur.execute(
            "UPDATE lancamentos SET status_titulo = 'LIQUIDADO', data_pagamento = ? WHERE codigo_lancamento_omie = ? AND tipo = ?",
            (data_baixa, cod, tipo),
        )
        conn.commit()
        conn.close()
        return {"codigo_lancamento_omie": cod, "codigo_status": "0", "descricao_status": "Baixa efetuada!"}

    conn.close()
    return omie_error(f"Método '{req.call}' não suportado.")


# --- 7. ENDPOINTS: FLUXO DE CAIXA (MOVIMENTOS E PESQUISA UNIFICADA) ---
@app.post("/api/v1/financas/mf/")
@app.post("/api/v1/financas/pesquisartitulos/")
def endpoint_fluxo_caixa(req: OmieRequest):
    conn = get_db()
    cur = conn.cursor()

    if req.call in ["ListarMovimentos", "PesquisarLancamentos"]:
        cur.execute("SELECT * FROM lancamentos")
        rows = [dict(r) for r in cur.fetchall()]

        total_receber_aberto = sum(r["valor_documento"] for r in rows if r["tipo"] == "RECEBER" and r["status_titulo"] == "ABERTO")
        total_pagar_aberto = sum(r["valor_documento"] for r in rows if r["tipo"] == "PAGAR" and r["status_titulo"] == "ABERTO")
        total_recebido = sum(r["valor_documento"] for r in rows if r["tipo"] == "RECEBER" and r["status_titulo"] == "LIQUIDADO")
        total_pago = sum(r["valor_documento"] for r in rows if r["tipo"] == "PAGAR" and r["status_titulo"] == "LIQUIDADO")

        conn.close()
        return {
            "pagina": 1,
            "total_de_registros": len(rows),
            "resumo_fluxo": {
                "realizado_entradas": total_recebido,
                "realizado_saidas": total_pago,
                "saldo_realizado_periodo": round(total_recebido - total_pago, 2),
                "previsto_entradas": total_receber_aberto,
                "previsto_saidas": total_pagar_aberto,
                "saldo_previsto_periodo": round(total_receber_aberto - total_pagar_aberto, 2),
            },
            "movimentos": rows,
        }

    conn.close()
    return omie_error(f"Método '{req.call}' não suportado.")