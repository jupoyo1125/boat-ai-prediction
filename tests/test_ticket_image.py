import base64
import copy
import hashlib
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest import mock

from flask import Flask
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_threads_publisher import forecast, buffer_post, MemoryStore
import threads_publisher as tp
import ticket_image as ti
from buffer_publisher import BufferClient


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.row = forecast()

    def test_exact_lists_are_sorted_without_formations_or_mutation(self):
        before = copy.deepcopy(self.row)
        value = ti.snapshot(self.row)
        for group, (key, _, _) in zip(value['groups'], ti.GROUPS):
            self.assertEqual(group['bets'], sorted(b['bet'] for b in before['bets'] if b['category']==key))
        self.assertEqual(self.row, before)
        self.assertEqual([len(g['bets']) for g in value['groups']], [10,10,3])

    def test_png_is_frozen_deterministically_and_changes_with_the_actual_bets(self):
        a, b = ti.freeze(self.row), ti.freeze(copy.deepcopy(self.row))
        self.assertEqual(a, b)
        raw = base64.b64decode(a['image_data'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), a['image_sha256'])
        self.assertEqual(Image.open(BytesIO(raw)).size, ti.SIZE)
        self.row['bets'][0]['bet']='6-5-4'
        self.assertNotEqual(ti.freeze(self.row)['image_sha256'], a['image_sha256'])

    def test_empty_category_is_skipped_and_invalid_combinations_fail_closed(self):
        self.row['bets']=[b for b in self.row['bets'] if b['category']!='oni']
        self.assertEqual(ti.snapshot(self.row)['groups'][2]['bets'], [])
        self.assertTrue(ti.render_row(self.row).startswith(b'\x89PNG'))
        for bad in ['1-1-2','1-2-7','1-2-3 / 1-2-4', None]:
            row=forecast();row['bets'][0]['bet']=bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):ti.freeze(row)
        row=forecast();row['bets'][1]['bet']=row['bets'][0]['bet']
        with self.assertRaises(ValueError):ti.freeze(row)

    def test_first_three_lines_are_picks_and_observed_exhibition_not_guessed_tactics(self):
        self.row['prediction']['boats'][2]['exhibition']=6.61
        text=tp.ticket_caption(self.row)
        self.assertEqual(text.splitlines()[:3], ['本命：①選手1','軸：④選手4',
                         '今回の注目点：展示最速は③（6.61秒）'])
        self.assertIn('独自サイト予想',text)
        self.assertNotIn('http',text)
        self.assertNotIn('1-2-3',text)
        self.assertLessEqual(len(text),500)
        self.assertIn('同じ6.70秒',tp.ticket_caption(forecast()))

    def test_buffer_candidates_have_images_but_line_promotions_do_not_leak_bets(self):
        now=tp.timestamp('2026-10-15T10:20:00+09:00')
        config=tp.Config('20261015',2,'fake','buffer','channel-1')
        entry=tp.candidate(self.row,config,now)
        self.assertEqual(entry['format'],'ticket-v1')
        self.assertTrue(entry['text'].startswith('本命：'))
        config=tp.Config('20261015',2,'fake','buffer','channel-1',
                         line_promotion=tp.LinePromotion('20261015',2,'https://lin.ee/example'))
        entry=tp.candidate(self.row,config,now)
        self.assertNotIn('image_url',entry)
        self.assertIn('無料予想LINE',entry['text'])

    def test_image_route_serves_only_queued_hashes_and_preserves_bytes_after_settlement(self):
        store=MemoryStore()
        entry=dict(ti.freeze(self.row),key=self.row['id'])
        store.enqueue([entry])
        app=Flask(__name__);ti.register_route(app,lambda:store)
        client=app.test_client()
        url=entry['image_url'].removeprefix(ti.ORIGIN)
        response=client.get(url)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.mimetype,'image/png')
        self.assertEqual(response.data,base64.b64decode(entry['image_data']))
        self.assertIn('immutable',response.headers['Cache-Control'])
        self.row['settled']=True;self.row['bets']=[]
        self.assertEqual(client.get(url).data,response.data)
        self.assertEqual(client.get(url.replace(entry['image_sha256'],'0'*64)).status_code,404)
        self.assertEqual(client.get(url.replace('/2/','/3/')).status_code,404)
        store.state['posts'][self.row['id']]['image_data']='invalid'
        self.assertEqual(client.get(url).status_code,404)


class ImageAdapterTests(unittest.TestCase):
    def setUp(self):
        self.response=mock.Mock(status_code=200)
        self.session=mock.Mock();self.session.request.return_value=self.response
        self.client=BufferClient('test-key','channel-1',self.session)
        self.text=tp.ticket_caption(forecast())
        self.url=ti.freeze(forecast())['image_url']
        self.options={'image_url':self.url,'asset_ids':['ticket-asset-1']}

    def test_private_image_draft_pins_attachment_and_publish_preserves_it(self):
        self.response.json.return_value={'data':{'createPost':{'post':buffer_post(ticket=True)}}}
        created=self.client.create(self.text,image_url=self.url)
        self.assertEqual(created,{'id':'buffer-post-1','asset_ids':['ticket-asset-1']})
        self.assertEqual(self.session.request.call_args.kwargs['json']['variables']['input']['assets'],
                         [{'image':{'url':self.url}}])
        self.response.json.return_value={'data':{'post':buffer_post(ticket=True)}}
        self.client.validate_draft(created['id'],self.text,**self.options)
        self.response.json.return_value={'data':{'editPost':{'post':buffer_post('sending',ticket=True)}}}
        self.client.publish(created['id'],self.text,**self.options)
        self.assertNotIn('assets',self.session.request.call_args.kwargs['json']['variables']['input'])

    def test_changed_missing_or_nonimage_attachment_never_validates(self):
        for changes in [{'id':'changed'},{'type':'video'},{'mimeType':'video/mp4'}]:
            post=buffer_post(ticket=True);post['assets'][0].update(changes)
            self.response.json.return_value={'data':{'post':post}}
            with self.subTest(changes=changes),self.assertRaises(tp.ThreadsError):
                self.client.validate_draft(post['id'],self.text,**self.options)
        post=buffer_post(ticket=True);post['assets']=[]
        self.response.json.return_value={'data':{'post':post}}
        with self.assertRaises(tp.ThreadsError):
            self.client.validate_draft(post['id'],self.text,**self.options)


if __name__=='__main__':unittest.main()
