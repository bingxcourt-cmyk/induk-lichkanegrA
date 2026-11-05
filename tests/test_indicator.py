"""python3 -m unittest discover -s tests -p test_indicator.py"""
import sqlite3
import sys
import unittest
from datetime import datetime, date
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from signalbot.indicator import Indicator, MSK, STRATEGIES, parse, payoff

SIGNAL = '#GOLD LONG 📈\n🔸 Вход сейчас или 4128.01\n🛑 SL 4125.39\n🎯 TP1  4130.63\n🎯 TP2  4133.25\n🎯 TP3  4135.88\n🎯 TP4  4138.50'
TP = '✅ GOLD TP {} ВЗЯТ 📈\n+{}.00% (1.00% риск)'

class TestIndicator(unittest.TestCase):
    def setUp(self):
        c = sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        self.book=Indicator(SimpleNamespace(conn=c),-1)
    def msg(self, mid, text, hour=10, reply=None, day=5):
        m=SimpleNamespace(id=mid,message=text,date=datetime(2026,11,day,hour,0,tzinfo=MSK),
                          reply_to=SimpleNamespace(reply_to_msg_id=reply) if reply else None)
        self.book.ingest(m)
    def test_formats(self):
        self.assertEqual(parse(SIGNAL),('signal','GOLD','LONG',0))
        self.assertEqual(parse(SIGNAL.replace('GOLD','GER40').replace('LONG','SHORT')),('signal','GER40','SHORT',0))
        self.assertEqual(parse('🏁 GOLD ФИНАЛЬНЫЙ TP ВЗЯТ 📈'),('tp','GOLD','',4))
        self.assertEqual(parse('❌ GOLD Сработал стоп-лосс'),('stop','GOLD','',0))
        self.assertEqual(parse('✅ XAUUSD TP 2 ВЗЯТ 📈'),('tp','GOLD','',2))
        self.assertEqual(parse('✅ GERMANY40 TP 1 ВЗЯТ 📈'),('tp','GER40','',1))
        self.assertIsNone(parse('#GOLD LONG\nВход 4128\nSL: 4100\nTakeprofit: 4150'))
        self.assertIsNone(parse('🔥 TP1 взят - 4130 GOLD, +0.10R'))
    def test_seven_strategies(self):
        expected={0:[-1]*7,1:[1,-1,-1,-1,Decimal('-.5'),Decimal('-.2'),0],
                  2:[1,2,-1,-1,Decimal('.25'),Decimal('.4'),Decimal('1.5')],
                  3:[1,2,3,-1,Decimal('1.25'),Decimal('1.2'),Decimal('1.5')],
                  4:[1,2,3,4,Decimal('2.5'),Decimal('2.2'),Decimal('1.5')]}
        for n in range(5):
            self.assertEqual([payoff(n,w) for _,w in STRATEGIES],expected[n])
    def test_cutoff_dedupe_and_cross_signal(self):
        self.msg(1,SIGNAL);self.msg(2,SIGNAL)
        self.msg(3,TP.format(3,3),11,1);self.msg(3,TP.format(3,3),11,1)
        self.msg(4,TP.format(3,3),12,1)
        self.msg(5,'❌ GOLD Сработал стоп-лосс',13,2)
        before,_=self.book.trades(datetime(2026,11,5,21,59,tzinfo=MSK))
        self.assertFalse(before[0]['finished']);self.assertTrue(before[1]['finished'])
        self.msg(6,'🏁 GOLD ФИНАЛЬНЫЙ TP ВЗЯТ 📈',22,1)
        after,_=self.book.trades(datetime(2026,11,5,22,0,tzinfo=MSK))
        self.assertEqual([x['max_tp'] for x in after],[3,0]);self.assertTrue(all(x['finished'] for x in after))
        self.assertEqual(len(after),2)
    def test_reply_chains_edits_future_and_symbol(self):
        self.msg(1,SIGNAL);self.msg(2,'обсуждение',11,1)
        self.msg(3,TP.format(2,2),12,2)
        self.msg(4,TP.format(4,4).replace('GOLD','GER40'),13,1)
        self.msg(5,'❌ GOLD Сработал стоп-лосс',14,1)
        self.msg(6,TP.format(4,4),15,1)
        trades,unmatched=self.book.trades(datetime(2026,11,5,18,tzinfo=MSK))
        self.assertEqual(trades[0]['max_tp'],2);self.assertEqual(len(unmatched),1)
        self.msg(3,TP.format(1,1),12,2)
        self.assertEqual(self.book.trades(datetime(2026,11,5,18,tzinfo=MSK))[0][0]['max_tp'],1)
    def test_instrument_filter(self):
        self.msg(1, SIGNAL)
        self.msg(2, SIGNAL.replace('GOLD', 'GER40'))
        self.msg(3, TP.format(2, 2), 11, 1)
        self.msg(4, TP.format(1, 1).replace('GOLD', 'GER40'), 12, 2)
        at = datetime(2026, 11, 5, 22, tzinfo=MSK)
        a = date(2026, 11, 5)
        gold = self.book.report(a, a, at, symbol='GOLD')
        germany = self.book.report(a, a, at, symbol='GER40')
        all_symbols = self.book.report(a, a, at)
        self.assertIn('Сигналов: 1', gold)
        self.assertIn('втором тейке = +2%', gold)
        self.assertIn('втором тейке = -1%', germany)
        self.assertIn('втором тейке = +1%', all_symbols)
        self.assertIn('Германия (GER40)', germany)
        self.assertIn('Сигналов: 0', self.book.report(a, a, at, symbol='SILVER'))

    def test_period_is_signal_date(self):
        self.msg(1,SIGNAL,day=4);self.msg(2,TP.format(4,4),11,1,day=5)
        report=self.book.report(date(2026,11,4),date(2026,11,4),datetime(2026,11,5,15,tzinfo=MSK))
        self.assertIn('Без тейков: 1',report)
        self.assertIn('первом тейке = -1%',report)
        self.assertIn('Сигналов: 0',self.book.report(date(2026,11,5),date(2026,11,5),datetime(2026,11,5,15,tzinfo=MSK)))

if __name__=='__main__':unittest.main()
