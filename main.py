"""Mock local da Omie para dados fictícios. Não valida credenciais nem emite documentos fiscais reais."""

import sqlite3
import json
import re
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from uuid import UUID
from datetime import datetime
from typing import Any, Dict, List
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Omie API - Mock Local (Finanças, Clientes & OS)")
DB_NAME = "omie_local.db"
DEMO_TIMEOUT_SECONDS = 11
DEMO_OS_KEY = "TSB-11111111111111111111111111111111-202610"


# --- 1. MODELO DE REQUISIÇÃO PADRÃO OMIE ---
class OmieRequest(BaseModel):
    call: str
    app_key: str
    app_secret: str
    param: List[Dict[str, Any]] = Field(default_factory=lambda: [{}])


# --- 2. BANCO DE DADOS SQLITE ---
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=15)
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

    # Tabela de Títulos / Movimentos Financeiros
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lancamentos (
            codigo_lancamento_omie INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            nCodCC INTEGER,
            cliente_fornecedor TEXT,
            valor_documento REAL NOT NULL,
            data_vencimento TEXT NOT NULL,
            data_pagamento TEXT,
            status_titulo TEXT DEFAULT 'ABERTO',
            FOREIGN KEY (nCodCC) REFERENCES contas_correntes(nCodCC)
        )
    """)

    # Tabela de Clientes (para ListarClientesResumido)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clientes (
            codigo_cliente_omie INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo_cliente_integracao TEXT,
            razao_social TEXT NOT NULL,
            nome_fantasia TEXT,
            cnpj_cpf TEXT,
            importado_api TEXT DEFAULT 'N'
        )
    """)

    # Tabela de Ordens de Serviço (para ConsultarOS)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ordens_servico (
            nCodOS INTEGER PRIMARY KEY AUTOINCREMENT,
            cCodIntOS TEXT UNIQUE NOT NULL,
            cEtapa TEXT DEFAULT '10',
            nCodCli INTEGER,
            nValorTotal REAL DEFAULT 0
        )
    """)

    # Contrato experimental exclusivo do mock. Não é uma NFS-e real.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS documentos_demo (
            operation_key TEXT PRIMARY KEY,
            request_json TEXT NOT NULL,
            document_json TEXT NOT NULL
        )
    """)

    # Seed Contas Correntes
    cur.execute("SELECT COUNT(*) FROM contas_correntes")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO contas_correntes (descricao, banco, saldo_inicial) VALUES (?, ?, ?)",
            [
                ("Conta Principal Itaú", "341", 15000.00),
                ("Conta Reserva Nubank", "260", 8500.50),
            ],
        )

    # Seed Lançamentos Financeiros
    cur.execute("SELECT COUNT(*) FROM lancamentos")
    if cur.fetchone()[0] == 0:
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

    # Seed Clientes
    cur.execute("SELECT COUNT(*) FROM clientes")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            """INSERT INTO clientes
               (codigo_cliente_integracao, razao_social, nome_fantasia, cnpj_cpf, importado_api)
               VALUES (?, ?, ?, ?, ?)""",
            [
                ("CLI-001", "Cliente Alpha Tecnologia Ltda", "Alpha Tech", "12.345.678/0001-99", "N"),
                ("CLI-002", "Cliente Beta Serviços S/A", "Beta Corp", "98.765.432/0001-10", "N"),
            ],
        )

    # Seed Ordens de Serviço (formato TSB-{32 hex}-{AAAAMM})
    cur.execute("SELECT COUNT(*) FROM ordens_servico")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO ordens_servico (cCodIntOS, cEtapa, nCodCli, nValorTotal) VALUES (?, ?, ?, ?)",
            [
                ("TSB-0123456789abcdef0123456789abcdef-202611", "20", 1, 4500.00),
            ],
        )

    # Fixture isolada do contrato de leitura; preserva OS e dados antigos.
    cur.execute(
        "INSERT OR IGNORE INTO ordens_servico (cCodIntOS, cEtapa, nCodCli, nValorTotal) VALUES (?, ?, ?, ?)",
        (DEMO_OS_KEY, "50", 1, 8099.64),
    )
    conn.commit()
    conn.close()


@app.on_event("startup")
def startup_event():
    init_db()


def omie_error(faultstring: str, faultcode: str = "SOAP-ENV:Client-101"):
    return JSONResponse(
        status_code=500,
        content={"faultstring": faultstring, "faultcode": faultcode},
    )


def get_first_param(req: OmieRequest) -> Dict[str, Any]:
    return req.param[0] if req.param else {}


# --- 3. ENDPOINT: CLIENTES (Novo - exigido pelo Probe do TSBilling) ---
@app.post("/api/v1/geral/clientes/")
def endpoint_clientes(req: OmieRequest):
    p = get_first_param(req)
    conn = get_db()
    cur = conn.cursor()

    if req.call == "ListarClientesResumido":
        try:
            pagina = int(p.get("pagina", 1))
            registros_por_pagina = int(p.get("registros_por_pagina", 50))
            if pagina < 1 or registros_por_pagina < 1:
                raise ValueError
        except (TypeError, ValueError):
            conn.close()
            return omie_error("Página e registros_por_pagina devem ser inteiros positivos.")
        apenas_importado = p.get("apenas_importado_api")

        query = "SELECT codigo_cliente_omie, codigo_cliente_integracao, razao_social, nome_fantasia, cnpj_cpf FROM clientes"
        params = []

        if apenas_importado in ("S", "N"):
            query += " WHERE importado_api = ?"
            params.append(apenas_importado)

        query += " ORDER BY codigo_cliente_omie"
        cur.execute(query, params)
        all_rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        total_registros = len(all_rows)
        total_paginas = max(1, (total_registros + registros_por_pagina - 1) // registros_por_pagina)
        inicio = (pagina - 1) * registros_por_pagina
        fim = inicio + registros_por_pagina
        paginados = all_rows[inicio:fim]

        return {
            "pagina": pagina,
            "total_de_paginas": total_paginas,
            "registros": len(paginados),
            "total_de_registros": total_registros,
            "clientes_cadastro_resumido": paginados,
        }

    conn.close()
    return omie_error(f"Método '{req.call}' não suportado em clientes.")


# --- 4. ENDPOINT: ORDENS DE SERVIÇO (Novo - exigido por ConsultarOS) ---
@app.post("/api/v1/servicos/os/")
def endpoint_os(req: OmieRequest):
    if req.call == "CriarDocumentoDemo":
        return criar_documento_demo(get_first_param(req))
    if req.call == "ConsultarDocumentoDemo":
        return consultar_documento_demo(get_first_param(req))
    p = get_first_param(req)
    conn = get_db()
    cur = conn.cursor()

    if req.call == "ConsultarOS":
        cod_int_os = p.get("cCodIntOS")
        cod_os = p.get("nCodOS")

        if cod_int_os:
            cur.execute("SELECT * FROM ordens_servico WHERE cCodIntOS = ?", (cod_int_os,))
        elif cod_os:
            cur.execute("SELECT * FROM ordens_servico WHERE nCodOS = ?", (cod_os,))
        else:
            conn.close()
            return omie_error("Informe cCodIntOS ou nCodOS para consulta.")

        os_row = cur.fetchone()
        conn.close()

        if not os_row:
            return omie_error("Ordem de Serviço não cadastrada!", faultcode="SOAP-ENV:Client-105")

        return {
            "Cabecalho": {
                "cCodIntOS": os_row["cCodIntOS"],
                "nCodOS": os_row["nCodOS"],
                "cEtapa": os_row["cEtapa"],
                "nCodCli": os_row["nCodCli"],
                "nValorTotal": os_row["nValorTotal"],
            }
        }

    conn.close()
    return omie_error(f"Método '{req.call}' não suportado em servicos/os.")


# --- 5. ENDPOINT: CONTAS CORRENTES ---
@app.post("/api/v1/geral/contacorrente/")
def endpoint_conta_corrente(req: OmieRequest):
    p = get_first_param(req)
    conn = get_db()
    cur = conn.cursor()

    if req.call == "ListarContasCorrentes":
        cur.execute("SELECT * FROM contas_correntes ORDER BY nCodCC")
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


# --- 6. ENDPOINT: EXTRATO MULTI-CONTAS ---
@app.post("/api/v1/financas/extrato/")
def endpoint_extrato(req: OmieRequest):
    if req.call != "ListarExtrato":
        return omie_error(f"Método '{req.call}' não suportado em extrato.")

    p = get_first_param(req)
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


# --- 7. ENDPOINTS: CONTAS A RECEBER E PAGAR ---
@app.post("/api/v1/financas/contareceber/")
def endpoint_conta_receber(req: OmieRequest):
    return _gerenciar_titulos(req, tipo="RECEBER", chave_lista="conta_receber_cadastro")


@app.post("/api/v1/financas/contapagar/")
def endpoint_conta_pagar(req: OmieRequest):
    return _gerenciar_titulos(req, tipo="PAGAR", chave_lista="conta_pagar_cadastro")


def _gerenciar_titulos(req: OmieRequest, tipo: str, chave_lista: str):
    p = get_first_param(req)
    listar, incluir, baixar = (
        ("ListarContasReceber", "IncluirContaReceber", "LancarRecebimento")
        if tipo == "RECEBER"
        else ("ListarContasPagar", "IncluirContaPagar", "LancarPagamento")
    )
    conn = get_db()
    try:
        cur = conn.cursor()
        if req.call == listar:
            cur.execute("SELECT * FROM lancamentos WHERE tipo = ? ORDER BY codigo_lancamento_omie", (tipo,))
            rows = [dict(r) for r in cur.fetchall()]
            return {
                "pagina": 1,
                "total_de_paginas": 1,
                "registros": len(rows),
                "total_de_registros": len(rows),
                chave_lista: rows,
            }

        if req.call == incluir:
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
            return {
                "codigo_lancamento_omie": cur.lastrowid,
                "codigo_status": "0",
                "descricao_status": "Título incluído com sucesso!",
            }

        if req.call == baixar:
            cod = p.get("codigo_lancamento_omie")
            if not cod:
                return omie_error("Parâmetro codigo_lancamento_omie obrigatório.")
            data_baixa = p.get("data", datetime.now().strftime("%d/%m/%Y"))
            cur.execute(
                "SELECT status_titulo FROM lancamentos WHERE codigo_lancamento_omie = ? AND tipo = ?",
                (cod, tipo),
            )
            titulo = cur.fetchone()
            if titulo is None:
                return omie_error("Título não encontrado.")
            if titulo["status_titulo"] != "LIQUIDADO":
                cur.execute(
                    """UPDATE lancamentos
                       SET status_titulo = 'LIQUIDADO', data_pagamento = ?
                       WHERE codigo_lancamento_omie = ? AND tipo = ?""",
                    (data_baixa, cod, tipo),
                )
                conn.commit()
            return {
                "codigo_lancamento_omie": cod,
                "codigo_status": "0",
                "descricao_status": "Baixa efetuada!",
            }

        return omie_error(f"Método '{req.call}' não suportado.")
    finally:
        conn.close()


# --- 8. ENDPOINTS: FLUXO DE CAIXA ---
@app.post("/api/v1/financas/mf/")
@app.post("/api/v1/financas/pesquisartitulos/")
def endpoint_fluxo_caixa(req: OmieRequest):
    if req.call not in ("ListarMovimentos", "PesquisarLancamentos"):
        return omie_error(f"Método '{req.call}' não suportado.")

    conn = get_db()
    try:
        rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM lancamentos ORDER BY codigo_lancamento_omie"
            ).fetchall()
        ]
    finally:
        conn.close()

    def total(tipo: str, status: str) -> float:
        return round(sum(
            r["valor_documento"] for r in rows
            if r["tipo"] == tipo and r["status_titulo"] == status
        ), 2)

    total_receber_aberto = total("RECEBER", "ABERTO")
    total_pagar_aberto = total("PAGAR", "ABERTO")
    total_recebido = total("RECEBER", "LIQUIDADO")
    total_pago = total("PAGAR", "LIQUIDADO")

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


# --- 9. CONTRATO EXPERIMENTAL DE DOCUMENTOS FISCAIS FICTÍCIOS ---
# Calls propostas em REQUISITOS_MOCK_OMIE.md, seção 7.1.
# NÃO são métodos oficiais da Omie nem são consumidas pelo TSBilling atual.
# Não há emissão real, autorização municipal, cálculo de tributos ou transferência.
# O mock usa os totais de teste recebidos; o simulador independente do TSBilling
# continua responsável pelo recálculo a partir das faixas e quantidades.

SCENARIOS = {"COMPATIVEL", "DIVERGENTE", "PENDENTE", "INCOMPLETO", "CANCELADA", "TIMEOUT"}


def demo_error(status: int, code: str, message: str):
    return JSONResponse(status_code=status, content={"faultcode": code, "faultstring": message})


def valid_key(value):
    return isinstance(value, str) and re.fullmatch(r"TSB-[0-9a-fA-F]{32}-[0-9]{4}(?:0[1-9]|1[0-2])", value) is not None


def required_string(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("String obrigatória")
    return value


def uuid_string(value):
    return str(UUID(required_string(value)))


def cents(value):
    # O contrato utiliza números JSON; não aceita strings, booleanos ou NaN.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Valor monetário inválido")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount > Decimal("999999999999.99"):
        raise ValueError("Valor monetário fora do intervalo")
    scaled = amount * 100
    if scaled != scaled.to_integral_value():
        raise ValueError("Valor monetário deve ter até duas casas decimais")
    return int(scaled)


def money(value):
    # Inteiros em centavos para toda a aritmética e comparação; número JSON na saída.
    return float(Decimal(value) / Decimal(100))


def valid_cnpj(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{14}", value) or len(set(value)) == 1:
        return False
    def digit(chars, weights):
        rest = sum(int(n) * w for n, w in zip(chars, weights)) % 11
        return str(0 if rest < 2 else 11 - rest)
    return (digit(value[:12], (5,4,3,2,9,8,7,6,5,4,3,2)) == value[12]
            and digit(value[:13], (6,5,4,3,2,9,8,7,6,5,4,3,2)) == value[13])


def normalize_demo(p):
    key = p.get("operationKey")
    if not valid_key(key):
        raise ValueError("Chave inválida")
    invoice = uuid_string(p.get("invoiceId"))
    competence = required_string(p.get("competence"))
    if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", competence):
        raise ValueError("Competência inválida")
    if key != f"TSB-{UUID(invoice).hex}-{competence.replace('-', '')}":
        raise ValueError("Chave incompatível com fatura e competência")
    cnpj = p.get("cnpj")
    if not valid_cnpj(cnpj):
        raise ValueError("CNPJ de teste inválido")
    if p.get("currency") != "BRL" or p.get("valueBasis") != "BRUTO":
        raise ValueError("Moeda ou base não suportada neste contrato experimental")
    scenario = p.get("scenario", "COMPATIVEL")
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise ValueError("Cenário inválido")
    raw_items = p.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Itens obrigatórios")
    items = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("Item inválido")
        service = uuid_string(raw.get("serviceId"))
        quantity = raw.get("quantity")
        if service in seen or type(quantity) is not int or quantity < 0:
            raise ValueError("Serviço duplicado ou quantidade inválida")
        seen.add(service)
        items.append({"serviceId": service, "serviceCode": required_string(raw.get("serviceCode")),
                      "quantity": quantity, "totalCents": cents(raw.get("total"))})
    return {"operationKey": key, "invoiceId": invoice,
            "organizationId": uuid_string(p.get("organizationId")),
            "clientId": uuid_string(p.get("clientId")),
            "snapshotHash": required_string(p.get("snapshotHash")), "cnpj": cnpj,
            "competence": competence, "currency": "BRL", "valueBasis": "BRUTO",
            "scenario": scenario, "items": items}


def build_demo(data):
    scenario = data["scenario"]
    items = [dict(item) for item in data["items"]]
    if scenario == "DIVERGENTE":
        items[0]["totalCents"] += 1
    total = sum(item["totalCents"] for item in items)
    doc = {key: value for key, value in data.items() if key not in ("scenario", "items")}
    doc.update(mode="SIMULATED", status="AUTORIZADA",
               number="DEMO-" + UUID(data["invoiceId"]).hex,
               total=money(total),
               items=[{**{k: v for k, v in item.items() if k != "totalCents"},
                       "total": money(item["totalCents"])} for item in items])
    if scenario == "PENDENTE":
        doc.update(status="PENDENTE", number=None)
    elif scenario == "CANCELADA":
        doc.update(status="CANCELADA")
    elif scenario == "INCOMPLETO":
        doc.update(total=None, items=None)
    return doc


def criar_documento_demo(p):
    try:
        data = normalize_demo(p)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return demo_error(422, "MOCK_INVALID", "Dados de teste inválidos")
    key = data["operationKey"]
    request_json = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    created = False
    # BEGIN IMMEDIATE serializa criação por chave, inclusive entre processos.
    # Nunca manter a transação aberta durante a simulação de timeout.
    with closing(get_db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT request_json, document_json FROM documentos_demo WHERE operation_key = ?", (key,)).fetchone()
        if row is not None:
            if row["request_json"] != request_json:
                return demo_error(409, "MOCK_DUPLICATE", "Chave já utilizada com dados diferentes")
            doc = json.loads(row["document_json"])
        else:
            doc = build_demo(data)
            conn.execute("INSERT INTO documentos_demo (operation_key, request_json, document_json) VALUES (?, ?, ?)",
                         (key, request_json, json.dumps(doc, ensure_ascii=False)))
            created = True
    if created and data["scenario"] == "TIMEOUT":
        # O cliente atual usa 10 s. O registro já está persistido antes do atraso.
        time.sleep(DEMO_TIMEOUT_SECONDS)
    return {"document": doc}


def consultar_documento_demo(p):
    key = p.get("operationKey")
    if not valid_key(key):
        return demo_error(422, "MOCK_INVALID", "Dados de teste inválidos")
    with closing(get_db()) as conn:
        row = conn.execute("SELECT document_json FROM documentos_demo WHERE operation_key = ?", (key,)).fetchone()
    if row is None:
        return demo_error(404, "MOCK_NOT_FOUND", "Documento de teste inexistente")
    return {"document": json.loads(row["document_json"])}
