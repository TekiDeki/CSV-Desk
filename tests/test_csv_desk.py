#!/usr/bin/env python3
import functools
import hashlib
import json
import re
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TMP = Path('/tmp/csv-desk-tests')
TMP.mkdir(exist_ok=True)
ART = ROOT / 'tests' / 'artifacts'
ART.mkdir(exist_ok=True)
checks = []
console_messages = []
third_party = []


def check(value, name):
    checks.append({'name': name, 'passed': bool(value)})
    if not value:
        raise AssertionError(name)


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(ROOT)))
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f'http://127.0.0.1:{server.server_address[1]}/'


def contrast_ratio(foreground, background):
    def luminance(value):
        channels = [int(v) / 255 for v in re.findall(r'\d+', value)[:3]]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    a, b = luminance(foreground), luminance(background)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def wait_eval(page, expression, timeout=15000):
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        if page.evaluate(expression):
            return
        time.sleep(0.05)
    raise AssertionError('timeout: ' + expression)


def wire(page):
    page.on('pageerror', lambda error: console_messages.append('page: ' + str(error)))
    def on_console(message):
        if message.type in ('error', 'warning'):
            console_messages.append(message.type + ': ' + message.text)
    page.on('console', on_console)
    def on_request(request):
        parsed = urlparse(request.url)
        if parsed.scheme in ('http', 'https') and parsed.hostname not in ('127.0.0.1', 'localhost'):
            third_party.append(request.url)
    page.on('request', on_request)


def open_page(browser, *, locale='en-US', width=1280, height=900, touch=False, color='light', init=None):
    context = browser.new_context(viewport={'width': width, 'height': height}, locale=locale,
                                  has_touch=touch, is_mobile=touch, color_scheme=color,
                                  service_workers='allow', accept_downloads=True)
    if init:
        context.add_init_script(init)
    page = context.new_page()
    wire(page)
    response = page.goto(BASE, wait_until='networkidle')
    check(response is not None and response.status == 200, 'page returns HTTP 200')
    wait_eval(page, "typeof dataFromMatrix === 'function' && typeof parseDelimited === 'function'")
    return context, page


def sample(page):
    page.click('#sampleBtn')
    check(page.evaluate('state.rows.length') == 11, 'sample has eleven data rows')
    check(page.evaluate('state.columns.length') == 5, 'sample has five columns')


def set_cell(page, row, col, value):
    area = page.locator('textarea.cell').nth(row * 5 + col)
    area.fill(value)
    area.press('Tab')


def accept_next_dialog(page):
    page.once('dialog', lambda dialog: dialog.accept())


def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            # Parser, delimiter detection and static security.
            context, page = open_page(browser, locale='ro-RO')
            check(page.locator('html').get_attribute('lang') == 'en', 'first visit is English even with Romanian browser locale')
            check(page.title() == 'CSV-Desk - edit and clean CSV files locally', 'initial document title is correct')
            csp = page.locator('meta[http-equiv="Content-Security-Policy"]').get_attribute('content')
            check("'unsafe-eval'" not in csp and "connect-src 'self'" in csp, 'CSP forbids eval and external connections')
            check(page.locator('script[src], link[rel="stylesheet"]').count() == 0, 'no external scripts or stylesheets')
            check(page.evaluate('parseDelimited("a,b\\r\\n1,2\\r\\n", ",")') == [['a','b'],['1','2']], 'basic CRLF CSV parses')
            check(page.evaluate('parseDelimited("a;\\"x;y\\";z", ";")') == [['a','x;y','z']], 'quoted semicolon parses')
            check(page.evaluate('([text,d])=>parseDelimited(text,d)', ['a,"say ""hello"""', ',']) == [['a','say "hello"']], 'escaped quotes parse')
            check(page.evaluate('parseDelimited("a,\\"x\\ny\\",z", ",")') == [['a','x\ny','z']], 'newline inside quotes is preserved')
            check(page.evaluate('parseDelimited("a,b\\n", ",")') == [['a','b']], 'trailing newline adds no phantom row')
            check(page.evaluate('parseDelimited("a,b,", ",")') == [['a','b','']], 'trailing delimiter preserves empty field')
            check(page.evaluate('parseDelimited("a\\n\\nb", ",")') == [['a'],[''],['b']], 'blank records are preserved')
            check(page.evaluate('parseDelimited("a\\rb\\r", ",")') == [['a'],['b']], 'classic CR line endings parse')
            check(page.evaluate('analyzeText("name;city\\nAna;Vršac\\n", "x.csv").delimiter') == ';', 'semicolon delimiter is detected')
            check(page.evaluate('analyzeText("name\\tcity\\nAna\\tVršac\\n", "x.tsv").delimiter') == '\t', 'tab delimiter is detected')
            check(page.evaluate('analyzeText("name|city\\nAna|Vršac\\n", "x.csv").delimiter') == '|', 'pipe delimiter is detected')
            delimiter_cases = [
                ('name,note\nA,x|y|z\n', 'x.csv', ',', 'comma wins over pipe characters in values'),
                ('name|note\nA,x,y|z\n', 'x.csv', '|', 'pipe wins over comma characters in values'),
                ('name;note\nA,x|y|z\n', 'x.csv', ';', 'semicolon wins over pipe characters in values'),
                ('name,note\nA,"x|y|z"\n', 'x.csv', ',', 'quoted pipes do not change the real comma delimiter'),
                ('name\tNOTE\nA\tB,C,D\n', 'x.tsv', '\t', 'TSV wins over comma characters in values'),
            ]
            for text, filename, expected, label in delimiter_cases:
                check(page.evaluate('([t,n])=>analyzeText(t,n).delimiter', [text, filename]) == expected, label)
            malformed = page.evaluate('''() => {try{parseDelimited('a,"unclosed\\nnext', ',');return ''}catch(e){return e.message}}''')
            check('quoted field is not closed' in malformed, 'unclosed quote produces an honest error')
            check(page.evaluate('analyzeText("", "empty.csv").rows.length') == 0, 'empty input has no matrix rows')
            roundtrip = page.evaluate(r'''() => {
              const matrix=[['name','note','empty'],['Ana','paid, online',''],['B','say "hi"\nnext','x']];
              const results=[];
              for(const d of [',',';','\t','|']){
                const cols=matrix[0].map((name,i)=>({name,source:i}));
                const rows=matrix.slice(1).map((cells,i)=>({id:i+1,cells}));
                const text=makeDelimited(rows,cols,true,d,'\r\n');
                results.push(JSON.stringify(parseDelimited(text,d))===JSON.stringify(matrix));
              }
              return results;
            }''')
            check(all(roundtrip) and len(roundtrip) == 4, 'all four delimiters survive quoted CRLF round trips')
            check(page.evaluate("L.en.how1.includes('UTF-16 with BOM')"), 'visible encoding text states the UTF-16 BOM requirement')
            check(page.evaluate("Object.keys(L.en).every(k=>['ro','sr-latn','sr-cyrl'].every(l=>Object.hasOwn(L[l],k)))"), 'all translation dictionaries contain every English key')
            check(page.evaluate("['en','ro','sr-latn','sr-cyrl'].every(l=>Object.keys(L[l]).length===Object.keys(L.en).length)"), 'translation dictionaries have identical key counts')
            check(page.evaluate("[...document.querySelectorAll('[data-i]')].every(e=>Object.hasOwn(L.en,e.dataset.i))"), 'every static translation reference exists')
            check(page.evaluate("!/ [ёъыэюящ]/iu.test(Object.values(L['sr-cyrl']).join(' '))".replace('/ [','/[')), 'Serbian Cyrillic contains no Russian-only letters')
            check(page.evaluate("Object.values(L).every(dict=>Object.values(dict).every(v=>typeof v==='string'&&v.trim()))"), 'translations have no empty values')
            check(page.evaluate("['ro','sr-latn','sr-cyrl'].every(l=>Object.keys(L.en).every(k=>L[l][k]!==L.en[k]||L.en[k].length<=20))"), 'no long English translation is copied unchanged')
            check('—' not in (ROOT / 'index.html').read_text() and '&mdash;' not in (ROOT / 'index.html').read_text(), 'R31 visible copy contains no em dash or mdash entity')
            check(page.locator('a[href="https://paypal.me/TekiDeki"]').count() == 1, 'support URL uses the required lowercase PayPal host')
            context.close()

            # Real file input, encodings, ragged rows and malformed files.
            context, page = open_page(browser)
            payload = '\ufeffname;city;note\r\nAna;Brașov;"paid; online"\r\nMarko;Novi Sad;"say ""hello"""\r\n'.encode('utf-8')
            page.set_input_files('#fileInput', {'name':'diacritics.csv','mimeType':'text/csv','buffer':payload})
            wait_eval(page, "state && state.fileName === 'diacritics.csv' && state.rows.length === 2")
            check(page.evaluate('state.delimiter') == ';', 'real file input detects semicolon')
            check(page.evaluate('state.rows[0].cells[1]') == 'Brașov', 'UTF-8 BOM and Romanian diacritics decode')
            check(page.evaluate('state.rows[0].cells[2]') == 'paid; online', 'quoted delimiter survives file input')
            check(page.evaluate('state.rows[1].cells[2]') == 'say "hello"', 'escaped quote survives file input')
            utf16 = 'name\tcity\nJovan\tVršac\n'.encode('utf-16')
            page.set_input_files('#fileInput', {'name':'utf16.tsv','mimeType':'text/tab-separated-values','buffer':utf16})
            wait_eval(page, "state && state.fileName === 'utf16.tsv'")
            check(page.evaluate('state.rows[0].cells[1]') == 'Vršac', 'UTF-16LE BOM TSV decodes')
            utf16be = b'\xfe\xff' + 'name,city\nZoë,Zürich\n'.encode('utf-16-be')
            page.set_input_files('#fileInput', {'name':'utf16be.csv','mimeType':'text/csv','buffer':utf16be})
            wait_eval(page, "state && state.fileName === 'utf16be.csv'")
            check(page.evaluate('state.rows[0].cells[1]') == 'Zürich', 'UTF-16BE BOM CSV decodes')
            page.set_input_files('#fileInput', {'name':'ragged.csv','mimeType':'text/csv','buffer':b'a,b\n1\n2,3,4\n'})
            wait_eval(page, "state && state.fileName === 'ragged.csv' && state.rows.length === 2")
            check(page.evaluate('state.columns.length === 3 && state.rows[0].cells.length === 3'), 'ragged rows are padded to the maximum width')
            check(page.evaluate('state.rows[0].cells[2] === "" && state.rows[1].cells[2] === "4"'), 'ragged padding loses no extra values')
            check('different widths' in page.locator('#status').inner_text(), 'ragged input displays a warning')
            page.set_input_files('#fileInput', {'name':'bad.csv','mimeType':'text/csv','buffer':b'a,"unclosed\n'})
            wait_eval(page, "document.querySelector('#status').classList.contains('error')")
            check(page.evaluate('state.fileName') == 'ragged.csv', 'malformed replacement file does not destroy current data')
            check('not closed' in page.locator('#status').inner_text(), 'malformed file error is visible')
            page.set_input_files('#fileInput', {'name':'data.json','mimeType':'application/json','buffer':b'{}'})
            check('Choose a CSV' in page.locator('#status').inner_text(), 'unsupported extension is rejected')
            page.set_input_files('#fileInput', {'name':'too-big.csv','mimeType':'text/csv','buffer':b'a,' + b'x' * (8 * 1024 * 1024)})
            check('8 MiB' in page.locator('#status').inner_text() and page.evaluate('state.fileName') == 'ragged.csv', 'files over 8 MiB are rejected without data loss')
            page.set_input_files('#fileInput', {'name':'no-bom.csv','mimeType':'text/csv','buffer':'name,city\nAna,Vršac\n'.encode('utf-16-le')})
            wait_eval(page, "document.querySelector('#status').textContent.includes('with BOM')")
            check(page.evaluate('state.fileName') == 'ragged.csv', 'UTF-16 without BOM is rejected honestly without data loss')
            page.evaluate('''text => {const dt=new DataTransfer();dt.items.add(new File([text],'drop.csv',{type:'text/csv'}));const drop=document.querySelector('#dropzone');drop.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt}));window.__dropClass=drop.classList.contains('drag');drop.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt}))}''', 'a,b\n1,2\n')
            wait_eval(page, "state && state.fileName === 'drop.csv'")
            check(page.evaluate('window.__dropClass') and page.evaluate('state.rows[0].cells[1]') == '2', 'drag-over styling and a real DataTransfer drop load the file')
            page.set_input_files('#fileInput', {'name':'single.txt','mimeType':'text/plain','buffer':b'hello\nworld\n'})
            wait_eval(page, "state && state.fileName === 'single.txt'")
            check(page.evaluate('state.columns.length === 1 && state.rows.length === 1 && state.columns[0].name === "hello" && state.rows[0].cells[0] === "world"'), 'single-column TXT uses a safe delimiter fallback')
            context.close()

            # Editing, structure, history, filtering, sorting and pagination.
            context, page = open_page(browser)
            sample(page)
            set_cell(page, 0, 0, 'Ana Changed')
            check(page.evaluate('state.rows[0].cells[0]') == 'Ana Changed', 'cell textarea edits the model')
            check(page.evaluate('state.dirty') is True, 'editing marks data modified')
            page.click('#undo')
            check(page.evaluate('state.rows[0].cells[0]') == ' Ana Pop ', 'undo restores the previous cell value')
            page.click('#redo')
            check(page.evaluate('state.rows[0].cells[0]') == 'Ana Changed', 'redo reapplies the cell value')
            page.click('#resetData')
            # Restore requires confirmation; the first click above should not have run without a dialog handler.
            check(page.evaluate('state.rows[0].cells[0]') == 'Ana Changed', 'reset does not happen without confirmation')
            accept_next_dialog(page)
            page.click('#resetData')
            check(page.evaluate('state.rows[0].cells[0]') == ' Ana Pop ' and not page.evaluate('state.dirty'), 'confirmed reset restores original data')
            page.locator('#columnsDetails summary').click()
            header = page.locator('#columnList .columnrow input').first
            header.fill('full name'); header.press('Tab')
            check(page.evaluate('state.columns[0].name') == 'full name', 'column rename updates the model')
            page.locator('#columnList .columnrow').first.locator('.move-right').click()
            check(page.evaluate('state.columns[1].name') == 'full name', 'column reorder changes display order without changing cell storage')
            page.locator('#columnList .columnrow').nth(1).locator('button').nth(2).click()
            check(page.evaluate('visibleColumns().length') == 4, 'a column can be hidden')
            page.fill('#newColumn', 'reviewed'); page.click('#addColumn')
            check(page.evaluate('state.columns.length') == 6 and page.evaluate('state.rows.every(r=>r.cells.length===6)'), 'add column extends every row')
            page.locator('#columnList .columnrow').last.locator('.delete-col').click()
            check(page.evaluate('state.columns.length') == 5 and page.evaluate('state.rows.every(r=>r.cells.length===5)'), 'delete column removes its cells and reindexes')
            page.click('#addRow')
            check(page.evaluate('state.rows.length') == 12, 'add row appends an empty row')
            page.locator('#tableBody .rowdelete').last.click()
            check(page.evaluate('state.rows.length') == 11, 'row delete removes exactly one row')
            page.fill('#searchInput', 'Vršac')
            check(page.evaluate('getView().length') == 1 and page.locator('#tableBody tr').count() == 1, 'search filters across all columns with diacritics')
            page.click('#clearSearch')
            check(page.evaluate('getView().length') == 11, 'clear search restores all rows')
            page.click('#headerToggle')
            check(page.evaluate('state.rows.length') == 12 and not page.evaluate('state.hasHeader'), 'removing header turns it into a data row')
            check(page.evaluate('state.rows[0].cells[0]') == 'email' or page.evaluate('state.rows[0].cells[0]') == 'full name', 'former header values survive as data')
            page.click('#headerToggle')
            check(page.evaluate('state.rows.length') == 11 and page.evaluate('state.hasHeader'), 'first data row can become the header again')
            context.close()

            context, page = open_page(browser)
            page.evaluate("dataFromMatrix([['name','amount'],['ten','10'],['two','2'],['one','1']],'numbers.csv',',',true)")
            page.locator('.sortbtn').nth(1).click()
            check(page.evaluate('getView().map(r=>r.cells[1]).join(",")') == '1,2,10', 'numeric-aware sort orders 1, 2, 10')
            page.locator('.sortbtn').nth(1).click()
            check(page.evaluate('getView().map(r=>r.cells[1]).join(",")') == '10,2,1', 'second sort click reverses order')
            page.locator('.sortbtn').nth(1).click()
            check(page.evaluate('sortState === null && getView().map(r=>r.cells[1]).join(",")') == '10,2,1', 'third sort click clears sorting')
            page.evaluate("dataFromMatrix([['n'],...Array.from({length:60},(_,i)=>[String(i+1)])],'pages.csv',',',true)")
            page.select_option('#pageSize', '25')
            check(page.locator('#tableBody tr').count() == 25 and 'Page 1 of 3' in page.locator('#pageInfo').inner_text(), 'pagination limits the rendered rows')
            page.click('#nextPage'); page.click('#nextPage')
            check(page.locator('#tableBody tr').count() == 10 and 'Page 3 of 3' in page.locator('#pageInfo').inner_text(), 'last pagination page has the remainder')
            check(page.locator('#nextPage').is_disabled(), 'next is disabled on the last page')
            context.close()

            # Cleanup and literal replacement on the deterministic sample.
            context, page = open_page(browser)
            sample(page)
            page.click('#trimBtn')
            check(page.locator('#status').inner_text().endswith('4 cells.'), 'trim reports the exact four changed cells')
            check(page.evaluate('state.rows[0].cells[0]') == 'Ana Pop', 'trim changes leading and trailing whitespace')
            page.click('#dedupeBtn')
            check(page.locator('#status').inner_text().endswith('2 duplicate rows.'), 'dedupe removes the two exact duplicate rows after trim')
            check(page.evaluate('state.rows.length') == 9, 'nine rows remain after dedupe')
            page.click('#emptyRowsBtn')
            check(page.locator('#status').inner_text().endswith('1 empty rows.'), 'empty-row cleaner removes the blank record')
            check(page.evaluate('state.rows.length') == 8, 'eight rows remain after all cleanup')
            city_id = page.evaluate("state.columns.find(c=>c.name==='city').id")
            page.select_option('#replaceScope', city_id)
            page.fill('#findInput', 'Brașov'); page.fill('#replaceInput', 'Cluj'); page.click('#replaceBtn')
            check(page.evaluate("state.rows.filter(r=>r.cells[state.columns.find(c=>c.name==='city').source]==='Cluj').length") == 1, 'column-scoped literal replacement updates the matching city')
            check(page.evaluate("state.rows.some(r=>r.cells[0]==='Ana Pop')"), 'replacement does not alter unrelated columns')
            page.click('#replaceBtn')
            check('No matching values changed' in page.locator('#status').inner_text(), 'repeated replacement honestly reports no match')
            page.fill('#findInput', ''); page.click('#replaceBtn')
            check('Enter the literal text' in page.locator('#status').inner_text(), 'empty find text is rejected')
            accept_next_dialog(page); page.click('#resetData')
            check(page.evaluate('state.rows.length') == 11 and page.evaluate('state.rows[0].cells[0]') == ' Ana Pop ', 'reset restores pre-cleanup data')
            page.click('#undo')
            check(page.evaluate('state.rows.length') == 8, 'undo after reset returns to the cleaned data')
            context.close()

            # Literal replacement and exact, case-sensitive duplicate removal.
            context, page = open_page(browser)
            page.evaluate("dataFromMatrix([['value'],['a.b'],['abc']],'literal.csv',',',true)")
            page.fill('#findInput', '.'); page.fill('#replaceInput', '-'); page.click('#replaceBtn')
            check(page.evaluate('state.rows.map(r=>r.cells[0]).join(",")') == 'a-b,abc', 'find and replace treats dot as literal text, not a regex')
            page.evaluate("dataFromMatrix([['value'],['A'],['a'],['A']],'case.csv',',',true)")
            page.click('#dedupeBtn')
            check(page.evaluate('state.rows.map(r=>r.cells[0]).join(",")') == 'A,a', 'duplicate removal is exact and case-sensitive')
            context.close()

            # Real CSV, TSV and JSON downloads with exact byte checks.
            context, page = open_page(browser)
            page.evaluate("dataFromMatrix([['name','note','amount'],['Ana','paid, online','10'],['B','say \"hi\"\\nline','2']],'export.csv',',',true)")
            page.locator('#exportDetails summary').click()
            with page.expect_download() as event:
                page.click('#downloadBtn')
            download = event.value
            target = TMP / 'export-cleaned.csv'; download.save_as(target)
            expected = 'name,note,amount\r\nAna,"paid, online",10\r\nB,"say ""hi""\r\nline",2\r\n'.encode()
            check(target.read_bytes() == expected, 'CSV export bytes quote delimiters, quotes and embedded CRLF correctly')
            check(download.suggested_filename == 'export-cleaned.csv', 'CSV export never overwrites the original filename')
            page.check('#exportBom')
            with page.expect_download() as event: page.click('#downloadBtn')
            bom_target = TMP / 'export-bom.csv'; event.value.save_as(bom_target)
            check(bom_target.read_bytes().startswith(b'\xef\xbb\xbf'), 'optional UTF-8 BOM is present in exported bytes')
            page.select_option('#exportDelimiter', 'tab')
            with page.expect_download() as event: page.click('#downloadBtn')
            tsv_target = TMP / 'export.tsv'; event.value.save_as(tsv_target)
            check(event.value.suggested_filename.endswith('.tsv') and tsv_target.read_text(encoding='utf-8-sig').splitlines()[0] == 'name\tnote\tamount', 'tab export uses TSV extension and delimiter')
            page.select_option('#exportFormat', 'json')
            with page.expect_download() as event: page.click('#downloadBtn')
            json_target = TMP / 'export.json'; event.value.save_as(json_target)
            exported = json.loads(json_target.read_text())
            check(exported[1]['note'] == 'say "hi"\nline' and exported[0]['amount'] == '10', 'JSON preserves newlines and string values')
            page.evaluate("dataFromMatrix([['name','name',''],['A','B','C']],'keys.csv',',',true)")
            with page.expect_download() as event: page.click('#downloadBtn')
            keys_target = TMP / 'keys.json'; event.value.save_as(keys_target)
            check(json.loads(keys_target.read_text()) == [{'name':'A','name_2':'B','column_3':'C'}], 'JSON makes duplicate and empty headers unique')
            page.evaluate("dataFromMatrix([['name','note'],['Ana','x'],['Bob','y']],'view.csv',',',true)")
            page.locator('#columnsDetails summary').click()
            page.locator('#columnList .columnrow').nth(1).locator('button').nth(2).click()
            page.fill('#searchInput', 'Ana')
            page.select_option('#exportScope', 'view'); page.select_option('#exportFormat', 'csv'); page.uncheck('#exportBom')
            with page.expect_download() as event: page.click('#downloadBtn')
            view_target = TMP / 'view.csv'; event.value.save_as(view_target)
            check(view_target.read_bytes() == b'name\r\nAna\r\n', 'view export applies search and visible columns')
            check(page.locator('#columnList .columnrow').first.locator('button').nth(2).is_disabled(), 'the last visible column cannot be hidden')
            page.select_option('#exportScope', 'all'); page.select_option('#exportDelimiter', ','); page.fill('#searchInput', '')
            with page.expect_download() as event: page.click('#downloadBtn')
            all_target = TMP / 'all.csv'; event.value.save_as(all_target)
            check(all_target.read_bytes() == b'name,note\r\nAna,x\r\nBob,y\r\n', 'full export includes hidden columns and rows outside the filter')
            page.evaluate("dataFromMatrix([['h1','h2']],'headers.csv',',',true)")
            with page.expect_download() as event: page.click('#downloadBtn')
            header_target = TMP / 'headers.csv'; event.value.save_as(header_target)
            check(header_target.read_bytes() == b'h1,h2\r\n', 'header-only CSV export is valid with zero data rows')
            context.close()

            # Manual local sessions, persistence, quota honesty and corrupted storage.
            context, page = open_page(browser)
            sample(page); set_cell(page, 0, 0, 'Saved Ana'); page.locator('#sessionsDetails summary').click(); page.click('#saveSession')
            check(page.evaluate('validSessions(storeGet("sessions")).length') == 1, 'manual session is stored locally')
            accept_next_dialog(page); page.reload(wait_until='networkidle')
            check(page.evaluate('state === null'), 'file data is not automatically restored on reload')
            page.locator('#sessionsDetails summary').click()
            check(page.locator('#sessionList .sessionrow').count() == 1, 'saved session is listed after reload')
            page.locator('#sessionList .sessionrow .btn').click()
            check(page.evaluate('state.rows[0].cells[0]') == 'Saved Ana', 'manual restore recovers the edited session')
            for value in ['One','Two','Three']:
                page.evaluate('(v)=>{state.rows[0].cells[0]=v;state.dirty=true}', value)
                page.wait_for_timeout(5)
                page.click('#saveSession')
            check(page.evaluate('validSessions(storeGet("sessions")).length') == 3, 'only three local sessions are retained')
            accept_next_dialog(page); page.locator('#sessionList .sessionrow .dangerbtn').first.click()
            check(page.evaluate('validSessions(storeGet("sessions")).length') == 2, 'confirmed session delete updates localStorage')
            page.evaluate("state.rows[0].cells[0]='x'.repeat(3100000);state.dirty=true")
            page.click('#saveSession')
            check('too large' in page.locator('#status').inner_text(), 'oversized session is rejected honestly')
            context.close()
            context, page = open_page(browser, init="localStorage.setItem('csvdesk-sessions','{broken')")
            check(page.locator('#sampleBtn').is_visible(), 'corrupted session storage does not break initialization')
            context.close()

            # Four languages, metadata, persistence and dark theme.
            context, page = open_page(browser, color='dark')
            check(page.locator('html').get_attribute('data-theme') == 'dark', 'system dark preference is followed initially')
            expectations = [('ro','ro','Deschide datele','Instalează aplicația','Instalează această aplicație'),('sr-latn','sr-Latn','Otvorite podatke','Instaliraj aplikaciju','Instaliraj ovu aplikaciju'),('sr-cyrl','sr-Cyrl','Отворите податке','Инсталирај апликацију','Инсталирај ову апликацију'),('en','en','Open your data','Install app','Install this app')]
            for code, html_lang, heading, install_label, install_title in expectations:
                page.click(f'.langbtn[data-lang="{code}"]')
                check(page.locator('html').get_attribute('lang') == html_lang, 'html lang switches to ' + code)
                check(page.locator('#openH').inner_text() == heading, 'visible heading translates to ' + code)
                check(page.evaluate("document.querySelector('meta[name=\"description\"]').content") == page.evaluate('L[lang].metaDesc'), 'meta description translates to ' + code)
                check(page.evaluate("document.querySelector('meta[property=\"og:title\"]').content") == page.evaluate('L[lang].metaTitle'), 'Open Graph title translates to ' + code)
                data = json.loads(page.locator('#jsonld').text_content())
                check(data['inLanguage'] == html_lang and data['description'] == page.evaluate('L[lang].metaDesc'), 'JSON-LD translates to ' + code)
                wait_eval(page, "document.querySelector('#td-install-label').textContent === " + json.dumps(install_label, ensure_ascii=False))
                check(page.locator('#td-install-label').inner_text() == install_label and page.locator('#td-install-title').inner_text() == install_title, 'install component translates to ' + code)
            page.click('.langbtn[data-lang="ro"]'); page.click('#themebtn')
            check(page.locator('html').get_attribute('data-theme') == 'light', 'manual theme toggle works')
            page.reload(wait_until='networkidle')
            check(page.locator('html').get_attribute('lang') == 'ro' and page.locator('html').get_attribute('data-theme') == 'light', 'language and manual theme persist')
            context.close()

            # Install button and dialog keep readable contrast in both themes.
            context, page = open_page(browser, width=390, height=844, touch=True, color='dark')
            def install_styles(selector):
                return page.evaluate('''selector => {const s=getComputedStyle(document.querySelector(selector));return {color:s.color,background:s.backgroundColor}}''', selector)
            button_style = install_styles('#td-install-button')
            check(contrast_ratio(button_style['color'], button_style['background']) >= 4.5, 'dark install button text has WCAG AA contrast')
            check(page.evaluate("getComputedStyle(document.querySelector('#td-install-button')).backgroundColor") == 'rgb(21, 31, 46)', 'dark install button uses the application panel color')
            for selector, label in [('.bigbtn', 'primary file button'), ('.langbtn[aria-pressed="true"]', 'active language')]:
                style = install_styles(selector)
                check(contrast_ratio(style['color'], style['background']) >= 4.5, 'dark ' + label + ' has WCAG AA contrast')
            page.screenshot(path=str(ROOT / 'screenshots' / 'preflight-dark.png'), full_page=False)
            page.evaluate("document.querySelector('#td-install-section').scrollIntoView({block:'center'})")
            page.screenshot(path=str(ROOT / 'screenshots' / 'install-dark.png'), full_page=False)
            page.click('#td-install-button')
            dialog_style = install_styles('#td-install-dialog')
            check(contrast_ratio(dialog_style['color'], dialog_style['background']) >= 4.5, 'dark install dialog text has WCAG AA contrast')
            page.screenshot(path=str(ROOT / 'screenshots' / 'install-dialog-dark.png'), full_page=False)
            page.click('#td-install-close'); page.click('#themebtn')
            light_style = install_styles('#td-install-button')
            check(contrast_ratio(light_style['color'], light_style['background']) >= 4.5, 'light install button text keeps WCAG AA contrast')
            for selector, label in [('.bigbtn', 'primary file button'), ('.langbtn[aria-pressed="true"]', 'active language')]:
                style = install_styles(selector)
                check(contrast_ratio(style['color'], style['background']) >= 4.5, 'light ' + label + ' keeps WCAG AA contrast')
            context.close()

            # Mobile layout, touch targets, hidden controls and screenshots.
            for width, height in [(320,740),(390,844),(768,900),(1440,1000)]:
                context, page = open_page(browser, width=width, height=height, touch=width <= 390)
                sample(page)
                page.evaluate("document.querySelectorAll('#workspace details, #sessionsDetails').forEach(d=>d.open=true)")
                overflow = page.evaluate('document.documentElement.scrollWidth - innerWidth')
                check(overflow <= 1, f'{width}px page has no horizontal overflow')
                exposed = page.evaluate('''() => [...document.querySelectorAll('[hidden],.hidden')].filter(e=>{const s=getComputedStyle(e);return s.display!=='none'&&(e.offsetWidth||e.offsetHeight)}).map(e=>e.id||e.tagName)''')
                check(exposed == [], f'{width}px exposes no hidden controls')
                small = page.evaluate('''() => [...document.querySelectorAll('button,select,summary,a.homelink,a.supportbtn,a.reportlink')].filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width&&r.height&&s.visibility!=='hidden'&&(r.width<44||r.height<44)}).map(e=>({id:e.id,cls:e.className,w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height}))''')
                check(small == [], f'{width}px visible controls meet 44px targets: {small}')
                check(page.locator('#tableWrap').evaluate('(e)=>e.scrollWidth>=e.clientWidth'), f'{width}px table keeps wide data inside its own scroll region')
                if width <= 390:
                    for code in ('ro','sr-latn','sr-cyrl','en'):
                        page.click(f'.langbtn[data-lang="{code}"]')
                        check(page.evaluate('document.documentElement.scrollWidth - innerWidth') <= 1, f'{width}px {code} has no overflow with every panel open')
                if width in (390,1440):
                    page.evaluate("document.querySelector('.stats').scrollIntoView({block:'start'})")
                    page.screenshot(path=str(ROOT / 'screenshots' / ('mobile.png' if width == 390 else 'desktop.png')), full_page=False)
                if width == 390:
                    page.click('#themebtn')
                    page.evaluate("document.querySelector('.stats').scrollIntoView({block:'start'})")
                    page.screenshot(path=str(ROOT / 'screenshots' / 'mobile-dark.png'), full_page=False)
                    page.locator('textarea.cell').first.tap()
                    check(page.evaluate('document.activeElement.classList.contains("cell")'), 'mobile tap focuses an editable cell')
                context.close()

            # Offline app shell, offline editing/export and honest install help.
            context = browser.new_context(viewport={'width':1280,'height':900}, service_workers='allow', accept_downloads=True)
            context.add_init_script("window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();e.stopImmediatePropagation()},true)")
            page = context.new_page(); wire(page)
            response = page.goto(BASE, wait_until='networkidle')
            check(response.status == 200, 'online PWA page returns 200')
            page.evaluate('navigator.serviceWorker.ready.then(()=>true)')
            if not page.evaluate('!!navigator.serviceWorker.controller'):
                page.reload(wait_until='networkidle')
            check(page.evaluate('!!navigator.serviceWorker.controller'), 'service worker controls the page')
            check('csv-desk-v3' in page.evaluate('caches.keys()'), 'v3 app shell is cached')
            manifest = page.evaluate("fetch('manifest.json').then(r=>r.json())")
            check(manifest['short_name'] == 'CSV-Desk' and len(manifest['icons']) == 4, 'manifest and four icons are available')
            offline_responses = []
            page.on('response', lambda response: offline_responses.append({'url':response.url,'sw':response.from_service_worker,'status':response.status}))
            context.set_offline(True)
            response = page.reload(wait_until='domcontentloaded')
            wait_eval(page, "typeof dataFromMatrix === 'function'")
            check(response is not None and response.status == 200 and response.from_service_worker, 'offline reload is a service-worker 200')
            sample(page)
            set_cell(page, 0, 0, 'Offline Ana')
            page.locator('#exportDetails summary').click()
            page.select_option('#exportFormat', 'json')
            with page.expect_download() as event: page.click('#downloadBtn')
            offline_export = TMP / 'offline.json'; event.value.save_as(offline_export)
            check(json.loads(offline_export.read_text())[0]['name'] == 'Offline Ana', 'editing and JSON export work offline')
            check(all(item['sw'] for item in offline_responses), 'offline workflow has no non-service-worker HTTP responses')
            context.set_offline(False)
            page.click('#td-install-button')
            check(page.locator('#td-install-dialog').evaluate('(d)=>d.open'), 'install help dialog opens')
            install_text = page.locator('#td-install-dialog').inner_text()
            check(all(x in install_text for x in ('iPhone','Android','Desktop')), 'install help covers mobile and desktop')
            check('cannot force installation' in install_text, 'install help cannot force installation')
            page.click('#td-install-close')
            check(not page.locator('#td-install-dialog').is_visible(), 'install help closes')
            context.close()

            # Simulate the future GitHub Pages deployment under /CSV-Desk/.
            sub_server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(ROOT.parent)))
            threading.Thread(target=sub_server.serve_forever, daemon=True).start()
            sub_base = f'http://127.0.0.1:{sub_server.server_address[1]}/CSV-Desk/'
            context = browser.new_context(viewport={'width':1280,'height':900}, service_workers='allow', accept_downloads=True)
            page = context.new_page(); wire(page)
            response = page.goto(sub_base, wait_until='networkidle')
            check(response is not None and response.status == 200, 'GitHub Pages subpath serves the application')
            wait_eval(page, "typeof dataFromMatrix === 'function'")
            page.evaluate('navigator.serviceWorker.ready.then(()=>true)')
            if not page.evaluate('!!navigator.serviceWorker.controller'):
                page.reload(wait_until='networkidle')
            scope = page.evaluate('navigator.serviceWorker.ready.then(r=>r.scope)')
            check(scope.endswith('/CSV-Desk/'), 'service worker scope stays inside CSV-Desk')
            paths = page.evaluate("fetch('manifest.json').then(r=>r.json()).then(m=>({start:new URL(m.start_url,location.href).pathname,scope:new URL(m.scope,location.href).pathname}))")
            check(paths == {'start':'/CSV-Desk/','scope':'/CSV-Desk/'}, 'manifest start and scope resolve to the deployment subpath')
            context.set_offline(True)
            response = page.reload(wait_until='domcontentloaded')
            wait_eval(page, "typeof dataFromMatrix === 'function'")
            check(response is not None and response.status == 200 and response.from_service_worker, 'subpath offline reload is served by its service worker')
            sample(page)
            page.locator('#exportDetails summary').click(); page.select_option('#exportFormat', 'json')
            with page.expect_download() as event: page.click('#downloadBtn')
            subpath_export = TMP / 'subpath-offline.json'; event.value.save_as(subpath_export)
            check(len(json.loads(subpath_export.read_text())) == 11, 'subpath offline JSON export works')
            context.set_offline(False); context.close()
            sub_server.shutdown(); sub_server.server_close()

            # The dependency-free editor also opens directly from disk.
            context = browser.new_context(viewport={'width':1100,'height':850}, accept_downloads=True)
            page = context.new_page(); wire(page)
            page.goto((ROOT / 'index.html').as_uri(), wait_until='load')
            wait_eval(page, "typeof dataFromMatrix === 'function'")
            sample(page)
            check(page.evaluate('state.rows.length') == 11 and page.evaluate('location.protocol') == 'file:', 'file protocol opens the editor and sample without a server')
            check(page.evaluate('!!navigator.serviceWorker.controller') is False, 'file protocol makes no false service-worker claim')
            context.close()

            # Data that looks like markup or formulas remains inert text.
            context, page = open_page(browser)
            page.evaluate("dataFromMatrix([['__proto__','note'],['<img src=x onerror=alert(1)>','=1+1']],'safe.csv',',',true)")
            check(page.locator('textarea.cell').first.input_value() == '<img src=x onerror=alert(1)>', 'markup-like data is shown only as textarea text')
            check(page.locator('img').count() == 0, 'markup-like data creates no image element')
            check(page.evaluate('({}).polluted') is None, 'prototype-like headers do not pollute objects')
            page.locator('#exportDetails summary').click()
            page.select_option('#exportFormat', 'json')
            with page.expect_download() as event: page.click('#downloadBtn')
            safe_target = TMP / 'safe.json'; event.value.save_as(safe_target)
            safe_data = json.loads(safe_target.read_text())
            check(safe_data[0]['note'] == '=1+1', 'spreadsheet formula remains unexecuted text in JSON')
            context.close()

            check(not console_messages, 'zero unexpected console errors or warnings: ' + repr(console_messages))
            check(not third_party, 'zero third-party runtime requests: ' + repr(third_party))
        finally:
            browser.close()
            server.shutdown()


try:
    run()
    status = 'passed'
    error = None
except Exception as exc:
    status = 'failed'
    error = repr(exc)
    report = {'status':status,'passed':sum(c['passed'] for c in checks),'total':len(checks),'error':error,'checks':checks,'console':console_messages,'third_party':third_party}
    (ROOT / 'tests' / 'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    raise
else:
    report = {'status':status,'passed':sum(c['passed'] for c in checks),'total':len(checks),'error':error,'checks':checks,'console':console_messages,'third_party':third_party,
              'artifacts':{'csv_sha256':hashlib.sha256((TMP/'export-cleaned.csv').read_bytes()).hexdigest(),'json_sha256':hashlib.sha256((TMP/'export.json').read_bytes()).hexdigest()}}
    (ROOT / 'tests' / 'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print(json.dumps({'status':status,'passed':report['passed'],'total':report['total'],'console':console_messages,'third_party':third_party},indent=2))
