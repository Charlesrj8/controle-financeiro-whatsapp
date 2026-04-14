import os
import json
import threading
import time
import re
import logging
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import SpreadsheetNotFound, WorksheetNotFound, APIError
import unicodedata

# Configurar logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ==============================
# CONFIGURAÇÕES
# ==============================
NOME_PLANILHA = "Controle_Despesas"
TWILIO_WHATSAPP_FROM = os.environ.get('TWILIO_WHATSAPP_FROM', 'whatsapp:+14155238886') # Seu número Twilio WhatsApp

# ==============================
# FUNÇÕES AUXILIARES
# ==============================
def normalize_text(text):
    """Remove acentos e converte para minúsculas para comparação."""
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')
    return text.lower()

def parse_float_value(value_str):
    """Converte string para float, aceitando ',' ou '.' como decimal."""
    if not isinstance(value_str, str):
        raise ValueError("O valor não é uma string.")

    # Remove 'R$', 'r$', espaços, e pontos de milhar
    cleaned_value = value_str.replace('R$', '').replace('r$', '').strip().replace('.', '').replace(',', '.')

    # Verifica se o resultado é um número válido
    if not re.match(r"^-?\d+(\.\d+)?$", cleaned_value):
        raise ValueError(f"Formato de valor inválido: '{value_str}'")

    return float(cleaned_value)

def retry_gspread_operation(func, *args, **kwargs):
    """Tenta executar uma operação gspread com retry exponencial."""
    max_retries = 3
    base_delay = 1 # segundos

    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            if e.response.status_code in [403, 429, 500, 502, 503, 504]:
                delay = base_delay * (2 ** i)
                logger.warning(f"Erro gspread API (status {e.response.status_code}), tentando novamente em {delay}s. Tentativa {i+1}/{max_retries}", exc_info=True)
                time.sleep(delay)
            else:
                raise # Outros erros API não são retentáveis
        except Exception as e:
            logger.error(f"Erro inesperado durante operação gspread. Tentativa {i+1}/{max_retries}", exc_info=True)
            raise # Erros não-API são re-lançados imediatamente
    raise Exception(f"Falha após {max_retries} tentativas na operação gspread.")

# ==============================
# INICIALIZAÇÃO DO CLIENTE GSPREAD
# ==============================
def obter_gspread_client():
    """Inicializa e retorna o cliente gspread."""
    try:
        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        if not creds_json:
            logger.critical("Variável de ambiente GOOGLE_CREDENTIALS_JSON não encontrada.")
            return None

        info = json.loads(creds_json)

        # Validação básica das credenciais
        if not all(k in info for k in ["type", "project_id", "private_key_id", "private_key", "client_email", "client_id", "auth_uri", "token_uri", "auth_provider_x509_cert_url", "client_x509_cert_url"]):
            logger.critical("JSON de credenciais do Google incompleto ou inválido.")
            return None

        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        logger.info("Credenciais do Google Sheets carregadas com sucesso.")
        return gspread.authorize(creds)
    except json.JSONDecodeError:
        logger.critical("Erro ao decodificar GOOGLE_CREDENTIALS_JSON. Verifique o formato.", exc_info=True)
        return None
    except Exception as e:
        logger.critical(f"Erro ao inicializar o cliente gspread: {e}", exc_info=True)
        return None

GSHEET_CLIENT = obter_gspread_client()

if GSHEET_CLIENT is None:
    logger.critical("Cliente gspread não inicializado. Funções de planilha não funcionarão.")

# ==============================
# SALVAR NA PLANILHA
# ==============================
def salvar_na_planilha(corpo_original):
    """Salva os dados na planilha Google Sheets."""
    if GSHEET_CLIENT is None:
        logger.error("Não foi possível salvar: Cliente gspread não está disponível.")
        return False

    try:
        partes = [p.strip() for p in corpo_original.split(';')]
        if len(partes) != 3:
            logger.error(f"Formato inválido para salvar: '{corpo_original}'. Esperado 3 partes.")
            return False

        desc, valor_str, cat = partes

        if not desc or not valor_str or not cat:
            logger.error(f"Campos vazios detectados: Descrição='{desc}', Valor='{valor_str}', Categoria='{cat}'")
            return False

        try:
            valor = parse_float_value(valor_str)
            if valor <= 0:
                logger.error(f"Valor deve ser positivo: '{valor_str}'")
                return False
        except ValueError as ve:
            logger.error(f"Erro de conversão de valor ao salvar: {ve}", exc_info=True)
            return False

        sh = retry_gspread_operation(GSHEET_CLIENT.open, NOME_PLANILHA)

        # Lógica para selecionar a aba
        normalized_desc = normalize_text(desc)
        nome_aba = "Geral" # Aba padrão

        if "blue" in normalized_desc:
            nome_aba = "Blue House"
        elif "up" in normalized_desc:
            nome_aba = "UP BAR"
        elif "house" in normalized_desc:
            nome_aba = "House" # Verifique o nome EXATO da aba na sua planilha

        try:
            aba = retry_gspread_operation(sh.worksheet, nome_aba)
        except WorksheetNotFound:
            logger.warning(f"Aba '{nome_aba}' não encontrada. Tentando usar 'Geral'.")
            nome_aba = "Geral"
            aba = retry_gspread_operation(sh.worksheet, nome_aba) # Tenta novamente com "Geral"

        data = datetime.now().strftime('%d/%m/%Y')

        retry_gspread_operation(aba.append_row, [data, desc, valor, cat])

        logger.info(f"Salvo com sucesso na aba '{nome_aba}': {desc}, R$ {valor:.2f}, {cat}")
        return True

    except SpreadsheetNotFound:
        logger.error(f"Planilha '{NOME_PLANILHA}' não encontrada. Verifique o nome ou permissões.", exc_info=True)
    except WorksheetNotFound:
        logger.error(f"Aba 'Geral' não encontrada na planilha '{NOME_PLANILHA}'. Verifique o nome da aba.", exc_info=True)
    except Exception as e:
        logger.error(f"Erro inesperado ao salvar na planilha: {e}", exc_info=True)
    return False

# ==============================
# WEBHOOK WHATSAPP
# ==============================
@app.route("/webhook", methods=['POST'])
def webhook():
    """Processa mensagens recebidas do Twilio WhatsApp."""
    corpo = request.values.get('Body', '').strip()
    logger.info(f"Webhook recebido: '{corpo}'")

    resp = MessagingResponse()

    # Validação inicial do formato
    partes = [p.strip() for p in corpo.split(';')]
    if len(partes) != 3 or not all(partes): # Verifica se tem 3 partes e nenhuma está vazia
        resp.body("⚠️ Formato inválido. Use: Descrição; Valor; Categoria\nEx: Café; 12,50; Alimentação")
        logger.warning(f"Formato inválido da mensagem: '{corpo}'")
        return str(resp), 200, {'Content-Type': 'application/xml'}

    # Tenta parsear o valor para dar feedback mais rápido
    try:
        valor_teste = parse_float_value(partes[1])
        if valor_teste <= 0:
            resp.body("⚠️ O valor deve ser um número positivo.\nEx: Café; 12,50; Alimentação")
            return str(resp), 200, {'Content-Type': 'application/xml'}
    except ValueError:
        resp.body("⚠️ Valor inválido. Use números com ',' ou '.' como decimal.\nEx: Café; 12,50; Alimentação")
        logger.warning(f"Valor inválido na mensagem: '{corpo}'")
        return str(resp), 200, {'Content-Type': 'application/xml'}

    # Salva em segundo plano para não bloquear a resposta do Twilio
    try:
        # Passa o corpo original para a thread
        threading.Thread(target=lambda: salvar_na_planilha(corpo)).start()
        logger.info(f"Tarefa de salvar na planilha iniciada em segundo plano para: '{corpo}'")
        resp.body("🚀 Recebido! Lançando na planilha...")
    except Exception as e:
        logger.error(f"Erro ao iniciar thread para salvar na planilha: {e}", exc_info=True)
        resp.body("❌ Erro interno ao processar sua solicitação. Tente novamente.")

    return str(resp), 200, {'Content-Type': 'application/xml'}

# ==============================
# MAIN
# ==============================
if __name__ == "__main__":
    logger.info("Iniciando aplicação Flask.")
    # O agendador foi movido para 'report_runner.py' para ser executado como um Cron Job.
    # Não inicie threads de agendamento aqui em um Web Service do Render.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
