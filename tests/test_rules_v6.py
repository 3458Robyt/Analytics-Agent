import unittest
from datetime import date
from decimal import Decimal
from analytics_agent.analysis_context import resolve_analysis
from analytics_agent.analysis_sql import compile_analysis, CompiledAnalysis
from analytics_agent.semantics import load_metric_definitions, MetricQuery
from analytics_agent.presentation import analysis_presentation
from analytics_agent.schema import SchemaCatalog
from analytics_agent.claims import select_payment_classes
from tests.test_agent import make_agent, StubLLM, StubBigQuery, action
from analytics_agent.memory import SessionStore
import tempfile

class RulesV6Tests(unittest.TestCase):
 def test_defaults_and_followup(self):
  spec=resolve_analysis({},'Pagos por ramo septiembre de 2026',today=date(2026,11,17))
  self.assertEqual('payment',spec.payment_date_basis)
  net=resolve_analysis({},'Ahora netos',today=date(2026,11,17),active=spec.to_dict())
  self.assertEqual('net',net.amount_basis)
  self.assertEqual(spec.start_date,net.start_date)
  occ=resolve_analysis({},'Ahora por fecha de ocurrencia',today=date(2026,11,17),active=net.to_dict())
  self.assertEqual('accident',occ.payment_date_basis)
 def test_compiled_payment(self):
  defs=load_metric_definitions()
  spec=resolve_analysis({},'Pagos septiembre de 2026',today=date(2026,11,17))
  sql=compile_analysis(spec,defs,tables={},timezone="America/Bogota").query.sql
  self.assertIn('pago_gross',sql);self.assertIn('fecha_pago',sql)
  self.assertIn('mm_final_netos',sql);self.assertNotIn('incurrido_total',sql)
  self.assertNotIn('TRIM',sql)
 def test_reserve_pending_same_turn(self):
  with tempfile.TemporaryDirectory() as folder, SessionStore(folder) as memory:
   agent=make_agent(StubLLM([action('run_analysis',metric_keys=['reservas_avisadas'],start_date='2026-09-01',end_date='2026-10-01')]),StubBigQuery())
   answer=agent.answer('Reservas septiembre de 2026',session_store=memory)
   self.assertEqual('partial',answer.status)
   self.assertIn('corte mensual',answer.answer)
 def test_totals_not_used_as_largest(self):
  spec=resolve_analysis({},'Prima emitida por ramo septiembre de 2026',today=date(2026,11,17))
  compiled=CompiledAnalysis(MetricQuery('SELECT 1'),spec,('prima_emitida',),('prima_emitida',))
  text,table,_=analysis_presentation({'columns':['ramo','prima_emitida'],'rows':[{'ramo':'A','prima_emitida':Decimal('1.01')},{'ramo':'B','prima_emitida':Decimal('-0.01')}]},compiled)
  self.assertEqual(Decimal('1.00'),table.rows[-1]['prima_emitida'])
  self.assertEqual('Total general',table.rows[-1]['ramo'])
  self.assertNotIn('Total general',text)
 def test_observed_honorarios(self):
  self.assertEqual(['HONORARIOS Y GASTOS'],select_payment_classes(['HONORARIOS Y GASTOS','INDEMNIZACIONES'],'honorarios'))

class PaymentAdapter(StubBigQuery):
 def inspect_table_metadata(self,table_id):
  from analytics_agent.models import TABLE_IDS
  return {'table_id':table_id,'location':'us-east1','table_type':'TABLE','actual_schema':{n:{'name':n,'data_type':t} for n,t in [('pago_gross','FLOAT64'),('pago_neto','FLOAT64'),('fecha_pago','TIMESTAMP'),('fecha_accidente','TIMESTAMP'),('cod_estimacion','STRING')]}}
 def execute(self,sql,**kwargs):
  from analytics_agent.bigquery_adapter import QueryPage
  self.executions+=1
  assert 'mm_final_netos' in sql and 'incurrido_total' not in sql
  value=60 if '`pago_neto`' in sql else 100
  return QueryPage('q',('pagos_siniestros','__rows_pagos_siniestros','__values_pagos_siniestros'),({'pagos_siniestros':value,'__rows_pagos_siniestros':1,'__values_pagos_siniestros':1},),False,1,100)

class PaymentIntegrationTests(unittest.TestCase):
 def test_net_and_gross_followup_both_engines(self):
  from dataclasses import replace
  for engine in ('legacy','langgraph'):
   with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder, SessionStore(folder) as memory:
    provider=StubLLM([action('run_analysis',metric_keys=['pagos_siniestros']),action('run_analysis',metric_keys=['pagos_siniestros'])])
    agent=make_agent(provider,PaymentAdapter());agent.settings=replace(agent.settings,engine=engine)
    first=agent.answer('Pagos septiembre de 2026',session_store=memory)
    second=agent.answer('Ahora netos',session_store=memory,session_id=first.session_id)
    self.assertEqual(100,first.summary_table.rows[0]['pagos_siniestros'])
    self.assertEqual(60,second.summary_table.rows[0]['pagos_siniestros'])
