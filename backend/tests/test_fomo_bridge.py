import json
import unittest
from packages.meme_scanner.fomo_bridge import normalize_tokens, trending_snapshot, normalize_theses


class BridgeTests(unittest.TestCase):
    def test_thesis_prose_has_no_nested_market_payload(self):
        items = normalize_theses([{'id':'x','comment':{'comment':'hello','marketCapAtCreation':100},'authorTrade':{'usdValue':10}}])
        self.assertEqual(items[0]['comment'],'hello')
        self.assertNotIn('marketCapAtCreation',json.dumps(items))
        self.assertNotIn('authorTrade',json.dumps(items))

    def test_only_complete_snapshot_for_both_chains(self):
        message = {'type':'data','topicType':'trending_tokens','topicId':'1,56,1399811149',
                   'payload':{'kind':'snapshot','tokens':[]}}
        self.assertEqual(trending_snapshot(json.dumps(message)), [])
        message['topicId'] = '56'
        self.assertIsNone(trending_snapshot(json.dumps(message)))
        message['topicId'] = '56,1399811149'
        message['payload']['kind'] = 'update'
        self.assertIsNone(trending_snapshot(json.dumps(message)))

    def test_solana_casing_preserved_and_other_chains_excluded(self):
        sol = 'So11111111111111111111111111111111111111112'
        tokens = normalize_tokens([
            {'networkId':1399811149,'token':{'address':sol},'marketCap':'25000'},
            {'networkId':56,'token':{'address':'0x'+'A'*40}},
            {'networkId':1,'token':{'address':'0x'+'B'*40}},
        ])
        self.assertEqual(len(tokens),2)
        self.assertEqual(tokens[0]['token_address'],sol)
        self.assertEqual(tokens[1]['token_address'],'0x'+'a'*40)


if __name__ == '__main__':
    unittest.main()
