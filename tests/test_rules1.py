"""Стандартные правила: сообщения COMMUNITY 777 без Telegram: python3 tests/test_rules1.py"""
import os, sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from signalbot.logic import classify_reply

D = Decimal


def test_classify():
    assert classify_reply("🚨 Отменяем шорт 4085-4087") == ("cancel", "SELL", [D("4085"), D("4087")])
    assert classify_reply("🚨 Отменяем лонг 4072-4070") == ("cancel", "BUY", [D("4072"), D("4070")])
    assert classify_reply("🚨 Отменяем лонг") == ("cancel", "BUY", [])
    assert classify_reply("🚨 Entry miss by 10 pips Отменяем лонг 4590-4588") == ("cancel", "BUY", [D("4590"), D("4588")])
    assert classify_reply("🚨Позиция была закрыта при цене 4620.\n\nРынок достиг нашего стоп-лосса резким скачком, "
                          "а затем пошел в том направлении, которое мы прогнозировали.") == ("sl",)
    assert classify_reply("Перезаход. Активная сделка") == ("reentry",)
    assert classify_reply("Заходим сейчас") == ("enter",)
    assert classify_reply("Очень риск высокий.\n\nЗанижайте риски максимально") == ("risk",)
    upd = ("When the signal was shared, price was at 4118, my entry is also at 4118, updating the TP.\n\n"
           "✅Take profit 1: 4114\n✅Take profit 2: 4110\n✅Take profit 3: 4106\n✅Take profit 4: 4080")
    assert classify_reply(upd) == ("tpupd", {1: D("4114"), 2: D("4110"), 3: D("4106"), 4: D("4080")})
    # прежние ответы не изменились
    assert classify_reply("✅TP1  - 4072 XAUUSD, +600 point") == ("tp", 1, D("4072"))
    assert classify_reply("🚨 Отстопило 4628") == ("sl",)
    for junk in ("Планирую лонг по XAUUSD на уровне 4050-4047\n\nБуду держать вас в курсе",
                 "Братцы, приветствую вас в новом месяце!", "🔫Братцы, результаты нашей движухи!!"):
        assert classify_reply(junk) is None, junk


if __name__ == "__main__":
    test_classify()
    print("ok test_classify")
