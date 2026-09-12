"""Narrow supported HA operations used during streaming commissioning."""
import json
import os
import re
import urllib.error
import urllib.request


def request(method, path, body=None):
    if not path.startswith('/api/') or '..' in path:
        raise ValueError('Invalid API path')
    token = os.environ['SUPERVISOR_TOKEN']
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request('http://supervisor/core' + path, data=payload,
                                 method=method, headers={
                                     'Authorization': 'Bearer ' + token,
                                     'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def mqtt_setup(api=request):
    existing = [e for e in api('GET', '/api/config/config_entries/entry')
                if e.get('domain') == 'mqtt']
    if existing:
        return {'mqtt_present': True, 'mqtt_loaded': any(e.get('state') == 'loaded' for e in existing),
                'changed': False}
    flow = api('POST', '/api/config/config_entries/flow', {'handler': 'mqtt'})
    if flow.get('type') != 'menu' or 'addon' not in flow.get('menu_options', []):
        return {'mqtt_present': False, 'changed': False, 'status': 'unsupported_mqtt_flow'}
    flow_id = flow.get('flow_id', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', flow_id):
        raise ValueError('Invalid flow identifier')
    result = api('POST', '/api/config/config_entries/flow/' + flow_id, {'next_step_id': 'addon'})
    return {'mqtt_present': result.get('type') == 'create_entry',
            'changed': result.get('type') == 'create_entry',
            'status': 'completed' if result.get('type') == 'create_entry' else 'mqtt_setup_incomplete'}


def ha_status(api=request):
    entries = api('GET', '/api/config/config_entries/entry')
    integration_status = {domain: [e.get('state') for e in entries if e.get('domain') == domain]
                          for domain in ('mqtt', 'tesla_fleet', 'cloudflare')}
    states = api('GET', '/api/states')
    # Output only our entities, availability and sample timestamps. Never location,
    # speed, person names, VINs, or arbitrary device attributes from the full API.
    names = re.compile(r'^(?:device_tracker|sensor|binary_sensor)\.[a-z0-9_]+_live_'
                       r'(?:location|speed|last_update|telemetry_fresh|connected|battery|usable_battery|gear)$')
    result = []
    for value in states:
        entity = value.get('entity_id', '')
        if not names.fullmatch(entity):
            continue
        state = value.get('state')
        item = {'entity_id': entity, 'available': state not in ('unavailable', 'unknown', None),
                'last_updated': value.get('last_updated')}
        if entity.startswith('binary_sensor.') and state in ('on', 'off', 'unavailable', 'unknown'):
            item['state'] = state
        result.append(item)
    return {'integrations': integration_status, 'live_entities': sorted(result, key=lambda v: v['entity_id'])}
