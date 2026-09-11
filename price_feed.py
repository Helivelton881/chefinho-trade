import asyncio
import inspect
import os
import time
from datetime import datetime

from dotenv import load_dotenv
from iqoptionapi.constants import ACTIVES
from iqoptionapi.stable_api import IQ_Option


class PriceFeed:

    def __init__(self, asset="EURUSD", candle_size=1, on_price=None):
        self.asset = self.normalize_asset(asset)
        self.candle_size = candle_size
        self.prices = {}
        self.running = False
        self.api = None
        self.on_price = on_price
        self.last_quote_time = 0
        self.empty_quotes = 0

    @staticmethod
    def normalize_asset(asset):
        """
        Normaliza o nome do ativo.

        Exemplos:
            EURUSD       -> EURUSD
            eurusd       -> EURUSD
            EURUSD-OTC   -> EURUSD-OTC
            eurusd_otc   -> EURUSD-OTC
        """

        if not asset:
            raise ValueError("Ativo não informado.")

        asset = str(asset).strip().upper()

        # Permite escrever EURUSD_OTC ou EURUSD OTC
        asset = asset.replace("_", "-")
        asset = asset.replace(" ", "-")

        return asset

    def _connect(self):
        load_dotenv()

        email = os.getenv("IQ_EMAIL")
        password = os.getenv("IQ_PASSWORD")

        if not email or not password:
            raise RuntimeError(
                "IQ_EMAIL ou IQ_PASSWORD não encontrados no .env"
            )

        print(f"Conectando na IQ Option para acompanhar {self.asset}...")

        last_error = None
        for attempt in range(1, 4):
            candidate = IQ_Option(email, password)
            try:
                connected, reason = candidate.connect()
                if not connected:
                    raise RuntimeError(reason or "A IQ Option recusou a conexão.")
                # Segurança: o feed continua exclusivamente em PRACTICE/DEMO.
                candidate.change_balance("PRACTICE")
            except Exception as error:
                last_error = error
                try:
                    candidate.close()
                except Exception:
                    pass
                if attempt < 3:
                    print(f"Feed instável; tentando novamente ({attempt}/3)...")
                    time.sleep(attempt)
                continue

            self.api = candidate
            self._refresh_active_mapping()
            return

        raise RuntimeError(
            "Não foi possível estabilizar o feed após 3 tentativas: "
            f"{type(last_error).__name__}: {last_error}"
        )

    def _refresh_active_mapping(self):
        """Atualiza a tabela antiga da biblioteca com os IDs OTC atuais."""
        try:
            data = self.api.get_all_init_v2()
            actives = data.get("binary", {}).get("actives", {})
        except Exception as error:
            raise RuntimeError(
                f"Não foi possível carregar IDs dos ativos BINÁRIOS: {error}"
            ) from error

        if not isinstance(actives, dict):
            raise RuntimeError("A IQ Option não retornou binary.actives para o feed.")

        added = 0
        for value in actives.values():
            if not isinstance(value, dict):
                continue
            asset_id = value.get("id")
            name = value.get("ticker") or value.get("name")
            if asset_id is None or not name:
                continue
            normalized = self.normalize_asset(name)
            if ACTIVES.get(normalized) != asset_id:
                ACTIVES[normalized] = asset_id
                added += 1
        if added:
            print(f"IDs BINÁRIOS atualizados: {added} ativo(s).")

    def _disconnect(self):
        """Fecha uma assinatura defeituosa antes de reconstruí-la."""
        api, self.api = self.api, None
        if api is None:
            return
        try:
            api.stop_candles_stream(self.asset, self.candle_size)
        except Exception:
            pass
        try:
            api.close()
        except Exception:
            pass

    def _reconnect(self):
        """Reconstrói conexão e stream; o feed nunca reutiliza socket quebrado."""
        self._disconnect()
        self._connect()
        self.start_stream()
        self.empty_quotes = 0

    def start_stream(self):
        """
        Inicia somente a assinatura em tempo real do ativo selecionado.

        A implementação ``start_candles_stream`` da versão instalada da
        iqoptionapi faz antes uma consulta histórica bloqueante por
        ``get_candles``. Quando há mais de um ativo, essa consulta pode ficar
        presa em "need reconnect" e impedir que o segundo feed confirme a
        assinatura. Para o gatilho precisamos apenas de preços futuros, não
        de velas históricas; por isso assinamos diretamente o stream.
        """

        if self.api is None:
            raise RuntimeError(
                "API da IQ Option ainda não está conectada."
            )

        print(
            f"Iniciando stream de {self.asset} "
            f"({self.candle_size}s)..."
        )

        try:
            # Mantém até 10 velas em memória, como faria o método público da
            # biblioteca, mas sem chamar a coleta histórica problemática.
            self.api.api.real_time_candles_maxdict_table[self.asset][self.candle_size] = 10
            started = self.api.start_candles_one_stream(self.asset, self.candle_size)
        except Exception as error:
            raise RuntimeError(
                f"Não foi possível assinar o stream de {self.asset}: {error}"
            ) from error

        if started is not True:
            raise RuntimeError(f"O stream de {self.asset} não confirmou a assinatura.")

    def stop_stream(self):
        """
        Para o stream do ativo atual.
        """

        if self.api is None:
            return

        try:
            self.api.stop_candles_stream(
                self.asset,
                self.candle_size
            )
        except Exception:
            pass

    def _latest_quote(self):
        """
        Obtém a vela mais recente do ativo.

        Retorna:
            (preço, timestamp)
        """

        if self.api is None:
            return None

        candles = self.api.get_realtime_candles(
            self.asset,
            self.candle_size
        )

        if not candles:
            return None

        valid_candles = [
            candle
            for candle in candles.values()
            if candle.get("from") is not None
            and candle.get("close") is not None
        ]

        if not valid_candles:
            return None

        latest = max(
            valid_candles,
            key=lambda candle: candle.get("from", 0)
        )

        quote_time = int(
            latest.get("from", 0)
        )

        price = float(
            latest["close"]
        )

        return price, quote_time

    def _restart_stream(self):
        """
        Reinicia o stream quando a cotação ficar defasada.
        """

        print(
            f"Reiniciando stream de {self.asset}..."
        )

        self._reconnect()

    async def start(self):

        try:
            print("=" * 50)
            print("CHEFINHO TRADE - PRICE FEED")
            print("=" * 50)

            print(
                f"Ativo solicitado: {self.asset}"
            )

            await asyncio.to_thread(self._reconnect)

            self.running = True

            print(
                "PRICE FEED conectado em PRACTICE"
            )

            print(
                f"Acompanhando: {self.asset}"
            )

            print(
                f"Timeframe do feed: "
                f"{self.candle_size} segundo(s)"
            )

            print("=" * 50)

            while self.running:

                try:

                    quote = await asyncio.to_thread(
                        self._latest_quote
                    )

                    if quote is None:

                        self.empty_quotes += 1

                        print(
                            f"[{datetime.now():%H:%M:%S}] "
                            f"{self.asset}: aguardando cotação..."
                        )

                        if self.empty_quotes >= 5:
                            print(f"⚠️ Feed de {self.asset} sem cotação; reconectando.")
                            await asyncio.to_thread(self._reconnect)
                            self.empty_quotes = 0

                        await asyncio.sleep(1)

                        continue

                    price, quote_time = quote

                    self.empty_quotes = 0

                    self.last_quote_time = quote_time

                    self.update_price(
                        self.asset,
                        price
                    )

                    now = datetime.now().strftime(
                        "%H:%M:%S"
                    )

                    source = datetime.fromtimestamp(
                        quote_time
                    ).strftime("%H:%M:%S")

                    delay = max(
                        0,
                        time.time() - quote_time
                    )

                    print(
                        f"[{now}] "
                        f"{self.asset}: {price} "
                        f"| cotação IQ: {source} "
                        f"| atraso: {delay:.1f}s"
                    )

                    # Se o feed ficar mais de 15 segundos
                    # sem atualizar, renovamos a assinatura.
                    if (
                        quote_time
                        and time.time() - quote_time > 15
                    ):

                        print(
                            f"⚠️ Feed de {self.asset} "
                            f"está defasado."
                        )

                        await asyncio.to_thread(self._restart_stream)

                        await asyncio.sleep(1)

                        continue

                    # Entrega o preço ao bot/gatilho.
                    if self.on_price is not None:

                        try:
                            result = self.on_price(self.asset, price)
                            if inspect.isawaitable(result):
                                await result
                        except Exception as callback_error:
                            # Uma falha do bot/Telegram não pode parar o feed.
                            print(f"⚠️ Erro ao entregar preço de {self.asset}: {callback_error}")

                    await asyncio.sleep(0.2)

                except Exception as error:

                    print(
                        f"⚠️ Erro no feed "
                        f"{self.asset}: "
                        f"{type(error).__name__}: {error}"
                    )

                    await asyncio.to_thread(self._disconnect)
                    await asyncio.sleep(2)

        finally:

            self.stop()

            await asyncio.to_thread(self._disconnect)

            print(
                f"PRICE FEED {self.asset} encerrado."
            )

    def update_price(self, asset, price):

        self.prices[asset] = {
            "price": price,
            "time": datetime.now()
        }

    def get_price(self, asset):

        asset = self.normalize_asset(asset)

        data = self.prices.get(asset)

        if data is None:
            return None

        return data["price"]

    def get_price_data(self, asset):

        asset = self.normalize_asset(asset)

        return self.prices.get(asset)

    def stop(self):

        self.running = False


class MultiAssetPriceFeed(PriceFeed):
    """Um único websocket PRACTICE para todos os ativos com taxas armadas."""

    def __init__(self, assets, candle_size=1, on_price=None):
        normalized = {self.normalize_asset(asset) for asset in assets}
        if not normalized:
            raise ValueError("Informe pelo menos um ativo para o feed.")
        super().__init__(next(iter(normalized)), candle_size, on_price)
        self.assets = normalized
        self.subscribed_assets = set()
        self.empty_quotes = {}

    def add_asset(self, asset):
        self.assets.add(self.normalize_asset(asset))

    def remove_asset(self, asset):
        self.assets.discard(self.normalize_asset(asset))
        if not self.assets:
            self.stop()

    def _disconnect(self):
        api, self.api = self.api, None
        if api is None:
            return
        for asset in list(self.subscribed_assets):
            try:
                api.stop_candles_stream(asset, self.candle_size)
            except Exception:
                pass
        self.subscribed_assets.clear()
        try:
            api.close()
        except Exception:
            pass

    def _reconnect(self):
        self._disconnect()
        self._connect()
        self.empty_quotes.clear()

    def _start_asset_stream(self, asset):
        if self.api is None:
            raise RuntimeError("API da IQ Option ainda não está conectada.")
        if asset not in ACTIVES:
            raise RuntimeError(f"A biblioteca não possui o ID atualizado de {asset}.")
        print(f"Iniciando stream de {asset} ({self.candle_size}s)...")
        try:
            self.api.api.real_time_candles_maxdict_table[asset][self.candle_size] = 10
            started = self.api.start_candles_one_stream(asset, self.candle_size)
        except Exception as error:
            raise RuntimeError(f"Não foi possível assinar {asset}: {error}") from error
        if started is not True:
            raise RuntimeError(f"O stream de {asset} não confirmou a assinatura.")
        self.subscribed_assets.add(asset)

    def _sync_subscriptions(self):
        if self.api is None:
            raise RuntimeError("Feed não conectado.")
        wanted = set(self.assets)
        for asset in self.subscribed_assets - wanted:
            try:
                self.api.stop_candles_stream(asset, self.candle_size)
            except Exception:
                pass
            self.subscribed_assets.discard(asset)
        for asset in wanted - self.subscribed_assets:
            self._start_asset_stream(asset)

    def _latest_quote_for(self, asset):
        if self.api is None:
            return None
        candles = self.api.get_realtime_candles(asset, self.candle_size)
        if not candles:
            return None
        valid = [
            candle for candle in candles.values()
            if candle.get("from") is not None and candle.get("close") is not None
        ]
        if not valid:
            return None
        latest = max(valid, key=lambda candle: candle.get("from", 0))
        return float(latest["close"]), int(latest["from"])

    async def start(self):
        retry_seconds = 1
        self.running = True
        try:
            print("=" * 50)
            print("CHEFINHO TRADE - FEED COMPARTILHADO")
            print("=" * 50)
            await asyncio.to_thread(self._reconnect)
            print("PRICE FEED conectado em PRACTICE")

            while self.running:
                try:
                    await asyncio.to_thread(self._sync_subscriptions)
                    for asset in list(self.assets):
                        quote = await asyncio.to_thread(self._latest_quote_for, asset)
                        if quote is None:
                            self.empty_quotes[asset] = self.empty_quotes.get(asset, 0) + 1
                            if self.empty_quotes[asset] >= 5:
                                raise RuntimeError(f"{asset} sem cotação recente")
                            continue

                        self.empty_quotes[asset] = 0
                        price, quote_time = quote
                        if quote_time and time.time() - quote_time > 15:
                            raise RuntimeError(f"{asset} com cotação defasada")
                        self.update_price(asset, price)
                        if self.on_price is not None:
                            try:
                                result = self.on_price(asset, price)
                                if inspect.isawaitable(result):
                                    await result
                            except Exception as callback_error:
                                print(f"⚠️ Erro ao entregar preço de {asset}: {callback_error}")
                    retry_seconds = 1
                    await asyncio.sleep(0.2)
                except Exception as error:
                    print(f"⚠️ Feed compartilhado: {type(error).__name__}: {error}")
                    await asyncio.to_thread(self._disconnect)
                    await asyncio.sleep(retry_seconds)
                    retry_seconds = min(retry_seconds * 2, 30)
        finally:
            self.stop()
            await asyncio.to_thread(self._disconnect)
            print("FEED COMPARTILHADO encerrado.")


async def main():

    feed = PriceFeed(
        "EURUSD-OTC"
    )

    await feed.start()


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        print(
            "\nPRICE FEED encerrado manualmente."
        )

    except Exception as error:

        print(
            f"\nErro no PRICE FEED: "
            f"{type(error).__name__}: {error}"
        )
