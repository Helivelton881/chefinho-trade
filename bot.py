import asyncio
import os
import re
import uuid
from datetime import date
from pathlib import Path

from dotenv import load_dotenv, set_key
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from iq_service import IQReadOnlyService
from price_feed import MultiAssetPriceFeed
from settings import load_settings, save_settings


# ============================================================
# CONFIGURAÇÃO
# ============================================================

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER = os.getenv("TELEGRAM_ALLOWED_USER_ID")
ENV_FILE = Path(__file__).with_name(".env")
CREDENTIAL_EMAIL, CREDENTIAL_PASSWORD = range(2)

settings = load_settings()

# Compatibilidade com versões anteriores que guardavam sinais
# como dict usando o ativo como chave.
def normalize_signals():
    raw = settings.get("signals", {})
    if isinstance(raw, dict):
        normalized = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            signal = dict(value)
            signal.setdefault("id", str(key))
            signal.setdefault("status", "PENDENTE")
            normalized[str(signal["id"])] = signal
        settings["signals"] = normalized
    elif isinstance(raw, list):
        normalized = {}
        for value in raw:
            if not isinstance(value, dict):
                continue
            signal = dict(value)
            signal.setdefault("id", uuid.uuid4().hex[:8])
            signal.setdefault("status", "PENDENTE")
            normalized[str(signal["id"])] = signal
        settings["signals"] = normalized
    else:
        settings["signals"] = {}


normalize_signals()
save_settings(settings)

broker = IQReadOnlyService()
price_feeds = {}
feed_tasks = {}
shared_feed = None
shared_feed_task = None
previous_prices = {}
processing_signals = set()
app_ref = None
state_lock = asyncio.Lock()
# A iqoptionapi não é segura para várias chamadas simultâneas na mesma conexão.
# As tarefas dos sinais continuam independentes, mas o trecho que conversa
# com o websocket da IQ Option é protegido por este lock.
broker_lock = asyncio.Lock()


# ============================================================
# AUXILIARES
# ============================================================

def mode_label():
    return "DEMO" if settings.get("account_mode") == "PRACTICE" else "REAL (somente consulta)"


def risk_text():
    return (
        f"Conta: {mode_label()}\n"
        f"Entrada: {float(settings.get('entry_amount', 2.50)):.2f}\n"
        f"Expiração: {int(settings.get('expiration', 1))} minuto(s)\n"
        f"Stop loss: {float(settings.get('stop_loss', 10)):.2f}\n"
        f"Stop win: {float(settings.get('stop_win', 10)):.2f}\n"
        f"Auto DEMO: {'LIGADO' if settings.get('autodemo_enabled') else 'DESLIGADO'}\n"
        f"Resultado registrado: {float(settings.get('daily_result', 0)):.2f}"
    )


def panel_text(balance=None):
    connection = "🟢 Conectada" if broker.is_connected(settings.get("account_mode", "PRACTICE")) else "⚪ Não conectada"
    balance_text = f"💰 Saldo: {balance:.2f}" if balance is not None else "💰 Saldo: toque em Atualizar"
    pending = sum(1 for signal in signal_list() if signal.get("status") == "PENDENTE")
    credentials = "✅ Configuradas" if os.getenv("IQ_EMAIL") and os.getenv("IQ_PASSWORD") else "⚠️ Não configuradas"
    return (
        "🤖 CHEFINHO TRADE\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🏦 Conta: {mode_label()}\n"
        f"🔌 IQ Option: {connection}\n"
        f"{balance_text}\n"
        f"📌 Taxas pendentes: {pending}\n"
        f"🤖 Auto DEMO: {'LIGADO' if settings.get('autodemo_enabled') else 'DESLIGADO'}\n"
        f"🔐 Credenciais IQ: {credentials}\n\n"
        "Entradas automáticas são permitidas somente em DEMO."
    )


def panel_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔌 Conectar", callback_data="panel:connect"),
            InlineKeyboardButton("💰 Saldo", callback_data="panel:balance"),
        ],
        [
            InlineKeyboardButton("🧪 Conta DEMO", callback_data="panel:practice"),
            InlineKeyboardButton("👁️ Conta REAL", callback_data="panel:real"),
        ],
        [
            InlineKeyboardButton("📌 Taxas", callback_data="panel:signals"),
            InlineKeyboardButton("⚙️ Configuração", callback_data="panel:config"),
        ],
        [InlineKeyboardButton("🔐 Configurar IQ Option", callback_data="panel:credentials")],
        [InlineKeyboardButton("🔄 Atualizar painel", callback_data="panel:refresh")],
    ])


def reset_daily_result_if_needed():
    today = date.today().isoformat()
    if settings.get("daily_date") != today:
        settings["daily_date"] = today
        settings["daily_result"] = 0.0
        save_settings(settings)


def can_open_demo_order():
    reset_daily_result_if_needed()
    result = float(settings.get("daily_result", 0))
    stop_loss = float(settings.get("stop_loss", 10))
    stop_win = float(settings.get("stop_win", 10))

    if result <= -abs(stop_loss):
        return False, "Stop loss diário atingido."
    if result >= abs(stop_win):
        return False, "Stop win diário atingido."
    if not settings.get("autodemo_enabled", False):
        return False, "AUTODEMO está desligado. Use /autodemo on."
    if settings.get("account_mode") != "PRACTICE":
        return False, "Entradas automáticas são permitidas somente em DEMO. Use /demo."
    return True, None


def signal_list():
    return list(settings.get("signals", {}).values())


def pending_signals_for_asset(asset):
    asset = str(asset).upper().strip()
    return [
        s for s in signal_list()
        if str(s.get("asset", "")).upper() == asset
        and s.get("status", "PENDENTE") == "PENDENTE"
    ]


def signal_is_touched(signal, price, previous):
    """Detecta cruzamento da taxa. Cada sinal possui seu próprio alvo."""
    try:
        target = float(signal["preco"])
        current = float(price)
    except (TypeError, ValueError, KeyError):
        return False

    if previous is None:
        # Não dispara simplesmente porque o feed iniciou já além do alvo.
        return False

    direction = str(signal.get("direcao", "")).upper()
    if direction == "CALL":
        return previous < target <= current
    if direction == "PUT":
        return previous > target >= current
    return False


def remove_signal(signal_id):
    return settings.get("signals", {}).pop(str(signal_id), None)


# ============================================================
# SEGURANÇA TELEGRAM
# ============================================================

async def owner_only(update: Update):
    if OWNER and str(update.effective_user.id) == OWNER:
        return True
    await update.effective_message.reply_text(
        "Acesso não autorizado.\n\n"
        f"Seu Telegram ID é: {update.effective_user.id}\n\n"
        "Defina TELEGRAM_ALLOWED_USER_ID no .env e reinicie."
    )
    return False


async def panel(update, context):
    if not await owner_only(update):
        return
    await update.effective_message.reply_text(panel_text(), reply_markup=panel_keyboard())


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    balance = None

    try:
        if action == "connect":
            async with broker_lock:
                await asyncio.to_thread(broker.connect, settings.get("account_mode", "PRACTICE"))
        elif action == "balance":
            mode = settings.get("account_mode", "PRACTICE")
            async with broker_lock:
                await asyncio.to_thread(broker.ensure_connected, mode)
                balance = await asyncio.to_thread(broker.balance)
        elif action in ("practice", "real"):
            settings["account_mode"] = "PRACTICE" if action == "practice" else "REAL"
            save_settings(settings)
            async with broker_lock:
                await asyncio.to_thread(broker.close)
        elif action == "signals":
            pending = [signal for signal in signal_list() if signal.get("status") == "PENDENTE"]
            summary = "\n".join(f"• {s['asset']} {float(s['preco']):g} {s['direcao']}" for s in pending[:8]) or "Nenhuma taxa pendente."
            await query.message.reply_text(f"📌 TAXAS PENDENTES\n\n{summary}")
        elif action == "config":
            await query.message.reply_text("⚙️ CONFIGURAÇÃO\n\n" + risk_text())
    except Exception as error:
        await query.message.reply_text(f"❌ Não foi possível concluir: {type(error).__name__}: {error}")

    await query.edit_message_text(panel_text(balance), reply_markup=panel_keyboard())


async def credentials_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "🔐 CONFIGURAR IQ OPTION\n\nEnvie seu e-mail da IQ Option. A mensagem será apagada após a leitura."
    )
    return CREDENTIAL_EMAIL


async def credentials_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    if "@" not in email or len(email) > 320:
        await update.message.reply_text("Informe um e-mail válido ou use /cancelar.")
        return CREDENTIAL_EMAIL
    context.user_data["iq_email_pending"] = email
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.effective_chat.send_message("Agora envie a senha da IQ Option. Esta mensagem também será apagada.")
    return CREDENTIAL_PASSWORD


async def credentials_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text
    email = context.user_data.pop("iq_email_pending", None)
    if not email or not password:
        await update.message.reply_text("Configuração cancelada. Abra o painel para tentar novamente.")
        return ConversationHandler.END
    set_key(str(ENV_FILE), "IQ_EMAIL", email)
    set_key(str(ENV_FILE), "IQ_PASSWORD", password)
    os.environ["IQ_EMAIL"] = email
    os.environ["IQ_PASSWORD"] = password
    try:
        await update.message.delete()
    except Exception:
        pass
    async with broker_lock:
        await asyncio.to_thread(broker.close)
    await update.effective_chat.send_message("✅ Credenciais salvas localmente. Elas não são enviadas ao GitHub. Use Conectar no painel.")
    return ConversationHandler.END


async def credentials_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("iq_email_pending", None)
    await update.effective_message.reply_text("Configuração de credenciais cancelada.")
    return ConversationHandler.END


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await panel(update, context)


# ============================================================
# CONECTAR / SALDO / CONTA
# ============================================================

async def conectar(update, context):
    if not await owner_only(update):
        return
    try:
        async with broker_lock:
            await asyncio.to_thread(broker.connect, settings.get("account_mode", "PRACTICE"))
        await update.message.reply_text(
            f"✅ Conexão confirmada: {mode_label()}\n\nNenhuma ordem foi enviada."
        )
    except Exception as error:
        await update.message.reply_text(f"❌ Falha de conexão\n\n{type(error).__name__}: {error}")


async def saldo(update, context):
    if not await owner_only(update):
        return
    try:
        mode = settings.get("account_mode", "PRACTICE")
        async with broker_lock:
            if broker.api is None or broker.mode != mode:
                await asyncio.to_thread(broker.connect, mode)
            value = await asyncio.to_thread(broker.balance)
        await update.message.reply_text(f"💰 Saldo {mode_label()}: {value:.2f}")
    except Exception as error:
        await update.message.reply_text(f"❌ Não foi possível consultar o saldo:\n{error}")


async def account(update, context, mode):
    if not await owner_only(update):
        return
    settings["account_mode"] = mode
    save_settings(settings)
    async with broker_lock:
        await asyncio.to_thread(broker.close)
    await update.message.reply_text(
        f"Conta selecionada: {mode_label()}\n\nUse /saldo para confirmar."
    )


async def demo(update, context):
    await account(update, context, "PRACTICE")


async def real(update, context):
    await account(update, context, "REAL")


# ============================================================
# CONFIGURAÇÕES
# ============================================================

async def config(update, context):
    if await owner_only(update):
        await update.message.reply_text(risk_text())


async def set_value(update, context, field, command):
    if not await owner_only(update):
        return
    try:
        if len(context.args) != 1:
            raise ValueError
        value = float(context.args[0].replace(",", "."))
        if value <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(f"Uso: /{command} 10")
        return
    settings[field] = value
    save_settings(settings)
    await update.message.reply_text("Configuração atualizada.\n\n" + risk_text())


async def entrada(update, context):
    await set_value(update, context, "entry_amount", "entrada")


async def stoploss(update, context):
    await set_value(update, context, "stop_loss", "stoploss")


async def stopwin(update, context):
    await set_value(update, context, "stop_win", "stopwin")


async def duracao(update, context):
    if not await owner_only(update):
        return
    try:
        if len(context.args) != 1:
            raise ValueError
        value = int(context.args[0])
        if value not in (1, 5):
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: /duracao 1 ou /duracao 5")
        return
    settings["expiration"] = value
    save_settings(settings)
    await update.message.reply_text(f"Expiração DEMO definida para {value} minuto(s).")


async def autodemo(update, context):
    if not await owner_only(update):
        return
    if len(context.args) != 1 or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Uso: /autodemo on ou /autodemo off")
        return
    enabled = context.args[0].lower() == "on"
    settings["autodemo_enabled"] = enabled
    save_settings(settings)
    state = "LIGADO" if enabled else "DESLIGADO"
    await update.message.reply_text(
        f"🤖 AUTODEMO {state}\n\nEntradas automáticas continuam restritas à conta DEMO."
    )


# ============================================================
# ATIVOS
# ============================================================

async def ativos(update, context):
    if not await owner_only(update):
        return
    try:
        mode = settings.get("account_mode", "PRACTICE")
        async with broker_lock:
            if broker.api is None or broker.mode != mode:
                await asyncio.to_thread(broker.connect, mode)
            assets = await asyncio.to_thread(broker.get_open_binary_assets)
        if not assets:
            await update.message.reply_text("❌ Nenhuma opção BINÁRIA foi encontrada como aberta neste momento.")
            return
        # Telegram limita mensagens; envia blocos.
        header = "📊 OPÇÕES BINÁRIAS ABERTAS\n\n"
        lines = [f"🟢 {asset}" for asset in assets]
        chunk = header
        for line in lines:
            if len(chunk) + len(line) + 1 > 3500:
                await update.message.reply_text(chunk)
                chunk = ""
            chunk += line + "\n"
        chunk += f"\nTotal: {len(assets)}"
        await update.message.reply_text(chunk)
    except Exception as error:
        await update.message.reply_text(
            f"❌ Erro ao consultar os ativos:\n\n{type(error).__name__}: {error}"
        )


# ============================================================
# FEEDS DINÂMICOS
# ============================================================

async def run_feed_hub(feed):
    global shared_feed, shared_feed_task
    print("\n========================================")
    print("PRICE FEED COMPARTILHADO")
    print("========================================")

    try:
        await feed.start()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[ERRO] Feed compartilhado: {type(error).__name__}: {error}")
    finally:
        for asset, current_feed in list(price_feeds.items()):
            if current_feed is feed:
                price_feeds.pop(asset, None)
                previous_prices.pop(asset, None)
        shared_feed = None
        shared_feed_task = None


def ensure_feed(asset):
    global shared_feed, shared_feed_task
    asset = str(asset).upper().strip()
    if shared_feed is not None and shared_feed_task is not None and not shared_feed_task.done():
        shared_feed.add_asset(asset)
        price_feeds[asset] = shared_feed
        return
    if app_ref is None:
        return
    shared_feed = MultiAssetPriceFeed([asset], candle_size=1, on_price=on_price)
    price_feeds[asset] = shared_feed
    # post_init roda antes de Application.run_polling; asyncio.create_task
    # funciona tanto nessa fase quanto durante os comandos, sem o aviso PTB.
    shared_feed_task = asyncio.create_task(run_feed_hub(shared_feed), name="feed-shared")


async def stop_unused_feeds():
    active_assets = {str(s.get("asset", "")).upper() for s in signal_list() if s.get("status") == "PENDENTE"}
    for asset, feed in list(price_feeds.items()):
        if asset not in active_assets:
            try:
                feed.remove_asset(asset)
                price_feeds.pop(asset, None)
            except Exception:
                pass


# ============================================================
# ARMAR SINAL - AGORA SUPORTA QUANTAS TAXAS FOREM NECESSÁRIAS
# ============================================================

async def sinal(update, context):
    if not await owner_only(update):
        return

    text = " ".join(context.args).upper().strip()
    match = re.fullmatch(r"([A-Z0-9:/._-]+)\s+([0-9]+(?:\.[0-9]+)?)\s+(CALL|PUT)", text)
    if not match:
        await update.message.reply_text(
            "Uso:\n/sinal EURUSD-OTC 1.16534 PUT\n\n"
            "Você pode adicionar várias taxas. Cada /sinal cria um ID independente."
        )
        return

    asset, price, direction = match.groups()
    asset = broker.normalize_asset(asset)
    target = float(price)

    try:
        # Primeira verificação antes de armar.
        async with broker_lock:
            status = await asyncio.to_thread(broker.get_binary_asset_status, asset)
    except Exception as error:
        await update.message.reply_text(
            f"❌ Não foi possível verificar a disponibilidade do ativo.\n\n"
            f"{type(error).__name__}: {error}\n\nA taxa NÃO foi armada."
        )
        return

    if not status.get("available"):
        await update.message.reply_text(
            "🔴 OPÇÃO INDISPONÍVEL\n\n"
            f"Ativo: {asset}\nTaxa: {target:g}\nDireção: {direction}\n\n"
            f"Motivo:\n{status.get('reason')}\n\n"
            "❌ A taxa NÃO foi armada."
        )
        return

    signal_id = uuid.uuid4().hex[:8]
    signal = {
        "id": signal_id,
        "asset": asset,
        "preco": target,
        "direcao": direction,
        "status": "PENDENTE",
    }
    settings["signals"][signal_id] = signal
    save_settings(settings)

    # Inicia apenas um feed por ativo. Várias taxas do mesmo ativo
    # compartilham o mesmo feed, mas são avaliadas separadamente.
    ensure_feed(asset)

    if settings.get("autodemo_enabled") and settings.get("account_mode") == "PRACTICE":
        execution_status = (
            "🤖 AUTODEMO está LIGADO.\n"
            "Uma entrada DEMO será tentada quando esta taxa for tocada.\n\n"
            "⚠️ A disponibilidade será verificada novamente imediatamente antes da compra."
        )
    else:
        execution_status = (
            "ℹ️ A taxa foi armada, mas nenhuma ordem será enviada.\n\n"
            "AUTODEMO está desligado ou a conta selecionada não é DEMO."
        )

    await update.message.reply_text(
        "🟢 TAXA ARMADA\n\n"
        f"ID: {signal_id}\n"
        f"Ativo: {asset}\n"
        f"Taxa: {target:g}\n"
        f"Direção: {direction}\n\n"
        "Opção binária:\n🟢 DISPONÍVEL\n\n"
        f"{execution_status}\n\n"
        "Você pode adicionar outra taxa sem substituir esta."
    )


# ============================================================
# LISTAR / REMOVER SINAIS
# ============================================================

async def sinais(update, context):
    if not await owner_only(update):
        return

    signals = signal_list()
    if not signals:
        await update.message.reply_text("Nenhuma taxa armada.")
        return

    lines = ["📌 TAXAS ARMADAS\n"]
    for index, signal in enumerate(signals, start=1):
        status = signal.get("status", "PENDENTE")
        lines.append(
            f"{index}️⃣ ID {signal.get('id')}\n"
            f"   🟢 {signal.get('asset')} | {float(signal.get('preco', 0)):g} | {signal.get('direcao')}"
            f"\n   Status: {status}"
        )

    await update.message.reply_text("\n".join(lines))


async def remover(update, context):
    if not await owner_only(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /remover ID\nExemplo: /remover a1b2c3d4")
        return

    signal_id = context.args[0].strip()
    removed = remove_signal(signal_id)
    if removed is None:
        await update.message.reply_text("❌ ID não encontrado.")
        return

    save_settings(settings)
    await stop_unused_feeds()
    await update.message.reply_text(
        f"🧹 Taxa removida: {removed.get('asset')} {float(removed.get('preco', 0)):g} {removed.get('direcao')}"
    )


async def limpar(update, context):
    if not await owner_only(update):
        return
    settings["signals"] = {}
    save_settings(settings)
    for feed in list(price_feeds.values()):
        try:
            feed.stop()
        except Exception:
            pass
    await update.message.reply_text("🧹 Todas as taxas foram removidas.")


# ============================================================
# GATILHO DE PREÇO
# ============================================================

async def on_price(asset, price):
    asset = str(asset).upper().strip()
    if asset.startswith("FRONT."):
        asset = asset[6:]

    try:
        current = float(price)
    except (TypeError, ValueError):
        return

    previous = previous_prices.get(asset)
    previous_prices[asset] = current

    # IMPORTANTE: todos os sinais do mesmo ativo são avaliados.
    candidates = [
        s for s in signal_list()
        if str(s.get("asset", "")).upper() == asset
        and s.get("status", "PENDENTE") == "PENDENTE"
        and str(s.get("id")) not in processing_signals
    ]

    for signal in candidates:
        if not signal_is_touched(signal, current, previous):
            continue

        signal_id = str(signal.get("id"))
        if signal_id in processing_signals:
            continue
        processing_signals.add(signal_id)
        signal["status"] = "ACIONADO"
        save_settings(settings)

        # Cada taxa recebe seu próprio processamento.
        app_ref.create_task(
            process_triggered_signal(signal, current),
            name=f"signal-{signal_id}"
        )


async def execute_practice_order_safe(amount, asset, direction, expiration):
    """
    Executa uma entrada DEMO num ciclo isolado.

    Cada taxa recebe seu próprio websocket em PRACTICE. Isso permite que
    sinais tocados juntos façam a confirmação e a compra sem uma fila global.
    A mesma conexão fica reservada para acompanhar o resultado daquela ordem.
    """
    order_broker = IQReadOnlyService()
    try:
        await asyncio.to_thread(
            order_broker.ensure_connected,
            "PRACTICE",
        )
        order_id = await asyncio.to_thread(
            order_broker.place_practice_order,
            amount,
            asset,
            direction,
            expiration,
        )
        return order_id, order_broker
    except Exception:
        await asyncio.to_thread(order_broker.close)
        raise


async def process_triggered_signal(signal, price):
    signal_id = str(signal.get("id"))
    asset = str(signal.get("asset"))
    try:
        allowed, reason = can_open_demo_order()
        if not allowed:
            signal["status"] = "BLOQUEADO"
            save_settings(settings)
            await app_ref.bot.send_message(
                chat_id=int(OWNER),
                text=(
                    "🚨 GATILHO ATIVADO\n\n"
                    f"ID: {signal_id}\n"
                    f"Ativo: {asset}\n"
                    f"Taxa: {signal['preco']}\n"
                    f"Preço: {price}\n"
                    f"Direção: {signal['direcao']}\n\n"
                    "❌ ENTRADA NÃO EXECUTADA\n\n"
                    f"Motivo:\n{reason}"
                )
            )
            return

        # NÃO serializamos as compras. Cada sinal cria uma instância
        # independente do serviço IQ Option, com sua própria conexão.
        # Isso permite que duas ou mais taxas tocadas no mesmo instante
        # façam suas verificações e compras em paralelo sem compartilhar
        # a mesma conexão websocket da iqoptionapi.
        amount = float(settings.get("entry_amount", 2.50))
        expiration = int(settings.get("expiration", 1))

        try:
            order_id, order_broker = await execute_practice_order_safe(
                amount,
                asset,
                signal["direcao"],
                expiration,
            )
        except Exception as error:
            signal["status"] = "ERRO"
            save_settings(settings)
            error_text = str(error)
            if any(word in error_text.lower() for word in ("indisponível", "fechad", "not available", "inactive")):
                message = (
                    "🚨 GATILHO ATIVADO\n\n"
                    f"ID: {signal_id}\n"
                    f"Ativo: {asset}\n"
                    f"Taxa: {signal['preco']}\n"
                    f"Preço: {price}\n"
                    f"Direção: {signal['direcao']}\n\n"
                    "🔴 OPÇÃO FICOU INDISPONÍVEL\n\n"
                    f"{error_text}\n\n❌ Nenhuma ordem foi enviada."
                )
            else:
                message = (
                    "🚨 GATILHO ATIVADO\n\n"
                    f"ID: {signal_id}\n"
                    f"Ativo: {asset}\n"
                    f"Taxa: {signal['preco']}\n"
                    f"Preço: {price}\n"
                    f"Direção: {signal['direcao']}\n\n"
                    "❌ ENTRADA DEMO FALHOU\n\n"
                    f"{type(error).__name__}: {error_text}"
                )
            await app_ref.bot.send_message(chat_id=int(OWNER), text=message)
            return

        # A taxa que disparou deixa de ser pendente, mas outras taxas
        # continuam armadas normalmente.
        signal["status"] = "EXECUTADO"
        signal["order_id"] = str(order_id)
        save_settings(settings)

        await app_ref.bot.send_message(
            chat_id=int(OWNER),
            text=(
                "🟢 ENTRADA DEMO ABERTA\n\n"
                f"ID: {signal_id}\n"
                f"Ativo: {asset}\n"
                f"Direção: {signal['direcao']}\n"
                f"Taxa: {signal['preco']}\n"
                f"Preço: {price}\n\n"
                f"Valor: {float(settings.get('entry_amount', 2.50)):.2f}\n"
                f"Expiração: {int(settings.get('expiration', 1))} min\n\n"
                f"Ordem: {order_id}"
            )
        )

        app_ref.create_task(
            track_demo_result(order_id, order_broker, signal_id),
            name=f"result-{order_id}"
        )
    finally:
        processing_signals.discard(signal_id)
        await stop_unused_feeds()


async def get_practice_result_safe(order_broker, order_id):
    """
    Consulta o resultado DEMO de forma segura.

    Cada resultado usa exclusivamente o websocket do seu próprio ciclo DEMO.
    """
    return await asyncio.to_thread(order_broker.wait_practice_result, order_id)


# ============================================================
# RESULTADO DEMO
# ============================================================

async def track_demo_result(order_id, order_broker, signal_id=None):
    try:
        profit = await get_practice_result_safe(order_broker, order_id)
        reset_daily_result_if_needed()
        settings["daily_result"] = float(settings.get("daily_result", 0)) + profit
        save_settings(settings)

        outcome = "🟢 WIN" if profit > 0 else "🔴 LOSS" if profit < 0 else "⚪ EMPATE"
        await app_ref.bot.send_message(
            chat_id=int(OWNER),
            text=(
                f"RESULTADO DEMO: {outcome}\n\n"
                f"ID: {signal_id or '-'}\n"
                f"Resultado: {profit:.2f}\n"
                f"Acumulado diário: {float(settings['daily_result']):.2f}"
            )
        )
    except Exception as error:
        await app_ref.bot.send_message(
            chat_id=int(OWNER),
            text=(
                "⚠️ Não foi possível obter o resultado da DEMO.\n\n"
                f"ID: {signal_id or '-'}\n"
                f"Ordem: {order_id}\n"
                f"Erro: {error}"
            )
        )
    finally:
        await asyncio.to_thread(order_broker.close)


# ============================================================
# INICIALIZAÇÃO / SHUTDOWN
# ============================================================

async def post_init(application):
    global app_ref
    app_ref = application

    # Recupera taxas pendentes após reiniciar o bot.
    for signal in signal_list():
        if signal.get("status", "PENDENTE") == "PENDENTE":
            ensure_feed(signal.get("asset"))


async def post_shutdown(application):
    for feed in list(price_feeds.values()):
        try:
            feed.stop()
        except Exception:
            pass
    price_feeds.clear()
    feed_tasks.clear()
    async with broker_lock:
        await asyncio.to_thread(broker.close)


# ============================================================
# MAIN
# ============================================================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não encontrado no .env")

    if not OWNER:
        print("⚠️ Defina TELEGRAM_ALLOWED_USER_ID no .env.")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    credentials_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(credentials_start, pattern=r"^panel:credentials$")],
        states={
            CREDENTIAL_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, credentials_email)],
            CREDENTIAL_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, credentials_password)],
        },
        fallbacks=[CommandHandler("cancelar", credentials_cancel)],
    )
    app.add_handler(credentials_conversation)
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^panel:"))

    handlers = [
        ("start", start),
        ("painel", panel),
        ("conectar", conectar),
        ("saldo", saldo),
        ("demo", demo),
        ("real", real),
        ("config", config),
        ("entrada", entrada),
        ("stoploss", stoploss),
        ("stopwin", stopwin),
        ("duracao", duracao),
        ("autodemo", autodemo),
        ("ativos", ativos),
        ("sinal", sinal),
        ("sinais", sinais),
        ("remover", remover),
        ("limpar", limpar),
    ]

    for name, handler in handlers:
        app.add_handler(CommandHandler(name, handler))

    print("\n====================================")
    print("🤖 CHEFINHO TRADE")
    print("====================================")
    print("🟢 Telegram conectado")
    print("🟢 Sistema de múltiplas taxas concorrentes carregado")
    print("🛡️ Verificação de disponibilidade ativa")
    print("🛡️ Confirmação antes da compra ativa")
    print("🧪 Entradas automáticas somente DEMO")
    print("📡 Feed dinâmico por ativo")
    print("⚡ Entradas simultâneas isoladas por conexão")
    print("====================================\n")

    app.run_polling()


if __name__ == "__main__":
    main()
