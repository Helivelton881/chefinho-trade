import os

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option

load_dotenv()

IQ_HOST = os.getenv("IQ_HOST", "iqoption.com")
IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

if not IQ_EMAIL or not IQ_PASSWORD:
    raise RuntimeError(
        "IQ_EMAIL ou IQ_PASSWORD não encontrados no arquivo .env"
    )

print("🔌 Iniciando conexão segura com a IQ Option...")
print("🧪 Modo: somente leitura em PRACTICE")

# IQ_HOST é mantido no .env para compatibilidade, mas este fork da
# stable_api resolve o host internamente.
api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)

try:
    connected, reason = api.connect()

    if not connected:
        raise RuntimeError(reason or "A IQ Option recusou a conexão.")

    api.change_balance("PRACTICE")

    # Consulta de leitura para confirmar que a conta PRACTICE foi selecionada.
    # O valor não é exibido para evitar expor dados financeiros no terminal.
    if api.get_balance() is None:
        raise RuntimeError("Não foi possível consultar o saldo da conta PRACTICE.")

    print()
    print("✅ CONEXÃO REALIZADA COM SUCESSO!")
    print("🧪 Conta PRACTICE selecionada.")
    print("📡 Consulta de saldo concluída (valor oculto).")
    print()
    print("⚠️ Nenhuma operação foi enviada.")
    print("🧪 O teste fez apenas conexão e consulta em PRACTICE.")

except Exception as erro:
    print()
    print("❌ ERRO AO CONECTAR:")
    print(type(erro).__name__)
    print(erro)

finally:
    try:
        api.close()
    except Exception:
        pass
