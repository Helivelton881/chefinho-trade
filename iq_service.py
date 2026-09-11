"""
Chefinho Trade - integração segura com a IQ Option.

- Consulta PRACTICE/REAL.
- Verifica disponibilidade de opções BINÁRIAS.
- Evita get_all_open_time(), que apresentou erro na versão instalada.
- Usa get_all_init_v2() -> binary -> actives.
- Compras automáticas SOMENTE em PRACTICE.
"""

import os
import time
from typing import Any, Optional

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option


class IQReadOnlyService:
    def __init__(self):
        self.api: Optional[IQ_Option] = None
        self.mode: Optional[str] = None

    def connect(self, mode: str = "PRACTICE") -> bool:
        load_dotenv()
        email = os.getenv("IQ_EMAIL")
        password = os.getenv("IQ_PASSWORD")

        if not email or not password:
            raise RuntimeError("IQ_EMAIL ou IQ_PASSWORD não configurados no .env")

        mode = str(mode).upper().strip()
        if mode not in ("PRACTICE", "REAL"):
            raise RuntimeError(f"Modo inválido: {mode}. Use PRACTICE ou REAL.")

        self.close()
        print("\n========================================")
        print("CONECTANDO NA IQ OPTION")
        print("========================================")
        print(f"Conta: {mode}\n")

        last_error = None
        # Um fechamento de websocket durante o handshake é transitório na IQ
        # Option. Não expomos a falha ao Telegram antes de esgotar as tentativas.
        for attempt in range(1, 4):
            candidate = IQ_Option(email, password)
            try:
                connected, reason = candidate.connect()
                if not connected:
                    raise RuntimeError(reason or "A IQ Option recusou a conexão.")
                candidate.change_balance(mode)
            except Exception as error:
                last_error = error
                try:
                    candidate.close()
                except Exception:
                    pass
                if attempt < 3:
                    print(f"Conexão instável; tentando novamente ({attempt}/3)...")
                    time.sleep(attempt)
                continue

            self.api = candidate
            self.mode = mode
            print("CONEXÃO CONFIRMADA")
            print(f"Conta ativa: {mode}\n")
            return True

        raise RuntimeError(
            "Não foi possível estabilizar a conexão após 3 tentativas: "
            f"{type(last_error).__name__}: {last_error}"
        )

    def ensure_connected(self, mode: str = "PRACTICE") -> bool:
        mode = str(mode).upper().strip()
        if self.api is None or self.mode != mode or not self._connection_is_healthy():
            self.connect(mode)
        return True

    def is_connected(self, mode: str = "PRACTICE") -> bool:
        """Retorna True quando a instância já está configurada para o modo pedido."""
        return (
            self.api is not None
            and self.mode == str(mode).upper().strip()
            and self._connection_is_healthy()
        )

    def _connection_is_healthy(self) -> bool:
        """Testa o websocket sem transformar uma conexão caída em uma compra."""
        if self.api is None:
            return False
        try:
            return bool(self.api.check_connect())
        except Exception:
            return False

    def balance(self) -> float:
        if self.api is None:
            raise RuntimeError("Conta não conectada. Use /conectar primeiro.")
        try:
            value = self.api.get_balance()
        except Exception as error:
            raise RuntimeError(
                f"Erro ao consultar saldo: {type(error).__name__}: {error}"
            ) from error
        if value is None:
            raise RuntimeError("Não foi possível consultar o saldo.")
        return float(value)

    @staticmethod
    def normalize_asset(asset: Any) -> str:
        if asset is None:
            return ""
        value = str(asset).upper().strip()
        if value.startswith("FRONT."):
            value = value[6:]
        return value

    @staticmethod
    def is_otc(asset: Any) -> bool:
        return IQReadOnlyService.normalize_asset(asset).endswith("-OTC")

    def get_binary_init(self) -> dict:
        if self.api is None:
            raise RuntimeError("Conta não conectada.")
        self.ensure_connected(self.mode or "PRACTICE")
        try:
            data = self.api.get_all_init_v2()
        except Exception as error:
            raise RuntimeError(
                f"Erro ao consultar get_all_init_v2(): {type(error).__name__}: {error}"
            ) from error
        if not isinstance(data, dict):
            raise RuntimeError("A IQ Option não retornou um dicionário válido em get_all_init_v2().")
        return data

    def get_binary_actives(self) -> dict:
        data = self.get_binary_init()
        binary = data.get("binary")
        if not isinstance(binary, dict):
            return {}

        actives = binary.get("actives")
        if isinstance(actives, dict):
            return actives

        if isinstance(actives, list):
            result = {}
            for item in actives:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("symbol") or item.get("asset") or item.get("instrument") or item.get("ticker")
                if name:
                    result[str(name)] = item
            return result
        return {}

    def _find_binary_asset_data(self, asset: str, actives: dict):
        asset = self.normalize_asset(asset)
        if not isinstance(actives, dict):
            return None

        # Busca direta e case-insensitive.
        for key, value in actives.items():
            key_norm = self.normalize_asset(key)
            if key_norm == asset:
                return value

        # Busca pelo nome dentro do objeto.
        for key, value in actives.items():
            if not isinstance(value, dict):
                continue
            names = (
                value.get("name"),
                value.get("symbol"),
                value.get("asset"),
                value.get("instrument"),
                value.get("ticker"),
            )
            if any(self.normalize_asset(name) == asset for name in names if name is not None):
                return value
        return None

    def _extract_active_status(self, asset_data: Any) -> Optional[bool]:
        if isinstance(asset_data, bool):
            return asset_data
        if not isinstance(asset_data, dict):
            return None

        # Uma suspensão sempre prevalece sobre qualquer flag positiva.
        for field in ("is_suspended", "suspended"):
            if field not in asset_data:
                continue
            value = asset_data[field]
            if isinstance(value, bool) and value:
                return False
            if isinstance(value, (int, float)) and bool(value):
                return False
            if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "suspended"):
                return False

        for field in (
            "open", "active", "enabled", "is_open", "is_active",
            "is_enabled", "open_status"
        ):
            if field not in asset_data:
                continue
            value = asset_data[field]
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                value = value.strip().lower()
                if value in ("true", "1", "yes", "open", "active", "enabled"):
                    return True
                if value in ("false", "0", "no", "closed", "inactive", "disabled"):
                    return False

        # Estrutura observada no retorno da IQ Option:
        # {"turbo": 0.85, "binary": 0.86}
        # Um valor binário positivo indica que há payout binário.
        binary_value = asset_data.get("binary")
        if isinstance(binary_value, (int, float)):
            return binary_value > 0

        return None

    def get_open_binary_assets(self) -> list[str]:
        actives = self.get_binary_actives()
        result = []
        for key, value in actives.items():
            if self._extract_active_status(value) is not True:
                continue
            name = key
            if isinstance(value, dict):
                name = value.get("name") or value.get("symbol") or value.get("asset") or value.get("instrument") or value.get("ticker") or key
            name = self.normalize_asset(name)
            if name and name not in result:
                result.append(name)
        return sorted(result)

    def check_asset_availability(self, asset: str) -> dict:
        asset = self.normalize_asset(asset)
        base = {
            "available": False,
            "asset": asset,
            "market": "binary",
            "otc": self.is_otc(asset),
            "raw": None,
        }

        if not asset:
            base["reason"] = "Ativo não informado."
            return base
        if self.api is None:
            base["reason"] = "Conta não conectada."
            return base

        try:
            data = self.get_binary_init()
        except Exception as error:
            base["reason"] = (
                "Não foi possível consultar os ativos BINÁRIOS da IQ Option: "
                f"{type(error).__name__}: {error}"
            )
            return base

        binary = data.get("binary")
        if not isinstance(binary, dict):
            base["reason"] = "A IQ Option não retornou a estrutura 'binary' esperada."
            base["raw"] = data
            return base

        actives = binary.get("actives")
        if isinstance(actives, list):
            normalized = {}
            for item in actives:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("symbol") or item.get("asset") or item.get("instrument") or item.get("ticker")
                if name:
                    normalized[self.normalize_asset(name)] = item
            actives = normalized

        if not isinstance(actives, dict):
            base["reason"] = "A IQ Option não retornou a coleção 'binary.actives'."
            base["raw"] = data
            return base

        asset_data = self._find_binary_asset_data(asset, actives)
        if asset_data is None:
            base["reason"] = (
                f"{asset} NÃO foi encontrado entre os ativos BINÁRIOS retornados pela IQ Option. "
                "Entrada bloqueada."
            )
            base["raw"] = data
            return base

        status = self._extract_active_status(asset_data)
        base["raw"] = asset_data

        if status is True:
            base["available"] = True
            base["reason"] = f"{asset} está disponível para opções BINÁRIAS."
            return base
        if status is False:
            base["reason"] = f"{asset} está FECHADO/INATIVO para opções BINÁRIAS."
            return base

        base["reason"] = (
            f"{asset} foi encontrado, porém a API não forneceu um status explícito de abertura. "
            "Compra bloqueada por segurança."
        )
        return base

    def get_binary_asset_status(self, asset: str) -> dict:
        return self.check_asset_availability(asset)

    def check_binary_asset(self, asset: str):
        result = self.check_asset_availability(asset)
        return result["available"], result["reason"]

    def place_practice_order(self, amount, asset, direction, expiration):
        """Executa compra BINÁRIA somente em PRACTICE, após duas verificações."""
        self.ensure_connected("PRACTICE")
        if self.api is None or self.mode != "PRACTICE":
            raise RuntimeError("Compra automática bloqueada: a conta não é PRACTICE.")

        asset = self.normalize_asset(asset)
        direction = str(direction).lower().strip()
        try:
            amount = float(amount)
            expiration = int(expiration)
        except (TypeError, ValueError) as error:
            raise RuntimeError("Valor ou expiração inválidos.") from error

        if amount <= 0:
            raise RuntimeError("O valor da entrada deve ser maior que zero.")
        if expiration <= 0:
            raise RuntimeError("A expiração deve ser maior que zero.")
        if not asset:
            raise RuntimeError("Ativo não informado.")
        if direction not in ("call", "put"):
            raise RuntimeError("Direção inválida. Use CALL ou PUT.")

        print("\n========================================")
        print("1ª VERIFICAÇÃO DE DISPONIBILIDADE")
        print("========================================")
        first = self.check_asset_availability(asset)
        print(first["reason"])
        if not first["available"]:
            raise RuntimeError("ATIVO/OPÇÃO INDISPONÍVEL ANTES DA COMPRA: " + first["reason"])

        print("\n========================================")
        print("2ª VERIFICAÇÃO - PRÉ-COMPRA")
        print("========================================")
        second = self.check_asset_availability(asset)
        print(second["reason"])
        if not second["available"]:
            raise RuntimeError(
                "ATIVO/OPÇÃO FICOU INDISPONÍVEL IMEDIATAMENTE ANTES DA COMPRA: "
                + second["reason"]
            )

        print("\n========================================")
        print("CONFIRMAÇÃO FINAL DA ENTRADA")
        print("========================================")
        print(f"Conta:      {self.mode}")
        print(f"Ativo:      {asset}")
        print(f"Direção:    {direction.upper()}")
        print(f"Valor:      {amount:.2f}")
        print(f"Expiração:  {expiration} minuto(s)")
        print("Mercado:    BINÁRIA")
        print("Disponível: SIM")
        print("========================================\n")

        try:
            success, order_id = self.api.buy(amount, asset, direction, expiration)
        except Exception as error:
            text = str(error).lower()
            if any(x in text for x in ("not available", "asset is not available", "cannot purchase", "closed", "inactive")):
                raise RuntimeError(
                    f"A IQ Option recusou a compra porque {asset} não está disponível no momento. "
                    "Nenhuma nova tentativa será feita."
                ) from error
            raise RuntimeError(
                f"Erro da IQ Option ao tentar comprar {asset}: {type(error).__name__}: {error}"
            ) from error

        if not success:
            raise RuntimeError(
                f"A IQ Option não aceitou a entrada DEMO. Ativo: {asset}. Retorno: {order_id}"
            )

        print("========================================")
        print("ENTRADA DEMO EXECUTADA")
        print("========================================")
        print(f"Ativo:      {asset}")
        print(f"Direção:    {direction.upper()}")
        print(f"Valor:      {amount:.2f}")
        print(f"Expiração:  {expiration} minuto(s)")
        print(f"Ordem ID:   {order_id}")
        print("Conta:      PRACTICE")
        print("========================================\n")
        return order_id

    def wait_practice_result(self, order_id):
        if self.api is None:
            raise RuntimeError("Conta não conectada.")
        if self.mode != "PRACTICE":
            raise RuntimeError("Consulta de resultado automático permitida somente em PRACTICE.")
        if order_id is None:
            raise RuntimeError("ID da ordem não informado.")
        try:
            result = self.api.check_win_v3(order_id)
        except Exception as error:
            raise RuntimeError(
                f"Erro ao consultar resultado da ordem {order_id}: {type(error).__name__}: {error}"
            ) from error
        if result is None:
            raise RuntimeError(f"A IQ Option não retornou o resultado da ordem {order_id}.")
        return float(result)

    def close(self):
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass
        self.api = None
        self.mode = None
