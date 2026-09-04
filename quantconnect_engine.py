from AlgorithmImports import *
import json

CONFIG_URL = "https://raw.githubusercontent.com/purelyraxer-png/premarket-trader-v8/main/strategy_config.json"


class PremarketV8Research(QCAlgorithm):

    def initialize(self):
        self.config = json.loads(self.download(CONFIG_URL))

        start = [int(x) for x in self.config["test_start"].split("-")]
        end = [int(x) for x in self.config["test_end"].split("-")]

        self.set_start_date(start[0], start[1], start[2])
        self.set_end_date(end[0], end[1], end[2])
        self.set_cash(float(self.config.get("cash", 100000)))
        self.set_time_zone(TimeZones.CHICAGO)

        self.symbols = []
        for ticker in self.config["tickers"]:
            symbol = self.add_equity(
                ticker,
                Resolution.MINUTE,
                extended_market_hours=True
            ).symbol
            self.symbols.append(symbol)

        self.spy = self.add_equity(
            "SPY",
            Resolution.MINUTE,
            extended_market_hours=True
        ).symbol

        self.price_720 = {}
        self.price_805 = {}
        self.prev_close = {}
        self.pm_high = {}
        self.early_volume = {}
        self.late_volume = {}
        self.entries = {}
        self.entry_info = {}
        self.high_after = {}
        self.low_after = {}

        times = self.config["times"]
        self._schedule(times["reset"], self.reset_day)
        self._schedule(times["capture_720"], self.capture_720)
        self._schedule(times["capture_805"], self.capture_805)
        self._schedule(times["entry"], self.rank_stocks)
        self._schedule(times["grade"], self.grade_results)

    def _schedule(self, hhmm, callback):
        hour, minute = [int(x) for x in hhmm.split(":")]
        self.schedule.on(
            self.date_rules.every_day(self.spy),
            self.time_rules.at(hour, minute),
            callback
        )

    def reset_day(self):
        self.price_720 = {}
        self.price_805 = {}
        self.prev_close = {}
        self.pm_high = {}
        self.early_volume = {}
        self.late_volume = {}
        self.entries = {}
        self.entry_info = {}
        self.high_after = {}
        self.low_after = {}

        for symbol in self.symbols:
            self.pm_high[symbol] = 0
            self.early_volume[symbol] = 0
            self.late_volume[symbol] = 0

            hist = self.history(symbol, 1, Resolution.DAILY)
            if not hist.empty:
                self.prev_close[symbol] = float(hist["close"].iloc[-1])

    def capture_720(self):
        for symbol in self.symbols:
            price = self.securities[symbol].price
            if price > 0:
                self.price_720[symbol] = price

    def capture_805(self):
        for symbol in self.symbols:
            price = self.securities[symbol].price
            if price > 0:
                self.price_805[symbol] = price

    def on_data(self, data: Slice):
        minutes = self.time.hour * 60 + self.time.minute
        entry_h, entry_m = [int(x) for x in self.config["times"]["entry"].split(":")]
        grade_h, grade_m = [int(x) for x in self.config["times"]["grade"].split(":")]
        entry_min = entry_h * 60 + entry_m
        grade_min = grade_h * 60 + grade_m

        for symbol in self.symbols:
            if symbol not in data.bars:
                continue

            bar = data.bars[symbol]

            if 180 <= minutes <= entry_min:
                self.pm_high[symbol] = max(self.pm_high.get(symbol, 0), bar.high)

            if 440 <= minutes < 485:
                self.early_volume[symbol] += bar.volume

            if 485 <= minutes <= entry_min:
                self.late_volume[symbol] += bar.volume

            if symbol in self.entries and entry_min < minutes <= grade_min:
                self.high_after[symbol] = max(self.high_after[symbol], bar.high)
                self.low_after[symbol] = min(self.low_after[symbol], bar.low)

    def _gap_points(self, gap):
        s = self.config["scoring"]
        if gap > 15:
            return s["gap_above_15_points"]
        if gap < 0:
            return s["gap_below_0_points"]
        for rule in s["gap"]:
            if rule["min"] <= gap <= rule["max"]:
                return rule["points"]
        return 0

    def _descending_min_points(self, value, rules, negative_points=0):
        for rule in sorted(rules, key=lambda r: r["min"], reverse=True):
            if value >= rule["min"]:
                return rule["points"]
        return negative_points

    def rank_stocks(self):
        rankings = []
        s = self.config["scoring"]

        for symbol in self.symbols:
            if symbol not in self.prev_close or symbol not in self.price_720 or symbol not in self.price_805:
                continue

            current = self.securities[symbol].price
            if current <= 0:
                continue

            previous = self.prev_close[symbol]
            gap = ((current / previous) - 1) * 100
            hour_move = ((current / self.price_720[symbol]) - 1) * 100
            final15 = ((current / self.price_805[symbol]) - 1) * 100

            high = self.pm_high.get(symbol, current)
            distance_high = ((high - current) / high) * 100 if high > 0 else 999

            early = self.early_volume.get(symbol, 0)
            late = self.late_volume.get(symbol, 0)
            early_per_min = early / 45
            late_per_min = late / 15
            volume_accel = late_per_min / early_per_min if early_per_min > 0 else 0
            volume_accel = min(volume_accel, float(self.config.get("volume_accel_cap", 5)))

            total_volume = early + late
            dollar_volume = total_volume * current

            score = 0
            score += self._gap_points(gap)
            score += self._descending_min_points(hour_move, s["hour_move"], s["hour_move_negative_points"])
            score += self._descending_min_points(final15, s["final15"], s["final15_negative_points"])

            high_points = 0
            for rule in sorted(s["pm_high_distance"], key=lambda r: r["max"]):
                if distance_high <= rule["max"]:
                    high_points = rule["points"]
                    break
            if distance_high > 2:
                high_points += s["pm_high_distance_above_2_points"]
            score += high_points

            score += self._descending_min_points(volume_accel, s["volume_accel"], 0)

            liq = s["liquidity"]
            if dollar_volume >= 5_000_000:
                score += liq["dollar_volume_5m_points"]
            elif dollar_volume >= 1_000_000:
                score += liq["dollar_volume_1m_points"]
            elif dollar_volume < 250_000:
                score += liq["dollar_volume_below_250k_points"]

            score = max(0, min(100, score))

            rankings.append({
                "symbol": symbol,
                "score": score,
                "price": current,
                "gap": gap,
                "hour": hour_move,
                "final15": final15,
                "high_distance": distance_high,
                "volume_accel": volume_accel,
                "dollar_volume": dollar_volume
            })

        rankings.sort(key=lambda x: x["score"], reverse=True)

        self.log(f"===== {self.time.date()} | {self.config['version']} 8:20 CT =====")
        if not rankings:
            self.log("NO DATA")
            return

        top_n = int(self.config.get("top_n", 3))
        for i, r in enumerate(rankings[:top_n]):
            self.log(
                f"#{i+1} {r['symbol'].value} | SCORE {r['score']} | "
                f"GAP {r['gap']:+.2f}% | 60m {r['hour']:+.2f}% | "
                f"15m {r['final15']:+.2f}% | PMHIGH {r['high_distance']:.2f}% | "
                f"VOL {r['volume_accel']:.2f}x"
            )

            symbol = r["symbol"]
            self.entries[symbol] = r["price"]
            self.entry_info[symbol] = r
            self.high_after[symbol] = r["price"]
            self.low_after[symbol] = r["price"]

        best = rankings[0]
        if best["score"] >= float(self.config.get("no_trade_score", 70)):
            self.log(f"OFFICIAL BUY CANDIDATE = {best['symbol'].value}")
        else:
            self.log("OFFICIAL CALL = NO TRADE")

    def grade_results(self):
        self.log("----- POST ENTRY RESULTS -----")
        thresholds = self.config["winner_thresholds"]

        for symbol, entry in self.entries.items():
            high = self.high_after[symbol]
            low = self.low_after[symbol]
            mfe = ((high / entry) - 1) * 100
            mae = ((low / entry) - 1) * 100
            info = self.entry_info[symbol]

            if mfe >= thresholds["jackpot"]:
                result = "JACKPOT"
            elif mfe >= thresholds["big_winner"]:
                result = "BIG WINNER"
            elif mfe >= thresholds["winner"]:
                result = "WINNER"
            elif mfe >= -2:
                result = "SMALL/FLAT"
            else:
                result = "FAIL"

            self.log(
                f"{symbol.value} | GAP {info['gap']:+.2f}% | SCORE {info['score']} | "
                f"BEST {mfe:+.2f}% | WORST {mae:+.2f}% | {result}"
            )
