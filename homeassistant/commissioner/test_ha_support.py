import unittest
import ha_support


class SupportTests(unittest.TestCase):
    def test_existing_mqtt_is_preserved(self):
        calls = []
        def api(method, path, body=None):
            calls.append(method)
            return [{'domain': 'mqtt', 'state': 'loaded'}]
        self.assertFalse(ha_support.mqtt_setup(api)['changed'])
        self.assertEqual(calls, ['GET'])

    def test_only_official_addon_flow_completed(self):
        calls = []
        responses = [[], {'type': 'menu', 'menu_options': ['addon', 'broker'], 'flow_id': 'test-flow'},
                     {'type': 'create_entry'}]
        def api(method, path, body=None):
            calls.append((method, path, body))
            return responses.pop(0)
        self.assertTrue(ha_support.mqtt_setup(api)['changed'])
        self.assertEqual(calls[-1][2], {'next_step_id': 'addon'})

    def test_unexpected_flow_is_not_submitted(self):
        responses = [[], {'type': 'form', 'step_id': 'broker', 'flow_id': 'test-flow'}]
        self.assertFalse(ha_support.mqtt_setup(lambda *args: responses.pop(0))['changed'])

    def test_private_attributes_and_states_never_returned(self):
        responses = [[{'domain': 'mqtt', 'state': 'loaded', 'secret': 'fake-secret'}],
                     [{'entity_id': 'device_tracker.example_live_location', 'state': 'private-zone',
                       'attributes': {'latitude': 12.345, 'longitude': 67.89}, 'last_updated': 'time'},
                      {'entity_id': 'person.somebody', 'state': 'private-zone'},
                      {'entity_id': 'binary_sensor.example_live_connected', 'state': 'on'}]]
        result = ha_support.ha_status(lambda *args: responses.pop(0))
        self.assertEqual(len(result['live_entities']), 2)
        for private in ('private-zone', 'latitude', 'somebody', 'fake-secret'):
            self.assertNotIn(private, str(result))


if __name__ == '__main__':
    unittest.main()
