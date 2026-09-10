"""Integração da IQ Option, limitada de propósito à conta PRACTICE."""

import os
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option

class IQReadOnlyService:
    def __init__(self):
        self.api = None
        self.mode = None

    def connect(self, mode="PRACTICE"):
        load_dotenv()
        email, password = os.getenv("IQ_EMAIL"), os.getenv("IQ_PASSWORD")
        if not email or not password:
            raise RuntimeError("IQ_EMAIL ou IQ_PASSWORD não configurados no .env")
        self.close()
        self.api = IQ_Option(email, password)
        connected, reason = self.api.connect()
        if not connected:
            self.api = None
            raise RuntimeError(reason or "A IQ Option recusou a conexão.")
        self.api.change_balance(mode)
        self.mode = mode
        return True

    def balance(self):
        if self.api is None:
            raise RuntimeError("Conta não conectada. Use /conectar primeiro.")
        value = self.api.get_balance()
        if value is None:
            raise RuntimeError("Não foi possível consultar o saldo.")
        return float(value)

    def place_practice_order(self, amount, asset, direction, expiration):
        """Abre uma opção binária somente na conta de prática."""
        if self.api is None or self.mode != "PRACTICE":
            self.connect("PRACTICE")

        success, order_id = self.api.buy(
            float(amount), asset, direction.lower(), int(expiration)
        )

        if not success:
            raise RuntimeError(order_id or "A corretora não aceitou a entrada DEMO.")

        return order_id

    def wait_practice_result(self, order_id):
        """Aguarda o fechamento da operação DEMO e devolve lucro/prejuízo."""
        return float(self.api.check_win_v3(order_id))

    def close(self):
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass
        self.api, self.mode = None, None
