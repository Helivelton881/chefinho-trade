import asyncio
import os
import re
from datetime import date

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from iq_service import IQReadOnlyService
from price_feed import PriceFeed
from settings import load_settings, save_settings
from trigger import TriggerManager

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER = os.getenv("TELEGRAM_ALLOWED_USER_ID")
settings = load_settings()
trigger, broker, price_feed, app_ref = TriggerManager(), IQReadOnlyService(), None, None

def mode_label():
    return "DEMO" if settings["account_mode"] == "PRACTICE" else "REAL (somente consulta)"

def risk_text():
    return (f"Conta: {mode_label()}\nEntrada: {settings['entry_amount']:.2f}\n"
            f"Expiração: {settings['expiration']} minuto(s)\n"
            f"Stop loss: {settings['stop_loss']:.2f}\nStop win: {settings['stop_win']:.2f}\n"
            f"Auto DEMO: {'LIGADO' if settings['autodemo_enabled'] else 'DESLIGADO'}\n"
            f"Resultado registrado: {settings['daily_result']:.2f}")

def reset_daily_result_if_needed():
    today = date.today().isoformat()
    if settings.get("daily_date") != today:
        settings["daily_date"] = today
        settings["daily_result"] = 0.0
        save_settings(settings)

def can_open_demo_order():
    reset_daily_result_if_needed()
    result = settings["daily_result"]
    if result <= -settings["stop_loss"]:
        return False, "Stop loss diário atingido."
    if result >= settings["stop_win"]:
        return False, "Stop win diário atingido."
    if not settings["autodemo_enabled"]:
        return False, "AUTODEMO está desligado. Use /autodemo on para habilitar."
    if settings["account_mode"] != "PRACTICE":
        return False, "Entradas automáticas são permitidas somente em DEMO. Use /demo."
    return True, None

async def owner_only(update):
    if OWNER and str(update.effective_user.id) == OWNER:
        return True
    await update.effective_message.reply_text(
        f"Acesso não autorizado. Seu Telegram ID é: {update.effective_user.id}\n"
        "Defina TELEGRAM_ALLOWED_USER_ID com esse número no .env e reinicie."
    )
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update): return
    await update.message.reply_text(
        "CHEFINHO TRADE - painel pessoal\n\n"
        "/conectar - conectar usando credenciais locais do .env\n"
        "/saldo - saldo da conta selecionada\n/demo ou /real - selecionar conta\n"
        "/config - ver gestão\n/entrada 2.50 - valor de entrada\n"
        "/duracao 1 - expiração em minutos\n"
        "/stoploss 10 - limite de perda\n/stopwin 10 - meta de ganho\n"
        "/autodemo on - habilitar entradas automáticas DEMO\n"
        "/sinal EURUSD 1.16534 PUT - armar taxa\n/sinais - listar taxas\n/limpar - apagar taxas\n\n"
        "O feed avisa quando a taxa é tocada. Nenhuma ordem é enviada."
    )

async def conectar(update, context):
    if not await owner_only(update): return
    try:
        await asyncio.to_thread(broker.connect, settings["account_mode"])
        await update.message.reply_text(f"Conexão confirmada: {mode_label()}. Nenhuma ordem foi enviada.")
    except Exception as error:
        await update.message.reply_text(f"Falha de conexão: {type(error).__name__}: {error}")

async def saldo(update, context):
    if not await owner_only(update): return
    try:
        if broker.api is None or broker.mode != settings["account_mode"]:
            await asyncio.to_thread(broker.connect, settings["account_mode"])
        value = await asyncio.to_thread(broker.balance)
        await update.message.reply_text(f"Saldo {mode_label()}: {value:.2f}")
    except Exception as error:
        await update.message.reply_text(f"Não foi possível consultar o saldo: {error}")

async def account(update, context, mode):
    if not await owner_only(update): return
    settings["account_mode"] = mode
    save_settings(settings)
    await asyncio.to_thread(broker.close)
    await update.message.reply_text(f"Conta selecionada: {mode_label()}. Use /saldo para confirmar.")

async def demo(update, context): await account(update, context, "PRACTICE")
async def real(update, context): await account(update, context, "REAL")

async def config(update, context):
    if await owner_only(update): await update.message.reply_text(risk_text())

async def set_value(update, context, field, command):
    if not await owner_only(update): return
    try:
        value = float(context.args[0].replace(",", "."))
        if value <= 0 or len(context.args) != 1: raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(f"Uso: /{command} 10")
        return
    settings[field] = value
    save_settings(settings)
    await update.message.reply_text(f"Configuração atualizada.\n\n{risk_text()}")

async def entrada(update, context): await set_value(update, context, "entry_amount", "entrada")
async def stoploss(update, context): await set_value(update, context, "stop_loss", "stoploss")
async def stopwin(update, context): await set_value(update, context, "stop_win", "stopwin")

async def duracao(update, context):
    if not await owner_only(update): return
    try:
        value = int(context.args[0])
        if len(context.args) != 1 or value not in (1, 5): raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: /duracao 1 ou /duracao 5")
        return
    settings["expiration"] = value
    save_settings(settings)
    await update.message.reply_text(f"Expiração DEMO definida para {value} minuto(s).")

async def autodemo(update, context):
    if not await owner_only(update): return
    if len(context.args) != 1 or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Uso: /autodemo on ou /autodemo off")
        return
    enabled = context.args[0].lower() == "on"
    settings["autodemo_enabled"] = enabled
    save_settings(settings)
    state = "LIGADO" if enabled else "DESLIGADO"
    await update.message.reply_text(
        f"AUTODEMO {state}. Entradas continuam restritas à conta DEMO."
    )

async def sinal(update, context):
    if not await owner_only(update): return
    match = re.fullmatch(r"([A-Z]{6})\s+([0-9]+(?:\.[0-9]+)?)\s+(CALL|PUT)", " ".join(context.args).upper())
    if not match:
        await update.message.reply_text("Uso: /sinal EURUSD 1.16534 PUT")
        return
    asset, price, direction = match.groups()
    if asset != "EURUSD":
        await update.message.reply_text("Nesta versão, o feed monitora somente EURUSD.")
        return
    settings["signals"][asset] = {"asset": asset, "preco": float(price), "direcao": direction}
    save_settings(settings)
    if settings["autodemo_enabled"] and settings["account_mode"] == "PRACTICE":
        status = "Uma entrada DEMO será enviada quando a taxa for tocada."
    else:
        status = "Nenhuma ordem será enviada enquanto o AUTODEMO estiver desligado."
    await update.message.reply_text(f"Taxa armada: {asset} {price} {direction}. {status}")

async def sinais(update, context):
    if not await owner_only(update): return
    signals = settings["signals"]
    text = "Nenhuma taxa armada." if not signals else "Taxas armadas:\n\n" + "\n".join(
        f"{s['asset']} | {s['preco']} | {s['direcao']}" for s in signals.values())
    await update.message.reply_text(text)

async def limpar(update, context):
    if not await owner_only(update): return
    settings["signals"] = {}
    trigger.triggered.clear(); trigger.previous_prices.clear()
    save_settings(settings)
    await update.message.reply_text("Todas as taxas foram removidas.")

async def on_price(asset, price):
    signal = settings["signals"].get(asset)
    if signal is None or not trigger.check(signal, price): return
    settings["signals"].pop(asset, None); save_settings(settings)
    allowed, reason = can_open_demo_order()
    if not allowed:
        await app_ref.bot.send_message(chat_id=int(OWNER), text=(
            f"GATILHO ATIVADO, SEM ENTRADA\n\nAtivo: {asset}\n"
            f"Taxa: {signal['preco']}\nPreço: {price}\nMotivo: {reason}"))
        return

    try:
        order_id = await asyncio.to_thread(
            broker.place_practice_order,
            settings["entry_amount"], asset, signal["direcao"], settings["expiration"],
        )
    except Exception as error:
        await app_ref.bot.send_message(chat_id=int(OWNER), text=(
            f"GATILHO ATIVADO, MAS A ENTRADA DEMO FALHOU\n\n{type(error).__name__}: {error}"))
        return

    await app_ref.bot.send_message(chat_id=int(OWNER), text=(
        f"ENTRADA DEMO ABERTA\n\nAtivo: {asset}\nDireção: {signal['direcao']}\n"
        f"Valor: {settings['entry_amount']:.2f}\nExpiração: {settings['expiration']} min\n"
        f"Ordem: {order_id}"))
    app_ref.create_task(track_demo_result(order_id), name=f"demo-order-{order_id}")

async def track_demo_result(order_id):
    try:
        profit = await asyncio.to_thread(broker.wait_practice_result, order_id)
        reset_daily_result_if_needed()
        settings["daily_result"] += profit
        save_settings(settings)
        outcome = "WIN" if profit > 0 else "LOSS" if profit < 0 else "EMPATE"
        await app_ref.bot.send_message(chat_id=int(OWNER), text=(
            f"RESULTADO DEMO: {outcome}\n\nResultado: {profit:.2f}\n"
            f"Acumulado diário: {settings['daily_result']:.2f}"))
    except Exception as error:
        await app_ref.bot.send_message(chat_id=int(OWNER), text=(
            f"Não foi possível obter o resultado da DEMO {order_id}: {error}"))

async def run_feed(application):
    global price_feed, app_ref
    app_ref = application
    price_feed = PriceFeed("EURUSD", on_price=on_price)
    await price_feed.start()

async def post_init(application): application.create_task(run_feed(application), name="eurusd-feed")
async def post_shutdown(application):
    if price_feed: price_feed.stop()
    await asyncio.to_thread(broker.close)

def main():
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN não encontrado no .env")
    if not OWNER: print("Defina TELEGRAM_ALLOWED_USER_ID no .env antes de usar o bot.")
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    for name, handler in [("start",start),("conectar",conectar),("saldo",saldo),("demo",demo),("real",real),
                          ("config",config),("entrada",entrada),("stoploss",stoploss),("stopwin",stopwin),
                          ("duracao",duracao),("autodemo",autodemo),
                          ("sinal",sinal),("sinais",sinais),("limpar",limpar)]:
        app.add_handler(CommandHandler(name, handler))
    print("CHEFINHO TRADE: painel Telegram + feed EURUSD (somente leitura).")
    app.run_polling()

if __name__ == "__main__": main()
