"""Read-only location archive downloads behind Home Assistant Ingress."""
from __future__ import annotations
import csv
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

ROOT = Path('/share/tesla-fleet-location-history')
NAME = re.compile(r'^\d{4}-\d{2}-\d{2}_[a-z0-9][a-z0-9_]{0,47}\.ndjson$')
MAX_LINE = 256 * 1024


def archive_files(root=ROOT):
    return sorted((p for p in root.glob('*.ndjson')
                   if NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()), reverse=True)


def csv_chunks(path):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['vehicle', 'source_time_utc', 'received_at_utc', 'latitude', 'longitude', 'is_resend', 'valid_location'])
    yield buffer.getvalue().encode(); buffer.seek(0); buffer.truncate(0)
    with path.open('rb') as stream:
        while line := stream.readline(MAX_LINE + 1):
            if len(line)>MAX_LINE or not line.endswith(b'\n'):
                # Raw download preserves interrupted/oversized rows for recovery.
                while line and not line.endswith(b'\n'):
                    line=stream.readline(MAX_LINE + 1)
                continue
            try:
                row=json.loads(line)
                location=(row.get('location') or {}).get('location_value') or {}
                lat,lon=location.get('latitude'),location.get('longitude')
                valid=(type(lat) in (int,float) and type(lon) in (int,float)
                       and math.isfinite(lat) and math.isfinite(lon) and -90<=lat<=90 and -180<=lon<=180)
                # Only fixed-shape, receiver-generated values enter a spreadsheet.
                vehicle=row['vehicle']
                if not re.fullmatch(r'[a-z0-9][a-z0-9_]{0,47}',vehicle): continue
                source=row.get('source_time') or ''
                received=row['received_at']
                if any(value and not re.fullmatch(r'[0-9TZ:+.\-]+',value) for value in (source,received)): continue
                writer.writerow([vehicle,source,received,lat if valid else '',lon if valid else '',bool(row.get('is_resend')),valid])
                yield buffer.getvalue().encode(); buffer.seek(0); buffer.truncate(0)
            except (ValueError,TypeError,KeyError,AttributeError,RecursionError):
                continue


def page(root=ROOT):
    files=archive_files(root)
    rows=''.join(f'<tr><td>{html.escape(p.stem)}</td><td>{p.stat().st_size:,} bytes</td>'
                 f'<td><a href="download?file={p.name}&amp;format=csv">CSV</a> · '
                 f'<a href="download?file={p.name}&amp;format=ndjson">Original data</a></td></tr>' for p in files)
    return ('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tesla location history</title><style>body{font:16px system-ui,sans-serif;color:#dde6ee;background:#111923;margin:0;padding:24px}main{max-width:920px;margin:auto}h1{font-size:26px}p{line-height:1.55;color:#b9c7d4}a{color:#76caff}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:14px 8px;border-bottom:1px solid #324150}small{color:#a7b6c5}.scroll{overflow:auto}</style></head><body><main>
<h1>Tesla location history</h1><p>Location samples are saved by vehicle and UTC date. Choose CSV for a spreadsheet or Original data for every saved field.</p>
<p>History is retained until you choose to delete it. Delayed samples and resends are included; repeated deliveries may appear more than once. These downloads contain precise locations.</p>'''
            + ('<div class="scroll"><table><thead><tr><th>Day / vehicle</th><th>Size</th><th>Download</th></tr></thead><tbody>'+rows+'</tbody></table></div>' if files else '<p>No location samples have been archived yet. Data will appear after the vehicle sends its next location.</p>')
            + '<p><small>CSV leaves invalid coordinates empty and skips incomplete rows. Original data preserves those rows for recovery. Reload this page to see new files.</small></p></main></body></html>').encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # No request paths, IP addresses, or downloaded values in logs.

    def send_headers(self, status, content_type, name=None, size=None):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'; base-uri 'none'")
        if name: self.send_header('Content-Disposition',f'attachment; filename="{name}"')
        if size is not None: self.send_header('Content-Length',str(size))
        self.end_headers()

    def do_GET(self):
        if self.client_address[0] != '172.30.32.2':
            self.send_headers(403,'text/plain',size=0); return
        try:
            request=urlsplit(self.path)
            if request.path=='/':
                content=page(); self.send_headers(200,'text/html; charset=utf-8',size=len(content)); self.wfile.write(content); return
            query=parse_qs(request.query)
            name=query.get('file',[''])[0]; format=query.get('format',[''])[0]
            path=ROOT/name
            if request.path!='/download' or not NAME.fullmatch(name) or format not in ('csv','ndjson') or path.is_symlink() or not path.is_file():
                self.send_headers(404,'text/plain',size=0); return
            if format=='csv':
                self.send_headers(200,'text/csv; charset=utf-8',path.stem+'.csv')
                for chunk in csv_chunks(path): self.wfile.write(chunk)
            else:
                with path.open('rb') as stream:
                    remaining=os.fstat(stream.fileno()).st_size
                    self.send_headers(200,'application/x-ndjson',name,remaining)
                    while remaining:
                        chunk=stream.read(min(remaining,65536))
                        if not chunk: break
                        self.wfile.write(chunk); remaining-=len(chunk)
        except (OSError,ValueError):
            self.close_connection=True


if __name__=='__main__':
    os.umask(0o077)
    ThreadingHTTPServer(('0.0.0.0',8099),Handler).serve_forever()
