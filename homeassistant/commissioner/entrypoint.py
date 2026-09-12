"""Dispatch only the explicit setup mode; never dump exception details."""
import json
import os
from pathlib import Path

import commissioner
import ha_support


if __name__ == '__main__':
    os.umask(0o077)
    try:
        mode = json.loads(Path('/data/options.json').read_text()).get('mode', 'inspect')
        if mode in ('mqtt_setup', 'ha_status'):
            result = getattr(ha_support, mode)()
            print(json.dumps(result, sort_keys=True), flush=True)
            raise SystemExit(0)
        raise SystemExit(commissioner.main())
    except Exception:
        print('{"status":"stopped","category":"setup_helper_error"}', flush=True)
        raise SystemExit(1)
