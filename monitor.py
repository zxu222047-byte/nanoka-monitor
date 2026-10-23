"""Nanoka HSR data monitor. Python 3.11+, standard library only."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

ROOT = Path(__file__).resolve().parent
MANIFEST = 'https://static.nanoka.cc/manifest.json'
BASE = 'https://static.nanoka.cc/hsr'
KINDS = ('character', 'lightcone', 'relicset', 'monster')


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def fetch(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'NanokaUpdateMonitor/1.0',
                'Cache-Control': 'no-cache', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=25) as response:
                data = response.read(20_000_001)
            if len(data) > 20_000_000:
                raise ValueError('Response exceeds limit')
            result = json.loads(data.decode('utf-8-sig'))
            if not isinstance(result, dict):
                raise ValueError('Expected JSON object')
            return result
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def snapshot(config, getter=fetch):
    manifest = getter(MANIFEST).get('hsr')
    if not isinstance(manifest, dict):
        raise ValueError('Missing HSR manifest')
    version = manifest.get('latest', '')
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', version):
        raise ValueError('Invalid version')
    if version not in manifest.get('available', []):
        raise ValueError('Latest version not available')
    records = {'manifest': digest(manifest)}
    labels = {'manifest': '星铁版本清单'}
    names = {'character': '角色', 'lightcone': '光锥',
             'relicset': '遗器/饰品', 'monster': '怪物'}
    for kind in KINDS:
        index = getter(f'{BASE}/{version}/{kind}.json')
        if not index:
            raise ValueError(f'Empty {kind} index')
        records[kind] = digest(index)
        labels[kind] = names[kind] + '目录'
        ids = {str(i) for i in manifest.get('new', {}).get(kind, [])}
        ids.update(str(i) for i in config.get('extra_ids', {}).get(kind, []))
        if kind == 'character' and config.get('watch_elation_characters', True):
            ids.update(str(i) for i, row in index.items()
                       if row.get('baseType') == 'Elation')
        for item_id in sorted(ids):
            if not item_id.isdigit():
                raise ValueError('Invalid item ID')
            # Extra IDs can disappear in a new cycle; new manifest IDs must exist.
            if item_id not in index:
                if item_id in {str(i) for i in manifest.get('new', {}).get(kind, [])}:
                    raise ValueError('New content missing from index')
                continue
            detail = getter(f'{BASE}/{version}/zh/{kind}/{item_id}.json')
            if not detail or not detail.get('name'):
                raise ValueError('Incomplete detail')
            key = f'{kind}/{item_id}'
            records[key] = digest(detail)
            labels[key] = names[kind] + '：' + str(detail['name'])
    # Refuse mixed snapshots if deployment changes during downloads.
    if digest(getter(MANIFEST).get('hsr')) != digest(manifest):
        raise ValueError('Manifest changed during scan; retry next run')
    return {'version': version, 'records': records, 'labels': labels}


def differences(old, current):
    a, b = old['records'], current['records']
    rows = []
    for key in sorted(a.keys() | b.keys()):
        if a.get(key) != b.get(key):
            verb = '新增' if key not in a else '移除' if key not in b else '调整'
            label = current['labels'].get(key, old['labels'].get(key, key))
            rows.append(f'{verb}：{label} [{key}]')
    return rows


def email_send(subject, body):
    required = ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'MAIL_TO')
    if any(not os.environ.get(k) for k in required):
        raise ValueError('Missing mail settings; configure GitHub Secrets')
    recipients = [x.strip() for x in os.environ['MAIL_TO'].split(',') if x.strip()]
    sender = os.environ.get('MAIL_FROM') or os.environ['SMTP_USER']
    msg = EmailMessage()
    msg['Subject'], msg['From'], msg['To'] = subject, sender, ', '.join(recipients)
    msg.set_content(body)
    context = ssl.create_default_context()
    mode = os.environ.get('SMTP_SECURITY') or 'ssl'
    port = int(os.environ.get('SMTP_PORT') or ('465' if mode == 'ssl' else '587'))
    if mode not in ('ssl', 'starttls'):
        raise ValueError('Use ssl or starttls')
    client = (smtplib.SMTP_SSL(os.environ['SMTP_HOST'], port, context=context, timeout=30)
              if mode == 'ssl' else smtplib.SMTP(os.environ['SMTP_HOST'], port, timeout=30))
    with client:
        if mode == 'starttls':
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
        refused = client.send_message(msg, from_addr=sender, to_addrs=recipients)
        if refused:
            raise RuntimeError('Some recipients rejected; notification will retry')


def process(state, current, confirmations, sender=email_send):
    """Advance confirmed baseline only after successful notification."""
    state = copy.deepcopy(state)
    if not state.get('baseline'):
        return {'baseline': current, 'candidate': None}, 'baseline_created'
    old = state['baseline']
    if digest(old['records']) == digest(current['records']) and old['version'] == current['version']:
        state['candidate'] = None
        return state, 'unchanged'
    fingerprint = digest(current)
    candidate = state.get('candidate') or {}
    count = candidate.get('count', 0) + 1 if candidate.get('fingerprint') == fingerprint else 1
    state['candidate'] = {'fingerprint': fingerprint, 'count': count}
    if count < confirmations:
        return state, 'pending_confirmation'
    changes = differences(old, current)
    when = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    body = (f'Nanoka 星穹铁道数据检测到更新\n\n'
            f'上次版本：{old["version"]}\n当前版本：{current["version"]}\n'
            f'确认时间：{when}\n连续确认：{count}次\n\n'
            + '\n'.join(changes[:100])
            + ('\n（更多变化请查看网站）' if len(changes) > 100 else '')
            + '\n\nhttps://hsr.nanoka.cc/character\n'
            '本邮件列出变化条目，不等同于完整数值差分或官方更新公告。')
    sender(f'[Nanoka更新] 星铁 {old["version"]} → {current["version"]}', body)
    return {'baseline': current, 'candidate': None}, 'notified'


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)+'\n', encoding='utf-8')
    temp.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dry-run', action='store_true', help='Read website without email or state writes')
    p.add_argument('--test-email', action='store_true', help='Send one explicitly requested test email')
    p.add_argument('--state', type=Path, default=ROOT/'.monitor/state.json')
    p.add_argument('--config', type=Path, default=ROOT/'config.json')
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    n = config.get('confirmations', 2)
    if type(n) is not int or not 1 <= n <= 5:
        raise ValueError('confirmations must be between 1 and 5')
    if args.test_email:
        email_send('[Nanoka监测] 测试邮件', '邮件配置成功。此邮件不代表网站发生更新。')
        print('test_email_sent')
        return
    current = snapshot(config)
    if args.dry_run:
        print(json.dumps({'mode': 'dry_run', 'version': current['version'],
                          'watched_records': len(current['records']),
                          'email_sent': False, 'state_written': False}, ensure_ascii=False))
        return
    state = json.loads(args.state.read_text(encoding='utf-8')) if args.state.exists() else {}
    updated, status = process(state, current, n)
    if updated != state:
        write_state(args.state, updated)
    print(json.dumps({'status': status, 'version': current['version'],
                      'watched_records': len(current['records'])}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not print SMTP responses, addresses or credentials in public logs.
        print(f'Monitor failed ({type(exc).__name__}). Baseline preserved; inspect settings or retry.', file=sys.stderr)
        sys.exit(1)
