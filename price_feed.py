import asyncio
import inspect
import os
import time
from datetime import datetime

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option


class PriceFeed:

    def __init__(self, asset="EURUSD", candle_size=1, on_price=None):
        self.asset = asset.upper()
        self.candle_size = candle_size
        self.prices = {}
        self.running = False
        self.api = None
        self.on_price = on_price

    def _connect(self):
        load_dotenv()

        email = os.getenv("IQ_EMAIL")
        password = os.getenv("IQ_PASSWORD")

        if not email or not password:
            raise RuntimeError("IQ_EMAIL ou IQ_PASSWORD não encontrados no .env")

        self.api = IQ_Option(email, password)

        connected, reason = self.api.connect()

        if not connected:
            raise RuntimeError(reason or "A IQ Option recusou a conexão.")

        self.api.change_balance("PRACTICE")

        # Mantém mais velas na memória para sempre escolher a mais nova.
        self.api.start_candles_stream(
            self.asset,
            self.candle_size,
            10
        )

    def _latest_quote(self):
        candles = self.api.get_realtime_candles(
            self.asset,
            self.candle_size
        )

        if not candles:
            return None

        # A escolha é feita pelo horário da vela, não pela posição no dicionário.
        latest = max(
            candles.values(),
            key=lambda candle: candle.get("from", 0)
        )

        quote_time = int(latest.get("from", 0))
        price = float(latest["close"])

        return price, quote_time

    def _restart_stream(self):
        try:
            self.api.stop_candles_stream(
                self.asset,
                self.candle_size
            )
        except Exception:
            pass

        self.api.start_candles_stream(
            self.asset,
            self.candle_size,
            10
        )

    async def start(self):
        try:
            print(f"Conectando o feed de {self.asset}...")
            await asyncio.to_thread(self._connect)

            self.running = True

            print("PRICE FEED conectado em PRACTICE (somente leitura)")
            print(f"Acompanhando {self.asset} em velas de 1 segundo")
            print("Pressione Ctrl+C para encerrar.")

            while self.running:
                quote = self._latest_quote()

                if quote is not None:
                    price, quote_time = quote

                    self.update_price(self.asset, price)

                    now = datetime.now().strftime("%H:%M:%S")
                    source = datetime.fromtimestamp(
                        quote_time
                    ).strftime("%H:%M:%S")

                    print(
                        f"[{now}] {self.asset}: {price} "
                        f"| cotação IQ: {source}"
                    )

                    # Se a fonte não atualiza por mais de 15 segundos,
                    # a assinatura é refeita automaticamente.
                    if quote_time and time.time() - quote_time > 15:
                        print("Feed defasado; renovando assinatura...")
                        await asyncio.to_thread(
                            self._restart_stream
                        )
                        await asyncio.sleep(1)
                        continue

                    if self.on_price is not None:
                        result = self.on_price(
                            self.asset,
                            price
                        )

                        if inspect.isawaitable(result):
                            await result

                await asyncio.sleep(1)

        finally:
            self.stop()

            if self.api is not None:
                try:
                    self.api.stop_candles_stream(
                        self.asset,
                        self.candle_size
                    )
                    self.api.close()
                except Exception:
                    pass

    def update_price(self, asset, price):
        self.prices[asset] = {
            "price": price,
            "time": datetime.now()
        }

    def get_price(self, asset):
        data = self.prices.get(asset)

        if data is None:
            return None

        return data["price"]

    def stop(self):
        self.running = False


async def main():
    feed = PriceFeed("EURUSD")
    await feed.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nPRICE FEED encerrado.")
    except Exception as error:
        print(f"\nErro no PRICE FEED: {type(error).__name__}: {error}")