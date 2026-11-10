import hashlib
import os
import pathlib
import re
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

EXPECTED = 'dcfccfb9098fd52ee7e0185bd27c613e4ffbda12c71157e47dd2fd0cb58361ec'
ROOT = pathlib.Path(os.environ['RUNNER_TEMP'])
ORIGIN_FILE = pathlib.Path(os.environ['PREVIEW_ORIGIN_FILE'])
PAGE = b'''<!doctype html><meta charset="utf-8"><title>APK install</title><input id="file" type="file" accept=".apk"><button id="send">Install</button><pre id="status"></pre><script>
const b=document.querySelector('#send');b.onclick=async()=>{b.disabled=true;const f=document.querySelector('#file').files[0];if(!f){document.querySelector('#status').textContent='Choose file';b.disabled=false;return;}try{const r=await fetch('/apk',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f});document.querySelector('#status').textContent=await r.text();}catch(e){document.querySelector('#status').textContent='Response failed; do not retry';}};
</script>'''

def stage(name):
    print('UPLOAD_STAGE=' + name, flush=True)
    (ROOT / ('upload-' + name.lower().replace('_', '-'))).touch(exist_ok=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def reply(self, code, body, content_type='text/plain; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
    def do_GET(self):
        if self.path != '/apk':
            return self.reply(404, b'Not found')
        self.reply(200, PAGE, 'text/html; charset=utf-8')
    def do_POST(self):
        if self.path != '/apk':
            return self.reply(404, b'Not found')
        if getattr(self.server, 'used', False):
            return self.reply(409, b'Already attempted')
        self.server.used = True
        name = None
        install_started = False
        install_ok = False
        response = (400, b'Upload or install failed; do not retry')
        try:
            origin = ORIGIN_FILE.read_text(encoding='utf-8').strip()
            if not origin.startswith('https://') or self.headers.get('Origin') != origin:
                raise ValueError('origin')
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('transfer')
            raw_length = self.headers.get('Content-Length', '')
            if not re.fullmatch(r'[0-9]{1,8}', raw_length):
                raise ValueError('length')
            length = int(raw_length)
            if not 0 < length <= 20 * 1024 * 1024:
                raise ValueError('size')
            self.connection.settimeout(45)
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError('incomplete')
            stage('RECEIVED')
            if hashlib.sha256(data).hexdigest() != EXPECTED:
                raise ValueError('hash')
            stage('HASH_OK')
            fd, name = tempfile.mkstemp(prefix='preview-', suffix='.apk', dir=ROOT)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            if hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest() != EXPECTED:
                raise ValueError('stored hash')
            stage('STORED_VERIFIED')
            stage('INSTALL_STARTED')
            install_started = True
            result = subprocess.run(['adb', '-s', 'emulator-5554', 'install', name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
            if result.returncode != 0 or b'Success' not in result.stdout.splitlines():
                stage('INSTALL_NONZERO_OR_NO_SUCCESS_UNKNOWN')
                stage('FAILED')
                response = (500, b'Install result unknown; do not retry')
            else:
                install_ok = True
                stage('INSTALL_OK')
                response = (200, b'APK installed; SHA-256 verified')
        except subprocess.TimeoutExpired:
            stage('INSTALL_TIMEOUT_UNKNOWN')
            stage('FAILED')
            response = (504, b'Install result unknown; do not retry')
        except Exception as error:
            print('UPLOAD_ERROR_TYPE=' + type(error).__name__, flush=True)
            if isinstance(error, ValueError):
                print('UPLOAD_INPUT_ERROR=' + str(error), flush=True)
            if install_ok:
                stage('POST_INSTALL_ERROR')
                response = (500, b'Install succeeded; later processing failed; do not retry')
            elif install_started:
                stage('INSTALL_UNKNOWN')
                response = (500, b'Install result unknown; do not retry')
            else:
                stage('INPUT_OR_STORAGE_FAILED')
            stage('FAILED')
        finally:
            if name:
                try:
                    os.unlink(name)
                    stage('TEMP_REMOVED')
                except FileNotFoundError:
                    pass
                except OSError:
                    stage('TEMP_REMOVE_FAILED')
                    stage('FAILED')
                    response = (500, b'Temporary file cleanup failed; do not retry')
        try:
            self.reply(*response)
        except (BrokenPipeError, ConnectionResetError, OSError):
            stage('RESPONSE_WRITE_FAILED_DELIVERY_UNKNOWN')
        else:
            stage('RESPONSE_WRITE_OK')

HTTPServer(('127.0.0.1', 8081), Handler).serve_forever()
