import os
import json
import time
import logging
from datetime import datetime
from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import SpreadsheetNotFound, WorksheetNotFound, APIError

# Configurar logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==============================
# CONFIGURAÇÕES
# ==============================
NOME_PLANILHA = "Controle_Despesas"
TWILIO_WHATSAPP_FROM = os.environ.get('TWILIO_WHATSAPP_FROM', 'whatsapp:+14155238886') # Seu número Twilio WhatsApp

# ==============================
# FUNÇÕES AUXILIARES (copiadas de app.py para auto-suficiência)
# ==============================
def parse_float_value(value_str):
    """Converte string para float, aceitando ',' ou '.' como decimal."""
    if not isinstance(value_str, str):
        raise ValueError("O valor não é uma string.")

    cleaned_value = value_str.replace('R$', '').replace('r$', '').strip().replace('.', '').replace(',', '.')

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
                raise
        except Exception as e:
            logger.error(f"Erro inesperado durante operação gspread. Tentativa {i+1}/{max_retries}", exc_info=True)
            raise
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
        logger.info("Credenciais do Google Sheets carregadas com sucesso para o relatório.")
        return gspread.authorize(creds)
    except json.JSONDecodeError:
        logger.critical("Erro ao decodificar GOOGLE_CREDENTIALS_JSON para o relatório. Verifique o formato.", exc_info=True)
        return None
    except Exception as e:
        logger.critical(f"Erro ao inicializar o cliente gspread para o relatório: {e}", exc_info=True)
        return None

GSHEET_CLIENT_REPORT = obter_gspread_client()

if GSHEET_CLIENT_REPORT is None:
    logger.critical("Cliente gspread para relatório não inicializado. O relatório não será enviado.")

# ==============================
# RELATÓRIO DIÁRIO
# ==============================
def enviar_relatorio_diario():
    """Gera e envia o relatório diário de despesas via WhatsApp."""
    logger.info("Iniciando envio de relatório diário.")
    if GSHEET_CLIENT_REPORT is None:
        logger.error("Não foi possível enviar relatório: Cliente gspread não está disponível.")
        return

    try:
        twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        seu_whatsapp = os.environ.get("SEU_WHATSAPP")

        if not all([twilio_account_sid, twilio_auth_token, seu_whatsapp]):
            logger.error("Variáveis de ambiente TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN ou SEU_WHATSAPP não configuradas para o relatório.")
            return

        client_twilio = Client(twilio_account_sid, twilio_auth_token)

        sh = retry_gspread_operation(GSHEET_CLIENT_REPORT.open, NOME_PLANILHA)
        aba = retry_gspread_operation(sh.worksheet, "Geral") # Assumindo que a aba "Geral" existe e tem todos os dados
        dados = retry_gspread_operation(aba.get_all_values)

        hoje = datetime.now().strftime('%d/%m/%Y')

        total = 0
        categorias = {}
        lancamentos_hoje = 0

        # Pula o cabeçalho
        for linha in dados[1:]:
            try:
                # Garante que a linha tem colunas suficientes
                if len(linha) < 4:
                    logger.warning(f"Linha com formato inesperado (menos de 4 colunas) no relatório: {linha}")
                    continue

                data, desc, valor_str, cat = linha[0], linha[1], linha[2], linha[3]

                if data == hoje:
                    valor = parse_float_value(valor_str)
                    total += valor
                    categorias[cat] = categorias.get(cat, 0) + valor
                    lancamentos_hoje += 1
            except ValueError as ve:
                logger.warning(f"Erro de valor ao processar linha do relatório: {linha} - {ve}", exc_info=True)
            except Exception as e:
                logger.error(f"Erro inesperado ao processar linha do relatório: {linha} - {e}", exc_info=True)
                continue

        msg = f"📊 Relatório do dia {hoje}\n\n"

        if lancamentos_hoje == 0:
            msg += "Nenhum lançamento registrado hoje."
        else:
            # Ordena categorias por valor decrescente
            sorted_categorias = sorted(categorias.items(), key=lambda item: item[1], reverse=True)
            for cat, v in sorted_categorias:
                msg += f"• {cat}: R$ {v:.2f}\n"
            msg += f"\n💰 Total: R$ {total:.2f} ({lancamentos_hoje} lançamentos)"

        client_twilio.messages.create(
            body=msg,
            from_=TWILIO_WHATSAPP_FROM,
            to=seu_whatsapp
        )

        logger.info("Relatório diário enviado com sucesso!")

    except SpreadsheetNotFound:
        logger.error(f"Planilha '{NOME_PLANILHA}' não encontrada para o relatório.", exc_info=True)
    except WorksheetNotFound:
        logger.error(f"Aba 'Geral' não encontrada na planilha '{NOME_PLANILHA}' para o relatório.", exc_info=True)
    except Exception as e:
        logger.error(f"Erro inesperado no envio do relatório diário: {e}", exc_info=True)

# ==============================
# MAIN para o Cron Job
# ==============================
if __name__ == "__main__":
    logger.info("Iniciando script de relatório diário.")
    enviar_relatorio_diario()
    logger.info("Script de relatório diário finalizado.")
