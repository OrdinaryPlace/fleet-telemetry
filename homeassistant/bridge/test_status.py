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
from datetime import timedelta

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

    def test_additional_fields_are_distinct_and_keep_invalid_unknown(self):
        self.assertEqual(decode_status('EstimatedHoursToChargeTermination', {'double_value': 1.5}),
                         {'charge_state_hours_to_charge_limit': 1.5})
        self.assertEqual(decode_status('OriginLocation', {'location_value': {'latitude': 10, 'longitude': 20}}),
                         {'drive_state_active_route_origin_latitude': 10, 'drive_state_active_route_origin_longitude': 20})
        for name in ('LocatedAtHome', 'HomelinkNearby'):
            self.assertEqual(list(decode_status(name, {'boolean_value': False}).values()), [False])
            self.assertEqual(list(decode_status(name, {'invalid': True}).values()), [None])
        self.assertEqual(len(STATUS_CONFIG), 90)

    def test_extra_entities_preserve_source_and_clear_invalid_without_invented_location(self):
        # Execute the actual entity classes with the HA entity bases isolated.
        class Entity: pass
        class SensorEntity(Entity): pass
        class BinarySensorEntity(Entity): pass
        class TrackerEntity(Entity): pass
        from datetime import datetime, timezone
        scope = dict(Entity=Entity, SensorEntity=SensorEntity, BinarySensorEntity=BinarySensorEntity,
                     TrackerEntity=TrackerEntity, SPECS=SPECS, datetime=datetime, timezone=timezone,
                     SourceType=SimpleNamespace(GPS='gps'), SensorDeviceClass=SimpleNamespace(DURATION='duration'))
        for filename, classname in [('entity.py', 'StreamField'), ('sensor.py', 'StreamDuration'),
                                     ('binary_sensor.py', 'StreamFlag'), ('device_tracker.py', 'StreamOrigin')]:
            tree = ast.parse((ROOT/'integration/tesla_fleet_stream'/filename).read_text())
            klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == classname)
            exec(compile(ast.Module(body=[klass], type_ignores=[]), filename, 'exec'), scope)
        m = StatusModel('test_car')
        adapter = SimpleNamespace(models={'test_car': m}, bridge_available=True)
        spec = {'slug': 'test_car', 'name': 'Test car'}
        origin = scope['StreamOrigin'](adapter, spec)
        flag = scope['StreamFlag'](adapter, spec, 'LocatedAtHome', 'tesla_home', 'Tesla home')
        duration = scope['StreamDuration'](adapter, spec, 'EstimatedHoursToChargeTermination', 'hours_to_charge_limit', 'Hours', 'h')
        self.assertFalse(origin.available)
        self.assertIsNone(origin.latitude)
        self.assertIsNone(flag.is_on)
        self.assertIsNone(duration.native_value)
        m.accept(snapshot(), 1700000000)
        self.assertTrue(origin.available)
        self.assertEqual((origin.latitude, origin.longitude), (10, 20))
        self.assertFalse(flag.is_on)
        observed = origin.extra_state_attributes['observed_at']
        newer = snapshot(1700000060)
        for name in ('OriginLocation', 'LocatedAtHome', 'EstimatedHoursToChargeTermination'):
            newer['fields'][name]['value'] = {'invalid': True}
        newer['invalid_fields'] = ['OriginLocation', 'LocatedAtHome', 'EstimatedHoursToChargeTermination']
        m.accept(newer, 1700000060)
        self.assertFalse(origin.available)
        self.assertIsNone(origin.latitude)
        self.assertIsNone(origin.longitude)
        self.assertIsNone(flag.is_on)
        self.assertIsNone(duration.native_value)
        self.assertNotEqual(origin.extra_state_attributes['observed_at'], observed)
        self.assertTrue(origin.extra_state_attributes['reported_invalid'])

    def test_public_and_companion_catalogs_match(self):
        self.assertEqual((ROOT/'bridge/status_fields.py').read_bytes(),
                         (ROOT/'integration/tesla_fleet_stream/status_fields.py').read_bytes())


class ActivityEventTests(unittest.TestCase):
    def base(self):
        model = StatusModel('test_car')
        base = snapshot()
        base['fields']['DetailedChargeState']['value'] = {'detailed_charge_state_value': 'DetailedChargeStateCharging'}
        base['fields']['SoftwareUpdateVersion']['value'] = {'invalid': True}
        base['invalid_fields'] = ['SoftwareUpdateVersion']
        model.accept(base, 1700000000)
        return model

    def change(self, model, changes, at=1700000001, now=None, invalid=()):
        before, quality = dict(model.fields), set(model.invalid)
        data = snapshot(at)
        for field, raw in changes.items(): data['fields'][field]['value'] = raw
        data['invalid_fields'] = list(invalid)
        model.accept(data, now or at)
        return model.activity_changes(before, quality, now or at)

    def test_selected_real_changes_and_pressure_units(self):
        model = self.base()
        events = self.change(model, {
            'DetailedChargeState': {'detailed_charge_state_value': 'DetailedChargeStateComplete'},
            'DoorState': {'door_value': {'DriverFront': True, 'TrunkRear': True}},
            'TpmsSoftWarnings': {'tire_location_value': {'rear_right': True}},
            'TpmsPressureRr': {'double_value': 2.1},
            'Version': {'string_value': '2026.1.1 build'},
            'SoftwareUpdateVersion': {'string_value': '2026.2.1'},
        })
        self.assertEqual([e['kind'] for e in events], ['charge_complete', 'door_opened', 'tire_pressure_warning', 'software_update_available'])
        self.assertEqual(events[1]['position'], 'front_driver')
        self.assertEqual(events[2]['position'], 'rear_right')
        self.assertEqual(events[2]['pressure_bar'], 2.1)
        self.assertNotIn('latitude', json.dumps(events))
        self.assertEqual(events[3]['version'], '2026.2.1')

    def test_startup_stale_duplicate_invalid_and_future_are_not_events(self):
        model = self.base()
        self.assertEqual(model.activity_changes({}, set(), 1700000000), [])
        self.assertEqual(model.activity_changes(dict(model.fields), set(model.invalid), 1700000000), [])
        change = {'DetailedChargeState': {'detailed_charge_state_value': 'DetailedChargeStateComplete'}}
        self.assertEqual(self.change(model, change, now=1700000032), [])
        model = self.base()
        self.assertEqual(self.change(model, {'DoorState': {'invalid': True}}, invalid=['DoorState']), [])
        self.assertEqual(self.change(model, {'DoorState': {'door_value': {'DriverFront': True}}}, at=1700000002), [])
        model = self.base()
        with self.assertRaises(ValueError): self.change(model, change, at=1700000010, now=1700000001)

    def test_update_is_not_repeated_or_current_installed_version(self):
        for offered in ('', ' ', '2026.1.1'):
            model = self.base()
            events = self.change(model, {'Version': {'string_value': '2026.1.1 build'},
                                          'SoftwareUpdateVersion': {'string_value': offered}})
            self.assertEqual(events, [])
        model = self.base()
        self.assertEqual(len(self.change(model, {'SoftwareUpdateVersion': {'string_value': '2026.2.1'}})), 1)
        self.assertEqual(self.change(model, {'SoftwareUpdateVersion': {'string_value': '2026.2.1'}}, at=1700000002), [])

    def test_tire_rearms_only_after_clear_and_missing_pressure_is_not_zero(self):
        model = self.base()
        changes = {'TpmsSoftWarnings': {'tire_location_value': {'front_left': True}}, 'TpmsPressureFl': {'invalid': True}}
        events = self.change(model, changes, invalid=['TpmsPressureFl'])
        self.assertIsNone(events[0]['pressure_bar'])
        self.assertEqual(self.change(model, changes, at=1700000002, invalid=['TpmsPressureFl']), [])
        self.assertEqual(self.change(model, {'TpmsSoftWarnings': {'tire_location_value': {}}}, at=1700000003), [])
        self.assertEqual(len(self.change(model, changes, at=1700000004)), 1)


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

    async def test_mqtt_callback_suppresses_retained_events_and_only_emits_fresh_changes(self):
        callbacks, events = {}, []
        async def subscribe(hass, topic, receive, qos):
            callbacks[topic] = receive
            return lambda: None
        self.hass.bus = SimpleNamespace(async_listen_once=lambda *a: None,
                                       async_fire=lambda kind, data: events.append((kind, data)))
        self.Adapter.start.__globals__.update(mqtt=SimpleNamespace(async_subscribe=subscribe),
            async_track_time_interval=lambda *a: lambda: None, json=json,
            DOMAIN='tesla_fleet_stream', LOGGER=SimpleNamespace(warning=lambda *a: None),
            timedelta=timedelta, EVENT_HOMEASSISTANT_STOP='stop')
        await self.adapter.start()
        receive = callbacks['test_live/test_car/fleet_status/state']
        for at, door, retained in ((1700000001, True, True), (1700000002, False, False),
                                   (1700000003, True, False), (1700000003, True, False)):
            data = snapshot(at)
            data['fields']['DoorState']['value'] = {'door_value': {'DriverFront': door}}
            with patch.object(time, 'time', return_value=at):
                receive(SimpleNamespace(payload=json.dumps(data), retain=retained))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 'tesla_fleet_stream_activity')
        self.assertEqual(events[0][1]['vehicle'], 'test_car')
        self.assertEqual(events[0][1]['kind'], 'door_opened')

    async def test_shutdown_cleans_subscriptions_but_not_its_removed_one_shot(self):
        cleaned = []
        listener = {}
        async def subscribe(*args):
            return lambda: cleaned.append('mqtt')
        def listen_once(event, callback):
            listener['callback'] = callback
            def unsubscribe():
                if listener.get('removed'):
                    raise AssertionError('one-shot already removed by HA')
            return unsubscribe
        self.hass.bus = SimpleNamespace(async_listen_once=listen_once)
        self.Adapter.start.__globals__.update(mqtt=SimpleNamespace(async_subscribe=subscribe),
            async_track_time_interval=lambda *args: lambda: cleaned.append('timer'),
            timedelta=timedelta, EVENT_HOMEASSISTANT_STOP='stop')
        await self.adapter.start()
        listener['removed'] = True
        listener['callback']()
        self.assertEqual(cleaned, ['mqtt', 'mqtt', 'timer'])
        self.assertEqual(self.adapter.unsub, [])


if __name__ == '__main__': unittest.main()
