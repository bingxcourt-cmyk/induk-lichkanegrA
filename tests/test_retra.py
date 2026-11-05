import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from signalbot.retra import format_message, Retra
from signalbot.indicator import parse

SIGNAL='#XAUUSD LONG 📈\n\n🔸 Вход сейчас или 4128.01\n🛑 SL 4125.39\n\n🎯 TP1  4130.63\n🎯 TP2  4133.25\n🎯 TP3  4135.88\n🎯 TP4  4138.50\n\n📊 Рекомендуемые лоты\n💼 $300 1%→0.01'
TP='✅ XAUUSD TP 1 ВЗЯТ 📈\n\n📈 Прибыль по сделке:\n+1.00% (1.00% риск)\n\n⚡️ Рекомендуем перевести позицию в безубыток (БУ)'
class TestRetra(unittest.TestCase):
 def test_formats_and_filters(self):
  for t,k in [(SIGNAL,'signal'),(TP,'tp'),('🏁 XAUUSD ФИНАЛЬНЫЙ TP ВЗЯТ 📉\n+4.00% (1.00% риск)','tp'),('❌ XAUUSD Сработал стоп-лосс\n📉 -1.00% закрыто по этой сделке.','stop')]:
   kind,text=format_message(t);self.assertEqual(kind,k);self.assertNotIn('XAUUSD',text);self.assertIn('GOLD',text);self.assertNotIn('Рекомендуемые лоты',text);self.assertLess(len(text),1024)
   self.assertIsNotNone(parse(text))
  for t in [SIGNAL.replace('XAUUSD','GER40'),SIGNAL.replace('XAUUSD','XAGUSD'),TP.replace('XAUUSD','GBPUSD'),'📊 ОТЧЁТ ЗА ДЕНЬ\nXAUUSD','Привет #XAUUSD','Рекламный пост','']:self.assertIsNone(format_message(t))
 def test_reply_mapping_and_edit(self):
  c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
  calls=[]
  class B:
   async def send(self,*a,**k):calls.append(('text',a,k));return len(calls)
   async def send_photo(self,*a,**k):calls.append(('photo',a,k));return len(calls)
   async def call(self,*a,**k):calls.append(('edit',a,k))
  class Reader:
   async def download_media(self,*a,**k):return b'jpg'
  r=Retra(B(),SimpleNamespace(conn=c));route={'id':1,'target':-1002}
  def msg(mid,text,reply=None,photo=False):return SimpleNamespace(id=mid,message=text,photo=photo,document=None,video=None,reply_to=SimpleNamespace(reply_to_msg_id=reply) if reply else None)
  async def run():
   await r.handle(route,msg(1,SIGNAL,photo=True),Reader())
   await r.handle(route,msg(1,SIGNAL,photo=True),Reader())
   await r.handle(route,msg(2,TP,reply=1),Reader())
   await r.handle(route,msg(3,TP,reply=999),Reader())
   await r.handle(route,msg(1,SIGNAL,photo=True),Reader(),edited=True)
  asyncio.run(run())
  self.assertEqual([x[0] for x in calls],['photo','text','edit'])
  self.assertEqual(calls[1][2]['reply_to'],1)
  self.assertEqual(calls[2][1][0],'editMessageCaption')
if __name__=='__main__':unittest.main()
