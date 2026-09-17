from __future__ import annotations
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from memleaf import Memleaf
from memleaf.budget import payload_chars
from memleaf.mcp_server import _TOOLS
from memleaf.retrieval import RetrievalError


class QueryClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Memleaf.initialize(Path(self.tmp.name)/'vault')

    def item(self,mid,due=None,**extra):
        return self.s.create_memory(memory_id=mid,title=mid,body='Action',type='todo',status='active',due_date=due,**extra)

    def test_first_query_reports_utc_fallback(self):
        value=self.s.list_todos(as_of='2026-09-17')
        self.assertEqual(value['query_clock'],{'as_of':'2026-09-17','timezone':'UTC'})

    def test_source_timezone_decides_first_day(self):
        with patch('memleaf.query_clock.datetime') as clock:
            clock.now.side_effect=lambda zone:datetime(2026,9,17,0,30,tzinfo=timezone.utc).astimezone(zone)
            value=self.s.list_todos(timezone='America/Los_Angeles')
        self.assertEqual(value['query_clock']['as_of'],'2026-09-16')

    def test_pagination_retains_day_and_zone_after_midnight(self):
        self.item('old','2026-09-16');self.item('today','2026-09-17');self.item('future','2026-09-18')
        first=self.s.list_todos(as_of='2026-09-17',timezone='Asia/Shanghai',limit=1)
        with patch('memleaf.query_clock.datetime') as clock:
            clock.now.side_effect=AssertionError('must not recompute date for continuation')
            second=self.s.list_todos(cursor=first['next_cursor'],limit=1)
        self.assertEqual(second['query_clock'],first['query_clock'])
        self.assertEqual(second['results'][0]['memory_id'],'today')

    def test_changing_clock_with_cursor_is_rejected(self):
        self.item('a');self.item('b')
        cursor=self.s.list_todos(as_of='2026-09-17',limit=1)['next_cursor']
        for kw in ({'as_of':'2026-09-18'},{'timezone':'Asia/Tokyo'}):
            with self.subTest(kw=kw), self.assertRaises(RetrievalError):self.s.list_todos(cursor=cursor,**kw)

    def test_invalid_clock_fields_rejected_without_fallback(self):
        for kw in ({'as_of':'tomorrow'},{'as_of':'2026-02-29'},{'as_of':True},{'timezone':'Mars/Base'},{'timezone':True}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):self.s.list_todos(**kw)

    def test_legacy_cursor_cannot_invent_first_page_clock(self):
        cursor=base64.urlsafe_b64encode(json.dumps({'kind':'active_todos','fingerprint':'a'*64,'offset':1}).encode()).decode()
        with self.assertRaises(RetrievalError):self.s.list_todos(cursor=cursor)

    def test_due_range_endpoints_and_unknown_count(self):
        self.item('low','2026-09-17');self.item('high','2026-09-20');self.item('old','2026-09-16');self.item('late','2026-09-21')
        self.item('unknown',due_text='after launch',due_status='unresolved');self.item('absent')
        result=self.s.list_todos(as_of='2026-09-17',due_from='2026-09-17',due_to='2026-09-20',include_overdue=False,include_unscheduled=False)
        self.assertEqual({r['memory_id'] for r in result['results']},{'low','high'})
        self.assertEqual(result['date_counts'],{'unresolved':1,'unscheduled':1})

    def test_overdue_union_does_not_change_state(self):
        self.item('old','2026-09-16');self.item('new','2026-09-20')
        result=self.s.list_todos(as_of='2026-09-17',due_from='2026-09-20',due_to='2026-09-20')
        self.assertEqual([r['memory_id'] for r in result['results']],['old','new'])
        self.assertEqual(self.s.read('old').status,'active')

    def test_date_unresolved_does_not_count_as_absent(self):
        self.item('unknown',due_text='a week after release',due_status='unresolved');self.item('absent')
        result=self.s.list_todos(as_of='2026-09-17')
        self.assertEqual(len(result['results']),2)
        self.assertEqual(result['date_counts'],{'unresolved':1,'unscheduled':1})

    def test_todo_clock_envelope_fits_budget_and_enumerates_all(self):
        for i in range(30):self.item(str(i))
        cursor=None;ids=[]
        while True:
            value=self.s.list_todos(cursor=cursor,as_of='2026-09-17')
            self.assertLessEqual(payload_chars(value),4000)
            ids.extend(v['memory_id'] for v in value['results'])
            cursor=value['next_cursor']
            if not cursor:break
        self.assertEqual(len(ids),30);self.assertEqual(len(set(ids)),30)

    def test_mcp_schema_exposes_calendar_not_new_tool(self):
        tools=_TOOLS
        schema=next(t['inputSchema']['properties'] for t in tools if t['name']=='list_todos')
        self.assertIn('as_of',schema);self.assertIn('timezone',schema)
