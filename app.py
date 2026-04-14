import os
import json
from datetime import datetime, timedelta
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

NOME_PLANILHA = "Controle_Despesas"

def conectar_google():
    info = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ])
    return gspread.authorize(creds)

def definir_aba(desc):
    d = desc.upper()
    if "BLUE" in d: return "Blue House"
    elif "UP" in d: return "UP BAR"
    elif "HOUSE" in d: return "House"
    return "Geral"

def salvar(corpo):
    client = conectar_google()
    desc, valor_str, cat = [p.strip() for p in corpo.split(';')]
    valor = float(valor_str.replace(',', '.'))

    sh = client.open(NOME_PLANILHA)
    aba = sh.worksheet(definir_aba(desc))

    data = datetime.now().strftime('%d/%m/%Y')
    aba.append_row([data, desc, valor, cat])

def gerar_relatorio(data_alvo):
    client = conectar_google()
    sh = client.open(NOME_PLANILHA)

    abas = ["Geral", "Blue House", "UP BAR", "House"]
    total_geral = 0
    resposta = f"📊 Relatório {data_alvo}\n\n"

    for nome in abas:
        aba = sh.worksheet(nome)
        dados = aba.get_all_values()

        total = 0
        categorias = {}

        for linha in dados[1:]:
            try:
                data, desc, valor, cat = linha
                if data == data_alvo:
                    valor = float(valor)
                    total += valor
                    categorias[cat] = categorias.get(cat, 0) + valor
            except:
                continue

        if total > 0:
            resposta += f"🏠 {nome}\n"
            for cat, v in categorias.items():
                resposta += f"{cat}: R$ {v:.2f}\n"
            resposta += f"Subtotal: R$ {total:.2f}\n\n"

        total_geral += total

    resposta += f"💰 TOTAL: R$ {total_geral:.2f}"
    return resposta

def enviar_whatsapp(msg):
    client = Client(
        os.environ.get("TWILIO_ACCOUNT_SID"),
        os.environ.get("TWILIO_AUTH_TOKEN")
    )
    client.messages.create(
        body=msg,
        from_='whatsapp:+14155238886',
        to=os.environ.get("SEU_WHATSAPP")
    )

@app.route("/webhook", methods=['POST'])
def webhook():
    corpo = request.values.get('Body', '').strip()
    resp = MessagingResponse()

    if corpo.lower() == "relatorio hoje":
        resp.body(gerar_relatorio(datetime.now().strftime('%d/%m/%Y')))
        return str(resp), 200, {'Content-Type': 'application/xml'}

    if corpo.lower() == "relatorio ontem":
        ontem = datetime.now() - timedelta(days=1)
        resp.body(gerar_relatorio(ontem.strftime('%d/%m/%Y')))
        return str(resp), 200, {'Content-Type': 'application/xml'}

    if ";" not in corpo or len(corpo.split(';')) != 3:
        resp.body("⚠️ Use: Descrição; Valor; Categoria")
        return str(resp), 200, {'Content-Type': 'application/xml'}

    salvar(corpo)

    resp.body("✅ Lançado com sucesso!")
    return str(resp), 200, {'Content-Type': 'application/xml'}

@app.route("/relatorio", methods=["GET"])
def relatorio():
    hoje = datetime.now().strftime('%d/%m/%Y')
    enviar_whatsapp(gerar_relatorio(hoje))
    return "OK"
