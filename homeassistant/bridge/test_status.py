"""Synthetic status migration, ordering, persistence, units and privacy tests."""
import copy
import ast
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import time

from bridge import Bridge, NANOSECOND, iso_time
from status_fields import SPECS, STATUS_CONFIG, decode_status
from test_bridge import CONFIG, Clock, TEST_VIN

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("_stream_model_test")
package.__path__ = [str(ROOT / "integration/tesla_fleet_stream")]
sys.modules[package.__name__] = package
model_module = importlib.import_module(package.__name__ + ".model")
StatusModel = model_module.StatusModel


def raw_for(name):
    spec = SPECS[name]
    kind = spec['kind']
    if kind in ('number', 'integer'):
        return {'int_value': max(1, spec['low'])}
    if kind == 'boolean': return {'boolean_value': False}
    if kind == 'string': return {'string_value': 'Synthetic value'}
    if kind == 'enum': return {spec['wire']: next(iter(spec['values']))}
    if kind == 'doors': return {'door_value': {}}
    if kind == 'tires': return {'tire_location_value': {}}
    if kind == 'location': return {'location_value': {'latitude': 10.0, 'longitude': 20.0}}
    if kind == 'hvac': return {'hvac_power_value': 'HvacPowerStateOff'}
    raise AssertionError('Unhandled synthetic type')


def snapshot(now=1700000000):
    return {'schema': 1, 'vehicle': 'test_car', 'observed_at': iso_time(now * NANOSECOND),
            'fields': {k: {'observed_at': iso_time(now * NANOSECOND), 'value': raw_for(k)} for k in SPECS},
            'invalid_fields': [], 'connection': True, 'connection_observed_at': iso_time(now * NANOSECOND)}


class StatusTests(unittest.TestCase):
    def test_every_requested_field_decodes_to_exact_keys(self):
        self.assertLessEqual(len(STATUS_CONFIG), 150)
        for name, spec in SPECS.items():
            with self.subTest(name=name):
                self.assertEqual(set(decode_status(name, raw_for(name))), set(spec['keys']))
                self.assertEqual(set(decode_status(name, {'invalid': True})), set(spec['keys']))
                with self.assertRaises(ValueError): decode_status(name, {'secret': 'not a supported value'})

    def test_units_enum_polarity_and_false_values(self):
        self.assertEqual(decode_status('TimeToFullCharge', {'double_value': 1.5})['charge_state_minutes_to_full_charge'], 90)
        self.assertEqual(decode_status('MediaNowPlayingElapsed', {'long_value': '120000'})['vehicle_state_media_info_now_playing_elapsed'], 120000)
        self.assertEqual(decode_status('DoorState', {'door_value': {'DriverFront': True}}),
                         {'vehicle_state_df': 1, 'vehicle_state_dr': 0, 'vehicle_state_pf': 0, 'vehicle_state_pr': 0, 'vehicle_state_ft': 0, 'vehicle_state_rt': 0})
        self.assertEqual(decode_status('ChargingCableType', {'invalid': True})['charge_state_conn_charge_cable'], '<invalid>')
        self.assertIsNone(decode_status('FdWindow', {'window_state_value': 'WindowStateUnknown'})['vehicle_state_fd_window'])
        self.assertFalse(decode_status('HvacPower', {'hvac_power_value': 'HvacPowerStateOff'})['climate_state_is_climate_on'])
        self.assertEqual(decode_status('TpmsPressureRr', {'double_value': 2.6}), {'vehicle_state_tpms_pressure_rr': 2.6})

    def test_malformed_and_unbounded_values_rejected(self):
        for name, value in [('BatteryLevel', {'double_value': True}), ('BatteryLevel', {'double_value': float('nan')}),
                            ('SeatHeaterLeft', {'int_value': 4}), ('Locked', {'boolean_value': 1}),
                            ('Version', {'string_value': 'x' * 2049}), ('DoorState', {'door_value': {'DriverFront': 'true'}}),
                            ('Location', {'location_value': {'latitude': 91}})]:
            with self.subTest(name=name), self.assertRaises(ValueError): decode_status(name, value)

    def test_model_rejects_missing_baseline_then_clears_polled_status(self):
        m = StatusModel('test_car')
        with self.assertRaises(ValueError): m.native_data({}, 1700000000)
        m.accept(snapshot(), 1700000000)
        self.assertTrue(m.ready)
        data = m.native_data({'vehicle_state_dashcam_state': 'Recording', 'charge_state_trip_charging': True,
                              'vehicle_config_has_ludicrous_mode': False, 'climate_state_min_avail_temp': 15}, 1700000000)
        self.assertNotIn('vehicle_state_dashcam_state', data)
        self.assertNotIn('charge_state_trip_charging', data)
        self.assertFalse(data['vehicle_config_has_ludicrous_mode'])
        self.assertEqual(data['climate_state_min_avail_temp'], 15)
        self.assertIsNone(data['vehicle_state_software_update_status'])

    def test_snapshot_validation_is_atomic_and_ordered(self):
        m = StatusModel('test_car'); base = snapshot(); m.accept(base, 1700000000)
        previous = copy.deepcopy(m.fields)
        invalid = copy.deepcopy(base)
        invalid['observed_at'] = iso_time(1700000001 * NANOSECOND)
        invalid['fields']['BatteryLevel'] = {'observed_at': invalid['observed_at'], 'value': {'double_value': 55}}
        invalid['fields']['Locked']['value'] = {'boolean_value': True}
        with self.assertRaises(ValueError): m.accept(invalid, 1700000001)
        self.assertEqual(m.fields, previous)
        for change in ({'vehicle': 'other_car'}, {'observed_at': iso_time(1700000006 * NANOSECOND)},
                       {'fields': {'unknown_field': {'value': {}, 'observed_at': base['observed_at']}}}):
            with self.assertRaises(ValueError): m.accept(base | change, 1700000000)
        self.assertFalse(m.accept(base, 1700000000))
        self.assertEqual(m.revision, 1)

    def test_power_source_and_rhd_and_countdown(self):
        s = snapshot(); fields = s['fields']
        for name, value in {'ACChargingPower': {'double_value': 7.2}, 'DCChargingPower': {'double_value': 120},
                            'FastChargerPresent': {'boolean_value': False}, 'RightHandDrive': {'boolean_value': True},
                            'HvacLeftTemperatureRequest': {'double_value': 20}, 'HvacRightTemperatureRequest': {'double_value': 22},
                            'TimeToFullCharge': {'double_value': 1.5}}.items(): fields[name]['value'] = value
        m = StatusModel('test_car'); m.accept(s, 1700000000)
        data = m.native_data({}, 1700000060)
        self.assertEqual(data['charge_state_charger_power'], 7.2)
        self.assertEqual(data['climate_state_driver_temp_setting'], 22)
        self.assertEqual(data['climate_state_passenger_temp_setting'], 20)
        self.assertEqual(data['charge_state_minutes_to_full_charge'], 89)

    def test_bridge_persists_all_status_without_replaying_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock(); messages = []
            settings = copy.deepcopy(CONFIG) | {'state_file': str(Path(directory) / 'state.json')}
            bridge = Bridge(settings, lambda *a, **kw: messages.append((a, kw)), lambda: clock.wall, lambda: clock.monotonic)
            record = {'vin': TEST_VIN, 'is_resend': False, 'created_at': iso_time(int(clock.wall * NANOSECOND)),
                      'data': [{'key': k, 'value': raw_for(k)} for k in SPECS]}
            bridge.receive(f'test_receiver/{TEST_VIN}/records', json.dumps(record).encode())
            saved = json.loads(Path(settings['state_file']).read_text())
            self.assertEqual(saved['version'], 3)
            self.assertNotIn(TEST_VIN, json.dumps(saved))
            original = saved['vehicles']['test_car']['status_fields']
            clock.advance(300); messages.clear()
            restored = Bridge(settings, lambda *a, **kw: messages.append((a, kw)), lambda: clock.wall, lambda: clock.monotonic)
            restored.resync()
            published = [json.loads(a[1]) for a, kw in messages if a[0].endswith('/fleet_status/state')]
            self.assertEqual(published[-1]['fields'], original)
            self.assertFalse(any(a[0].endswith('/driver_present/state') for a, kw in messages))
            restored.receive(f'test_receiver/{TEST_VIN}/records', json.dumps(record).encode())
            self.assertEqual(restored.vehicles[TEST_VIN].status_fields, original)

    def test_public_and_companion_catalogs_match(self):
        self.assertEqual((ROOT/'bridge/status_fields.py').read_bytes(),
                         (ROOT/'integration/tesla_fleet_stream/status_fields.py').read_bytes())


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    """Execute the actual adapter class against isolated coordinator contracts."""
    def setUp(self):
        source = ast.parse((ROOT/'integration/tesla_fleet_stream/__init__.py').read_text())
        klass = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'StreamAdapter')
        globals_ = {'StatusModel': StatusModel, 'callback': lambda f: f, 'time': time,
                    'async_dispatcher_send': lambda *args: None, 'SIGNAL': 'synthetic'}
        exec(compile(ast.Module(body=[klass], type_ignores=[]), '<reviewed adapter>', 'exec'), globals_)
        self.Adapter = globals_['StreamAdapter']
        self.updates = []
        self.coordinator = SimpleNamespace(data={'vehicle_config_rhd': False}, updated_once=False,
                                          async_set_updated_data=self.updates.append)
        self.vehicle = SimpleNamespace(device={'name': 'Test car'}, coordinator=self.coordinator)
        self.entry = SimpleNamespace(disabled_by=None, pref_disable_polling=False,
                                     runtime_data=SimpleNamespace(vehicles=[self.vehicle], energysites=[]))
        self.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: [self.entry]))
        self.adapter = self.Adapter(self.hass, [{'name': 'Test car', 'slug': 'test_car'}], 'test_live')
        self.adapter.models['test_car'].accept(snapshot(), 1700000000)

    async def test_no_mix_with_polling_then_update_existing_coordinator_only(self):
        await self.adapter.bind()
        self.assertEqual(self.updates, [])
        self.assertEqual(self.adapter.status['test_car'], 'ready_to_disable_polling')
        self.entry.pref_disable_polling = True
        await self.adapter.bind()
        self.assertEqual(len(self.updates), 1)
        self.assertTrue(self.coordinator.updated_once)
        self.assertEqual(self.updates[0]['_tesla_stream_source'], 'fleet_telemetry')
        self.assertEqual(self.updates[0]['state'], 'offline')  # Bridge availability not asserted.
        self.assertEqual(self.adapter.status['test_car'], 'streaming')

    async def test_uncovered_vehicle_or_energy_site_prevents_binding(self):
        self.entry.runtime_data.vehicles.append(SimpleNamespace(device={'name': 'Other car'}))
        await self.adapter.bind()
        self.assertEqual(self.adapter.status['test_car'], 'vehicle_mapping_mismatch')
        self.assertEqual(self.updates, [])
        self.entry.runtime_data.vehicles.pop()
        self.entry.runtime_data.energysites = [object()]
        await self.adapter.bind()
        self.assertEqual(self.adapter.status['test_car'], 'unsupported_native_layout')

    async def test_rebind_after_native_reload_uses_new_coordinator(self):
        self.entry.pref_disable_polling = True
        await self.adapter.bind()
        replacement = []
        self.vehicle.coordinator = SimpleNamespace(data={'vehicle_config_rhd': False}, updated_once=False,
                                                   async_set_updated_data=replacement.append)
        await self.adapter.bind()
        self.assertEqual(len(replacement), 1)
        self.assertEqual(len(self.updates), 1)

    async def test_no_unknown_cover_baseline_is_invented(self):
        self.entry.pref_disable_polling = True
        self.adapter.models['test_car'] = StatusModel('test_car')
        await self.adapter.bind()
        self.assertEqual(self.updates, [])
        self.assertEqual(self.adapter.status['test_car'], 'waiting_for_stream_baseline')

    async def test_inferred_online_expires_without_new_stream_or_vehicle_reads(self):
        self.entry.pref_disable_polling = True
        self.adapter.bridge_available = True
        self.adapter.models['test_car'].connection = None
        with patch.object(time, 'time', return_value=1700000001):
            await self.adapter.bind()
        self.assertEqual(self.updates[-1]['state'], 'online')
        self.coordinator.data = self.updates[-1]
        with patch.object(time, 'time', return_value=1700000091):
            await self.adapter.bind()
        self.assertEqual(self.updates[-1]['state'], 'offline')
        self.assertEqual(len(self.updates), 2)


if __name__ == '__main__': unittest.main()
