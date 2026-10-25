import unittest
from unittest.mock import patch
import refine_crypto_universe as r
import broad_collect as b

class Tests(unittest.TestCase):
    def test_tokenized_stock_excluded_crypto_symbol_collision_retained(self):
        rows=[{'id':'conflux-token','symbol':'cfx','name':'Conflux','market_cap_rank':100},
              {'id':'cfx-stock','symbol':'cfx','name':'CFX Tokenized Stock','market_cap_rank':20},
              {'id':'apple-bstocks','symbol':'aaplb','name':'Apple bStocks Tokenized Stock','market_cap_rank':300},
              {'id':'jupiter-exchange','symbol':'jup','name':'Jupiter','market_cap_rank':40}]
        with patch.object(r,'PRIORITY',[]):
            names,ids=r.classify(rows,{'CFXUSDT','AAPLBUSDT','JUPUSDT'})
        self.assertNotIn('AAPLBUSDT',names);self.assertEqual(ids['CFXUSDT']['asset_id'],'conflux-token');self.assertIn('JUPUSDT',names)
    def test_missing_priority_not_silently_omitted(self):
        with self.assertRaises(ValueError):r.classify([],set())
    def test_unique_id_map_scope_and_collision(self):
        good={'schema_version':1,'crypto':['BTCUSDT'],'crypto_asset_ids':{'BTCUSDT':'bitcoin'}};b.validate(good)
        for ids in [{'OTHER':'bitcoin'},{'BTCUSDT':'../../unsafe'}]:
            with self.assertRaises(ValueError):b.validate(dict(good,crypto_asset_ids=ids))
        with self.assertRaises(ValueError):b.validate({'schema_version':1,'crypto':['AUSDT','BUSDT'],'crypto_asset_ids':{'AUSDT':'same','BUSDT':'same'}})

if __name__=='__main__':unittest.main()
