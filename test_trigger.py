from trigger import TriggerManager


trigger = TriggerManager()


signal_put = {
    "asset": "EURUSD",
    "preco": 1.16534,
    "direcao": "PUT"
}


print("🧪 TESTE DE GATILHO")
print()

precos = [
    1.16530,
    1.16531,
    1.16532,
    1.16533,
    1.16534,
]


for preco in precos:

    print(f"Preço recebido: {preco}")

    resultado = trigger.check(
        signal_put,
        preco
    )

    if resultado:

        print("✅ ENTRADA DETECTADA")
        break
