import os
import json
import threading
import time
import schedule
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials
import logging

# Configurar logging para ver os erros no Render
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

# ==============================
# CONFIGURAÇÕES
# ==============================
NOME_PLANILHA = "Controle_Despesas"
TWILIO_WHATSAPP_FROM = 'whatsapp:+14155238886' # Seu número Twilio WhatsApp

# ==============================
# INICIALIZAÇÃO DO CLIENTE GSPREAD (UMA VEZ APENAS)
# ==============================
def obter_gspread_client():
    try:
        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        if not creds_json:
            logging.error("Variável de ambiente GOOGLE_CREDENTIALS_JSON não encontrada.")
            return None

        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        logging.info("Credenciais do Google Sheets carregadas com sucesso.")
        return gspread.authorize(creds)
    except Exception as e:
        logging.error(f"Erro ao inicializar o cliente gspread: {e}", exc_info=True)
        return None

GSHEET_CLIENT = obter_gspread_client()

if GSHEET_CLIENT is None:
    logging.critical("Cliente gspread não inicializado. Funções de planilha não funcionarão.")

# ==============================
# SALVAR NA PLANILHA
# ==============================
def salvar_na_planilha(corpo):
    if GSHEET_CLIENT is None:
        logging.error("Não foi possível salvar: Cliente gspread não está disponível.")
        return

    try:
        partes = [p.strip() for p in corpo.split(';')]
        desc, valor_str, cat = partes
        valor = float(valor_str.replace(',', '.'))

        sh = GSHEET_CLIENT.open(NOME_PLANILHA)

        d = desc.upper()
        nome_aba = "Geral"

        if "BLUE" in d:
            nome_aba = "Blue House"
        elif "UP" in d:
            nome_aba = "UP BAR"
        elif "HOUSE" in d: # Verifique o nome EXATO da aba no Google Sheets
            nome_aba = "House" # Ou "HOUSE" se for tudo maiúsculo na planilha

        aba = sh.worksheet(nome_aba)

        data = datetime.now().strftime('%d/%m/%Y')

        aba.append_row([data, desc, valor, cat])

        logging.info(f"Salvo com sucesso na aba '{nome_aba}': {desc}, R$ {valor:.2f}, {cat}")

    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"Planilha '{NOME_PLANILHA}' não encontrada. Verifique o nome ou permissões.", exc_info=True)
    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Aba '{nome_aba}' não encontrada na planilha '{NOME_PLANILHA}'. Verifique o nome da aba.", exc_info=True)
    except ValueError:
        logging.error(f"Erro de conversão de valor ao salvar: '{valor_str}' não é um número válido.", exc_info=True)
    except Exception as e:
        logging.error(f"Erro inesperado ao salvar na planilha: {e}", exc_info=True)

# ==============================
# RELATÓRIO DIÁRIO
# ==============================
def enviar_relatorio_diario():
    logging.info("Iniciando envio de relatório diário.")
    if GSHEET_CLIENT is None:
        logging.error("Não foi possível enviar relatório: Cliente gspread não está disponível.")
        return

    try:
        twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        seu_whatsapp = os.environ.get("SEU_WHATSAPP")

        if not all([twilio_account_sid, twilio_auth_token, seu_whatsapp]):
            logging.error("Variáveis de ambiente TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN ou SEU_WHATSAPP não configuradas para o relatório.")
            return

        client_twilio = Client(twilio_account_sid, twilio_auth_token)

        sh = GSHEET_CLIENT.open(NOME_PLANILHA)
        aba = sh.worksheet("Geral") # Assumindo que a aba "Geral" existe e tem todos os dados
        dados = aba.get_all_values()

        hoje = datetime.now().strftime('%d/%m/%Y')

        total = 0
        categorias = {}

        # Pula o cabeçalho
        for linha in dados[1:]:
            try:
                data, desc, valor_str, cat = linha
                if data == hoje:
                    valor = float(valor_str.replace(',', '.')) # Garante que o valor é float
                    total += valor
                    categorias[cat] = categorias.get(cat, 0) + valor
            except ValueError:
                logging.warning(f"Valor não numérico encontrado na linha do relatório: {linha}", exc_info=True)
            except IndexError:
                logging.warning(f"Linha com formato inesperado no relatório: {linha}", exc_info=True)
            except Exception as e:
                logging.error(f"Erro ao processar linha do relatório: {linha} - {e}", exc_info=True)
                continue

        msg = f"📊 Relatório do dia {hoje}\n\n"

        if not categorias:
            msg += "Nenhum lançamento registrado hoje."
        else:
            for cat, v in categorias.items():
                msg += f"{cat}: R$ {v:.2f}\n"
            msg += f"\n💰 Total: R$ {total:.2f}"

        client_twilio.messages.create(
            body=msg,
            from_=TWILIO_WHATSAPP_FROM,
            to=seu_whatsapp
        )

        logging.info("Relatório diário enviado com sucesso!")

    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"Planilha '{NOME_PLANILHA}' não encontrada para o relatório.", exc_info=True)
    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Aba 'Geral' não encontrada na planilha '{NOME_PLANILHA}' para o relatório.", exc_info=True)
    except Exception as e:
        logging.error(f"Erro inesperado no envio do relatório diário: {e}", exc_info=True)

# ==============================
# AGENDADOR
# ==============================
def rodar_agendador():
    # ATENÇÃO: Esta função só é adequada se você tiver um worker separado no Render
    # ou se estiver rodando localmente.
    # No Web Service do Render, pode não ser confiável.
    logging.info("Agendador iniciado. Relatório diário agendado para 23:59.")
    schedule.every().day.at("23:59").do(enviar_relatorio_diario)

    while True:
        schedule.run_pending()
        time.sleep(1) # Reduzido para 1 segundo para maior responsividade do agendador

# ==============================
# WEBHOOK WHATSAPP
# ==============================
@app.route("/webhook", methods=['POST'])
def webhook():
    corpo = request.values.get('Body', '').strip()
    logging.info(f"Webhook recebido: '{corpo}'")

    resp = MessagingResponse()

    if ";" not in corpo or len(corpo.split(';')) != 3:
        resp.body("⚠️ Use: Descrição; Valor; Categoria")
        logging.warning(f"Formato inválido da mensagem: '{corpo}'")
        return str(resp), 200, {'Content-Type': 'application/xml'}

    # Salva em segundo plano para não bloquear a resposta do Twilio
    try:
        threading.Thread(target=salvar_na_planilha, args=(corpo,)).start()
        logging.info(f"Tarefa de salvar na planilha iniciada em segundo plano para: '{corpo}'")
        resp.body("🚀 Recebido! Lançando na planilha...")
    except Exception as e:
        logging.error(f"Erro ao iniciar thread para salvar na planilha: {e}", exc_info=True)
        resp.body("❌ Erro interno ao processar sua solicitação. Tente novamente.")

    return str(resp), 200, {'Content-Type': 'application/xml'}

# ==============================
# MAIN
# ==============================
if __name__ == "__main__":
    # ATENÇÃO: No Render, se você usar um Web Service, rodar o agendador em uma thread
    # pode não ser a melhor prática. O ideal é usar um Cron Job separado para o relatório.
    # Para testes locais, funciona.
    logging.info("Iniciando aplicação Flask.")
    threading.Thread(target=rodar_agendador, daemon=True).start() # daemon=True para a thread morrer com o app
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
