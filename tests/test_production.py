"""Production workflow invariants: partial installs, whole-stack movement, supply and accounting."""
import contextlib
from dataclasses import replace
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from eve_skills import (cli, industry, ledger, ledger_db, operation, production as p,
                        production_data as data, production_reports as reports,
                        production_scan as scan, production_store as store, cmd_production)
from tests.test_ledger import facts, job, replay, stamp, RECIPES, INVENTION, T2_BP, T2_ITEM as T2, PART as MAT

NOW = '2030-01-03T08:00:00Z'
START = '2030-01-03T07:00:00Z'
FAC = 1040000000002


def line(n=1, **extra):
    return dict(line_id=n, product_id=200, product='Example product', installer_id=1, installer='Builder',
                activity='manufacturing', activity_id=1, facility_id=FAC, facility='Factory', runs=10,
                blueprint_type_id=100, blueprint_id=None, me=0, te=0, required_skills={},
                materials={'34': 60}, start=START, hours_per_run=1, **extra)


def plan(lines=None):
    return dict(id=1, site='example', title='Morning', created_at=START, start=START, lines=lines or [line()])


def observed(ident=11, runs=10, **kw):
    r = dict(job_id=ident, installer_id=1, activity_id=1, blueprint_type_id=100, facility_id=FAC,
             blueprint_id=1000, status='active', runs=runs, start_date=START, end_date='2030-01-03T17:00:00Z')
    r.update(kw)
    return r


def snapshot(qty=100, usable=True):
    return data.Snapshot(NOW, assets=[dict(root=FAC, type_id=34, quantity=qty, owner='char:2', usable=usable)],
                         blueprints=[dict(item_id=1000 + n, type_id=100, root=FAC, owner='char:2',
                                          flag='Hangar', runs=10, quantity=-2, material_efficiency=0) for n in range(3)],
                         skills={1: {3387: 5, 24625: 5}}, characters={1: 'Builder', 2: 'Holder'},
                         documents=[dict(section=k, owner='char:2', as_of=NOW, expires=NOW, available=True)
                                    for k in ('jobs', 'assets', 'blueprints')])


class RoundTests(unittest.TestCase):
    def test_exact_matches_are_reserved_before_changed_run_counts(self):
        lines = [dict(line(1), runs=5), line(2)]
        rows, extras = p.reconcile(plan(lines), [observed()], NOW)
        self.assertEqual(['remaining', 'installed'], [r['state'] for r in rows])
        self.assertFalse(extras)

    def test_finished_job_is_installed_cancelled_job_is_not(self):
        rows, _ = p.reconcile(plan(), [observed(status='delivered')], NOW)
        self.assertEqual('installed', rows[0]['state'])
        rows, _ = p.reconcile(plan(), [observed(status='cancelled')], NOW)
        self.assertEqual('remaining', rows[0]['state'])

    def test_changed_runs_and_extra_jobs_are_visible(self):
        rows, extra = p.reconcile(plan(), [observed(runs=8), observed(12, installer_id=3)], NOW)
        self.assertEqual('changed', rows[0]['state'])
        self.assertEqual(12, extra[0]['job_id'])

    def test_old_jobs_do_not_match(self):
        rows, _ = p.reconcile(plan(), [observed(start_date='2030-01-02T07:00:00Z')], NOW)
        self.assertEqual('remaining', rows[0]['state'])

    def test_missing_jobs_timestamp_is_not_evidence_of_missed_install(self):
        rows, _ = p.reconcile(plan(), [], None)
        self.assertEqual('awaiting_refresh', rows[0]['state'])

    def test_whole_pool_covers_builder_with_empty_personal_hangar(self):
        doc = p.check_round(plan(), snapshot())
        self.assertEqual('remaining', doc['lines'][0]['state'])
        self.assertEqual('char:2', doc['holders'][0]['bulk_holder'])
        self.assertEqual(['Builder'], doc['installer_sequence'][FAC])
        self.assertEqual(0, doc['pool'][0]['short'])

    def test_two_jobs_cannot_spend_same_sixty_units(self):
        doc = p.check_round(plan([line(1), line(2)]), snapshot(100))
        self.assertEqual(['remaining', 'blocked'], [r['state'] for r in doc['lines']])
        self.assertEqual(20, doc['pool'][0]['short'])

    def test_delivery_pool_is_owned_but_not_installable(self):
        doc = p.check_round(plan(), snapshot(100, False))
        self.assertEqual('blocked', doc['lines'][0]['state'])
        self.assertEqual(100, doc['pool'][0]['move_to_hangar'])
        self.assertEqual(0, doc['pool'][0]['short'])

    def test_post_snapshot_jobs_make_balance_unknown(self):
        s = snapshot()
        s.jobs = [observed(installer_id=9, start_date='2030-01-03T08:01:00Z')]
        doc = p.check_round(plan(), s)
        self.assertIsNone(doc['pool'][0]['short'])
        self.assertEqual('awaiting_refresh', doc['lines'][0]['state'])

    def test_recent_transfer_is_labelled_user_confirmation(self):
        doc = p.check_round(plan(), snapshot(), [dict(holder_id=1, facility_id=FAC, at='2030-01-03T08:01:00Z')])
        self.assertTrue(any('not yet confirmed' in s for s in doc['warnings']))

    def test_consumed_copy_in_old_snapshot_cannot_be_reused(self):
        s = snapshot()
        s.blueprints = s.blueprints[:1]
        s.documents[2]['as_of'] = '2030-01-02T12:00:00Z'
        s.jobs = [observed(installer_id=9, status='delivered', completed_date=START, start_date='2030-01-02T13:00:00Z')]
        doc = p.check_round(plan(), s)
        self.assertEqual('blocked', doc['lines'][0]['state'])

    def test_schedule_moves_forward_from_current_time(self):
        row = p.check_round(plan(), snapshot())['lines'][0]
        self.assertEqual('2030-01-03T18:00:00Z', row['estimated_end'])

    def test_wrong_me_and_short_copy_do_not_qualify(self):
        s = snapshot()
        s.blueprints[0]['material_efficiency'] = 2
        s.blueprints[1]['runs'] = 3
        s.blueprints = s.blueprints[:2]
        self.assertEqual('blocked', p.check_round(plan(), s)['lines'][0]['state'])

    def test_unknown_assets_do_not_become_zero(self):
        s = snapshot(); s.documents[1]['available'] = False
        row = p.check_round(plan(), s)['pool'][0]
        self.assertIsNone(row['held']); self.assertIsNone(row['short'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger_db.connect(':memory:')
        self.addCleanup(self.conn.close)

    def test_one_active_round_per_site_and_close_preserves_plan(self):
        doc = plan(); ident = store.save_round(self.conn, doc)
        with self.assertRaises(RuntimeError):
            store.save_round(self.conn, doc)
        with self.conn:
            self.conn.execute('UPDATE production_rounds SET closed_at=? WHERE id=?', (NOW, ident))
        store.save_round(self.conn, doc)
        self.assertEqual(doc['lines'], store.get_round(self.conn, ident)['lines'])

    def test_facility_observations_are_immutable_and_effective_dated(self):
        store.record_facility(self.conn, FAC, START, {'material': {'manufacturing': .95}}, 'verified')
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('UPDATE facility_history SET reason="oops"')
        self.assertIsNone(store.profile_at(store.histories(self.conn), FAC, '2029-01-01T00:00:00Z'))
        self.assertEqual(.95, store.profile_at(store.histories(self.conn), FAC, NOW)['profile']['material']['manufacturing'])

    def test_sync_observation_is_not_repeated_or_backdated(self):
        op = operation.parse({'facilities': {'f': {'location_id': FAC, 'system': 'Jita', 'material': {'manufacturing': .9}}}})
        store.observe_facilities(self.conn, op, START)
        store.observe_facilities(self.conn, op, NOW)
        history = store.histories(self.conn)
        self.assertEqual(1, len(history)); self.assertEqual(START, history[0]['effective_at'])

    def test_nonfinite_multiplier_rejected(self):
        for value in (float('nan'), float('inf'), 0, -1):
            with self.assertRaises(RuntimeError):
                store.record_facility(self.conn, FAC, START, {'material': {'manufacturing': value}}, 'bad')


class ForecastTests(unittest.TestCase):
    def test_scheduled_demand_replaces_historical_baseline(self):
        check = dict(lines=[dict(line(), state='remaining', materials={'34': 120})])
        doc = reports.forecast(snapshot(100), {(FAC, 34): 70}, days=7, history_days=7, lead_days=2,
                               facilities={FAC}, plan_check=check)
        row = doc['items'][0]
        self.assertEqual(20, row['buy'])  # max(120,70)-100, not 120+70-100
        self.assertEqual(20, row['bridge_buy'])
        self.assertEqual(1, row['first_blocked'])

    def test_zero_usage_has_no_invented_depletion(self):
        doc = reports.forecast(snapshot(), {(FAC, 34): 0}, days=7, history_days=7, lead_days=2, facilities={FAC})
        self.assertIsNone(doc['items'][0]['depletion'])

    def test_partial_book_depth_is_not_priced_as_fully_filled(self):
        q = reports.depth_cost([dict(price=10, volume=2), dict(price=20, volume=3)], 8)
        self.assertEqual((5, 3, 80), (q['filled'], q['unfilled'], q['cost']))
        self.assertFalse(q['complete'])

    def test_fallback_is_before_exhaustion_by_travel_time(self):
        doc = reports.forecast(snapshot(30), {(FAC, 34): 70}, days=7, history_days=7, lead_days=2, facilities={FAC})
        r = doc['items'][0]
        self.assertEqual(2, (reports.moment(r['depletion']) - reports.moment(r['bid_fallback_by'])).days)


class ReplenishmentTests(unittest.TestCase):
    def test_sold_out_restock_listing_before_building(self):
        recipe = industry.Recipe(100, 'manufacturing', 200, 1, 3600, 10, {34: 6}, (), {})
        s = snapshot(); s.assets.append(dict(root=60003760, type_id=200, quantity=80, owner='char:2', usable=True))
        f = ledger.Facts([], [dict(is_buy=0, date=START, location_id=60003760, type_id=200, quantity=10)], [], [], [], {}, set())
        doc = reports.replenish(s, f, {100: recipe}, {'blueprints': {}, 'decryptors': {}}, ledger.Book(),
                                market_hub(), targets={200: 100})
        self.assertEqual('list', doc['items'][0]['action'])
        self.assertEqual(80, doc['items'][0]['list_or_haul'])

    def test_unlisted_at_factory_requires_haul_and_untested_line_needs_trial(self):
        recipe = industry.Recipe(100, 'manufacturing', 200, 1, 3600, 10, {34: 6}, (), {})
        s = snapshot(); s.assets.append(dict(root=FAC, type_id=200, quantity=20, owner='char:2', usable=True))
        f = ledger.Facts([], [], [], [], [], {}, set())
        args = (s, f, {100: recipe}, {'blueprints': {}, 'decryptors': {}}, ledger.Book(), market_hub())
        self.assertEqual('haul', reports.replenish(*args, targets={200: 10})['items'][0]['action'])
        self.assertEqual('set trial target', reports.replenish(*args)['items'][0]['action'])


def market_hub():
    from eve_skills import market
    return market.get_hub('jita')


class CostingTests(unittest.TestCase):
    def test_dated_profile_does_not_reprice_earlier_jobs(self):
        f = facts(jobs=[job(1, 1, blueprint_type=T2_BP, product=T2, runs=10, start=stamp(10), end=stamp(11), facility=FAC),
                        job(2, 1, blueprint_type=T2_BP, product=T2, runs=10, start=stamp(20), end=stamp(21), facility=FAC)],
                  opening={MAT: 10})
        baseline = replay(f)
        f.facility_history = [dict(id=1, facility_id=FAC, effective_at=stamp(15), profile={'material': {'manufacturing': .5}})]
        revised = replay(f)
        self.assertEqual(baseline.jobs[1].total, revised.jobs[1].total)
        self.assertLess(revised.jobs[2].materials, baseline.jobs[2].materials)
        self.assertEqual(1, revised.jobs[2].facility_observation_id)
        self.assertEqual(1, revised.notes['structure_jobs'])

    def test_invention_cost_spreads_failed_attempts_and_adds_decryptor(self):
        recipe = industry.Recipe(100, 'manufacturing', 200, 1, 3600, 10, {34: 100}, (), {})
        source = {'m': {'40': 2}, 't': 60}
        prices = industry.Prices({40: 10, 50: 100}, {34: 1})
        q = scan.invention_quote(recipe, source, [100, 1, .5], {'type_id': 50, 'probability': 1, 'runs': 1},
                                 prices, industry.Facility(0, 0, scc_surcharge=0))
        self.assertEqual(120, q['cost_per_unit'])  # (20+100) / (.5*2)

    def test_absent_datacore_book_never_becomes_free(self):
        recipe = industry.Recipe(100, 'manufacturing', 200, 1, 3600, 10, {34: 100}, (), {})
        q = scan.invention_quote(recipe, {'m': {'40': 2}}, [100, 1, .5], None,
                                 industry.Prices({}, {34: 1}), industry.Facility(0))
        self.assertIsNone(q['cost_per_unit'])

    def test_skill_unknown_is_not_eligible(self):
        self.assertIsNone(scan.eligibility(None, {}))
        self.assertFalse(scan.eligibility({34: 4}, {34: 3}))
        self.assertTrue(scan.eligibility({34: 4}, {34: 4}))


class CliTests(unittest.TestCase):
    def test_every_entrypoint_parses(self):
        parser = cli.build_parser()
        for args in (['round', 'list'], ['round', 'check'], ['supply', '--no-prices'],
                     ['replenish', '--target', 'Example=10'], ['scan', 'Example'], ['facility', 'impact']):
            self.assertEqual('production', parser.parse_args(['production', *args]).command)

    def test_round_save_check_close_end_to_end_in_temporary_ledger(self):
        from tests.fake_esi import FakeEsiEnv, BUILD_ARTICLE, ADA
        env = FakeEsiEnv(); env.start(); self.addCleanup(env.stop)
        env.install_core(); env.install_build_cost()
        op = operation.parse({'default_site': 'example', 'sites': {'example': {'owner': 'personal', 'facility': 'f', 'characters': ['Ada']}},
                              'facilities': {'f': {'location_id': FAC, 'system': 'Jita'}}})
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'round.json')
            with open(path, 'w') as fh:
                json.dump({'lines': [{'product': str(BUILD_ARTICLE), 'installer': 'Ada', 'runs': 1}]}, fh)
            with mock.patch('eve_skills.cmd_production.operation.load', return_value=op):
                code, out, err = env.run(['production', 'round', 'save', path, '--json'])
                self.assertEqual(0, code, err)
                ident = json.loads(out)['id']
                with mock.patch('eve_skills.production_data.read_site', return_value=snapshot()):
                    code, out, err = env.run(['production', 'round', 'check', str(ident), '--json'])
                    self.assertEqual(0, code, err)
                    self.assertIn('pool', json.loads(out))
                code, out, err = env.run(['production', 'round', 'close', str(ident), '--json'])
                self.assertEqual(0, code, err)
                self.assertIsNotNone(json.loads(out)['closed_at'])


class LiveCliTests(unittest.TestCase):
    """Exercise real CLI, ESI adapter and ledger paths with isolated on-disk state."""
    def setUp(self):
        from tests import fake_esi as f
        self.f = f
        self.env = f.FakeEsiEnv(); self.env.start(); self.addCleanup(self.env.stop)
        self.env.install_core(); self.env.install_build_cost(); self.env.install_standings()
        self.op = operation.parse({'default_site': 'example', 'sites': {'example': {
            'owner': 'personal', 'facility': 'f', 'characters': ['Ada'], 'seller': 'Ada', 'hub': 'jita'}},
            'facilities': {'f': {'location_id': FAC, 'system': str(f.SYSTEM_FORGE), 'material': {'manufacturing': .95}}}})
        patch = mock.patch('eve_skills.operation.load', return_value=self.op)
        patch.start(); self.addCleanup(patch.stop)
        self.conn = ledger_db.connect(); self.addCleanup(self.conn.close)
        self.headers = {'Last-Modified': f.http_date(-10), 'Expires': f.http_date(300)}
        self.env.server.get(f'/universe/stations/{f.STATION_JITA}', doc={'owner': 1000125})
        self.docs = {
            'assets': [dict(item_id=1000, type_id=f.BUILD_CELL, location_id=f.STATION_JITA,
                            location_flag='Hangar', quantity=80)],
            'blueprints': [], 'industry/jobs': [],
        }
        for section, rows in self.docs.items():
            self.env.server.get(f'/characters/{f.ADA.character_id}/{section}', token=f.ADA.token,
                                doc=rows, headers=self.headers)
        self.env.server.get(f'/characters/{f.ADA.character_id}/orders', token=f.ADA.token,
                            doc=[], headers=self.headers)

    def run_json(self, *args):
        code, out, err = self.env.run(['production', *args, '--json'])
        self.assertEqual(0, code, err)
        return json.loads(out)

    def test_live_replenishment_reads_hub_holdings_before_suggesting_build(self):
        doc = self.run_json('replenish', '--target', f'{self.f.BUILD_CELL}=100')
        row = next(r for r in doc['items'] if r['type_id'] == self.f.BUILD_CELL)
        self.assertEqual(('list', 80), (row['action'], row['held_at_hub']))
        self.assertTrue(any(d['section'] == 'orders' and d['available'] for d in doc['documents']))

    def test_supply_reads_saved_demand_and_does_not_count_bids_as_stock(self):
        self.env.server.get(f'/characters/{self.f.ADA.character_id}/orders', token=self.f.ADA.token,
                            doc=[dict(order_id=3, type_id=self.f.BUILD_GOO, is_buy_order=True,
                                      volume_remain=500, location_id=self.f.STATION_JITA)], headers=self.headers)
        doc = plan([dict(line(), materials={str(self.f.BUILD_GOO): 100}, start=self.f.iso(-60),
                        installer_id=self.f.ADA.character_id, blueprint_type_id=930030, product_id=self.f.BUILD_CELL)])
        doc['start'] = self.f.iso(-60)
        store.save_round(self.conn, doc)
        report = self.run_json('supply', '--no-prices')
        row = report['items'][0]
        self.assertEqual((100, 500), (row['buy'], row['open_bid_units']))
        self.assertEqual(1, row['first_blocked'])
        self.assertIsNotNone(row['bid_fallback_by'])

    def test_supply_quotes_real_depth_with_partial_fill(self):
        doc = plan([dict(line(), materials={str(self.f.BUILD_GOO): 5000}, start=self.f.iso(-60),
                        installer_id=self.f.ADA.character_id, blueprint_type_id=930030, product_id=self.f.BUILD_CELL)])
        doc['start'] = self.f.iso(-60)
        store.save_round(self.conn, doc)
        self.env.server.get(f'/universe/types/{self.f.BUILD_GOO}', doc={'name': 'Raw input', 'packaged_volume': 1, 'volume': 1, 'group_id': 1})
        report = self.run_json('supply')
        quote = report['items'][0]['hub_purchase']
        self.assertEqual((4000, 1000, 20000), (quote['filled'], quote['unfilled'], quote['cost']))
        self.assertFalse(quote['complete'])

    def test_facility_preview_has_no_write_then_apply_is_immutable(self):
        args = ('facility', 'record', '--facility', 'f', '--effective', START, '--reason', 'Test evidence')
        preview = self.run_json(*args)
        self.assertFalse(preview['applied'])
        self.assertFalse(store.histories(self.conn))
        applied = self.run_json(*args, '--apply')
        self.assertTrue(applied['applied'])
        self.assertEqual(1, len(store.histories(self.conn)))
        self.assertEqual('unverified', self.run_json('facility', 'impact')['fee_quality']['status'])

    def test_scan_uses_cost_engine_without_requiring_an_unused_lab_index(self):
        self.env.server.get(f'/markets/{self.f.MARKET_FORGE}/history', doc=[dict(
            date=self.f.iso(-86400)[:10], average=12, highest=13, lowest=11, volume=3000, order_count=100)])
        doc = self.run_json('scan', str(self.f.BUILD_CELL), '--seller', 'Ada')
        row = doc['items'][0]
        self.assertGreater(row['cost_per_unit'], 5)
        self.assertEqual(8.99, row['conservative_price'])
        self.assertIn('regional share', row['demand_basis'])
        self.assertIsNone(row['realized'])
        self.assertGreater(row['daily_units'], 0)

    def test_t2_scan_prices_invention_and_identifies_lab_bottleneck(self):
        self.env._write_json(os.path.join(self.env.data_home, 'eve-skills', 'blueprint_invention.json'), {
            'blueprints': {'930000': {'m': {str(self.f.BUILD_GOO): 2}, 'p': [[930030, 2, .5]], 't': 86400, 's': {}}},
            'decryptors': {}})
        self.env.server.get('/industry/systems', doc=[{'solar_system_id': self.f.SYSTEM_FORGE,
            'cost_indices': [{'activity': 'manufacturing', 'cost_index': .1575}, {'activity': 'invention', 'cost_index': .1}]}])
        self.env.server.get(f'/markets/{self.f.MARKET_FORGE}/history', doc=[dict(
            date=self.f.iso(-86400)[:10], average=12, highest=13, lowest=11, volume=300000, order_count=100)])
        row = self.run_json('scan', str(self.f.BUILD_CELL), '--seller', 'Ada')['items'][0]
        self.assertGreater(row['invention_cost'], 1)
        self.assertAlmostEqual(2.4, row['lab_hours'])
        self.assertEqual('labs', row['bottleneck'])
        self.assertAlmostEqual(10, row['daily_units'])

    def test_scan_caps_own_hub_sales_and_compares_realized_job_cost(self):
        ledger_db.upsert_jobs(self.conn, 'char:1', [dict(job_id=11, activity_id=1, installer_id=self.f.ADA.character_id,
            facility_id=FAC, blueprint_id=17, blueprint_type_id=930030, product_type_id=self.f.BUILD_CELL,
            runs=1, cost=20, status='delivered', start_date=self.f.iso(-172800), end_date=self.f.iso(-86400))], self.f.iso())
        ledger_db.insert_transactions(self.conn, 'char:1', [dict(transaction_id=21, is_buy=False,
            type_id=self.f.BUILD_CELL, quantity=30, unit_price=9, client_id=2,
            location_id=self.f.STATION_JITA, date=self.f.iso(-86400))])
        self.conn.commit()
        self.env.server.get(f'/markets/{self.f.MARKET_FORGE}/history', doc=[dict(
            date=self.f.iso(-86400)[:10], average=12, highest=13, lowest=11, volume=300000, order_count=100)])
        # A real prior job must not shadow the realized accounting map in the scan.
        self.env.server.get(f'/characters/{self.f.ADA.character_id}/industry/jobs', token=self.f.ADA.token,
                            doc=[dict(observed(), blueprint_type_id=930030)], headers=self.headers)
        row = self.run_json('scan', str(self.f.BUILD_CELL), '--seller', 'Ada')['items'][0]
        self.assertEqual(1, row['own_hub_sales_per_day'])
        self.assertEqual(1, row['daily_units'])
        self.assertIsNotNone(row['realized'])
        self.assertAlmostEqual(row['cost_per_unit'] - row['realized']['unit_cost'],
                               row['projected_minus_realized_unit_cost'])

    def test_missing_asset_document_suppresses_replenishment_action(self):
        self.env.server.get(f'/characters/{self.f.ADA.character_id}/assets', token=self.f.ADA.token,
                            error=(403, {'error': 'not authorized'}))
        row = self.run_json('replenish', '--target', f'{self.f.BUILD_CELL}=100')['items'][0]
        self.assertEqual('refresh', row['action'])
        self.assertIsNone(row['held_at_hub'])
        self.assertIsNone(row['production_gap'])


class AdditionalInvariantTests(unittest.TestCase):
    def test_blueprint_deliveries_is_a_blocker_even_with_usable_inputs(self):
        snap = snapshot()
        for bp in snap.blueprints:
            bp['flag'] = 'Deliveries'
        row = p.check_round(plan(), snap)['lines'][0]
        self.assertEqual('blocked', row['state'])
        self.assertIn('blueprint from Deliveries', ' '.join(row['blockers']))

    def test_usable_inputs_are_reserved_across_all_remaining_jobs(self):
        snap = snapshot(60)
        snap.assets.append(dict(root=FAC, type_id=34, quantity=60, owner='char:2', usable=False))
        doc = p.check_round(plan([line(1), line(2)]), snap)
        self.assertEqual(['remaining', 'blocked'], [r['state'] for r in doc['lines']])
        self.assertEqual(0, doc['pool'][0]['short'])

    def test_a_job_before_a_later_line_cannot_satisfy_that_line(self):
        doc = plan([dict(line(), start=NOW)])
        self.assertEqual('remaining', p.reconcile(doc, [observed()], NOW)[0][0]['state'])

    def test_copy_acquisition_is_spread_across_expected_successes(self):
        recipe = industry.Recipe(100, 'manufacturing', 200, 1, 3600, 10, {34: 100}, (), {})
        q = scan.invention_quote(recipe, {'m': {'40': 2}}, [100, 1, .5], None,
                                 industry.Prices({40: 10}, {34: 1}),
                                 industry.Facility(0, 0, scc_surcharge=0), copy_cost=100)
        self.assertEqual(240, q['cost_per_unit'])


class PipelineTests(unittest.TestCase):
    @mock.patch('eve_skills.cmd_production.sso.resolve_character', return_value=1)
    def test_saved_invention_respects_horizon_and_builds_already_planned(self, resolve):
        op = operation.parse({'sites': {'example': {'owner': 'personal', 'facility': 'f', 'characters': ['Builder']}},
                              'facilities': {'f': {'location_id': FAC, 'system': 'Jita'}}})
        recipe = industry.Recipe(100, 'manufacturing', 200, 1, 3600, 10, {34: 6}, (), {})
        snap = snapshot()
        snap.jobs = [observed(status='delivered')]
        lines = [dict(line(), state='remaining'),
                 dict(line(2), activity_id=8, state='remaining', expected_units=15,
                      estimated_end='2030-01-03T12:00:00Z')]
        args = (snap, ledger.Book(), {100: recipe}, {'blueprints': {}, 'decryptors': {}}, op, op.site('example'), 1)
        needed, pipeline = cmd_production._pipeline_materials(*args, {'lines': lines})
        self.assertEqual(10, pipeline[0]['already_planned_units'])
        self.assertEqual(5, pipeline[0]['forecast_units'])
        self.assertEqual(30, needed[FAC, 34])
        lines[1]['estimated_end'] = '2030-01-10T12:00:00Z'
        needed, pipeline = cmd_production._pipeline_materials(*args, {'lines': lines})
        self.assertEqual(0, pipeline[0]['forecast_units'])
        self.assertEqual(0, sum(needed.values()))

    def test_sync_rejects_ambiguous_aliases_without_partial_observations(self):
        op = operation.parse({'facilities': {
            'a': {'location_id': FAC, 'material': {'manufacturing': .9}},
            'b': {'location_id': FAC, 'material': {'manufacturing': .8}}}})
        with contextlib.closing(ledger_db.connect(':memory:')) as conn:
            with self.assertRaisesRegex(RuntimeError, 'conflicting facility aliases'):
                store.observe_facilities(conn, op, START)
            self.assertFalse(store.histories(conn))
