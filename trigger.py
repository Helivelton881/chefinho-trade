from datetime import datetime


class TriggerManager:

    def __init__(self):
        self.triggered = set()
        self.previous_prices = {}

    def check(self, signal, current_price):

        asset = signal["asset"]
        target = signal["preco"]
        direction = signal["direcao"]

        signal_id = f"{asset}_{target}_{direction}"

        # Não dispara o mesmo sinal novamente
        if signal_id in self.triggered:
            return False

        previous_price = self.previous_prices.get(asset)

        triggered = False

        # =====================================================
        # PRIMEIRO PREÇO RECEBIDO
        # =====================================================

        if previous_price is None:

            # Se já chegou exatamente na taxa
            if current_price == target:
                triggered = True

        # =====================================================
        # PUT
        # =====================================================

        elif direction == "PUT":

            # Cruzamento para baixo ou toque
            if previous_price >= target and current_price <= target:
                triggered = True

        # =====================================================
        # CALL
        # =====================================================

        elif direction == "CALL":

            # Cruzamento para cima ou toque
            if previous_price <= target and current_price >= target:
                triggered = True

        # Guarda o preço atual para o próximo tick
        self.previous_prices[asset] = current_price

        # =====================================================
        # GATILHO
        # =====================================================

        if triggered:

            self.triggered.add(signal_id)

            horario = datetime.now().strftime(
                "%H:%M:%S.%f"
            )[:-3]

            print()
            print("====================================")
            print("🚨 GATILHO ATIVADO")
            print("====================================")
            print(f"Ativo: {asset}")
            print(f"Direção: {direction}")
            print(f"Taxa: {target}")
            print(f"Preço: {current_price}")
            print(f"Horário: {horario}")
            print("====================================")
            print()

            return True

        return False