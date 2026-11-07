#!/usr/bin/env python3
"""Run on the LicenseBot VPS with /opt/licensebot/.venv/bin/python. Contains no secrets."""
import asyncio
import getpass
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def update_env(text, settings):
    ending = '\r\n' if '\r\n' in text else '\n'
    seen, lines = set(), []
    for line in text.splitlines(keepends=True):
        match = re.match(r'^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=', line)
        key = match.group(1) if match else None
        if key in settings:
            if key not in seen:
                lines.append(key + '=' + json.dumps(settings[key], ensure_ascii=False) + ending)
                seen.add(key)
        else:
            lines.append(line)
    result = ''.join(lines)
    for key, value in settings.items():
        if key not in seen:
            if result and not result.endswith(('\n', '\r')):
                result += ending
            result += key + '=' + json.dumps(value, ensure_ascii=False) + ending
    return result


def atomic_write(path, data, owner):
    fd, temporary = tempfile.mkstemp(prefix='.env-resume-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if os.geteuid() == 0:
            os.fchown(fd, owner.st_uid, owner.st_gid)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def service(*arguments):
    subprocess.run(['systemctl', *arguments, 'licensebot.service'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)


def check_api(port):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/resume/analyze', data=b'{}',
                                     headers={'Content-Type': 'application/json'})
    for _ in range(15):
        try:
            urllib.request.urlopen(request, timeout=2).close()
        except urllib.error.HTTPError as error:
            if error.code == 403 and json.loads(error.read()).get('reason') == 'no_license':
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('API анализа не ответил после перезапуска')


async def check_provider(key, configured_model):
    import aiohttp
    from bot.resume_analysis import PolzaClient
    client = PolzaClient(key, configured_model)
    async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + key},
                                     timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as http:
        model, _ = await client._resolve(http)
        # One short paid request checks credentials, balance and completion access.
        response = await client._json(http, 'POST', '/chat/completions', json={
            'model': model, 'messages': [{'role': 'user', 'content': 'Ответь одним словом: готово.'}],
            'max_tokens': 128, 'stream': False,
        })
        if not isinstance(response.get('choices'), list) or not response['choices']:
            raise RuntimeError('Модель не вернула ответ')
        return model


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Запустите эту настройку от root в консоли VPS')
    app = Path('/opt/licensebot')
    env_file = app / '.env'
    if not (app / 'bot/resume_analysis.py').is_file():
        raise RuntimeError('На VPS ещё нет серверного обновления анализа резюме. Проверьте выкладку LicenseBot.')
    if not env_file.is_file() or env_file.is_symlink():
        raise RuntimeError('Не найден обычный файл /opt/licensebot/.env')
    sys.path.insert(0, str(app))
    from dotenv import dotenv_values
    before = env_file.read_bytes()
    owner = env_file.stat()
    values = dotenv_values(stream=io.StringIO(before.decode('utf-8')))
    port = int(values.get('LICENSE_API_PORT') or 8791)
    if port <= 0:
        raise RuntimeError('API лицензий выключен в .env (LICENSE_API_PORT)')
    try:
        with open('/dev/tty', 'r+'):
            pass
    except OSError:
        raise RuntimeError('Нужна интерактивная консоль для скрытого ввода ключа') from None
    print('Настройка анализа резюме. Ключ вводится скрыто и остаётся в серверном .env.')
    key = getpass.getpass('Ключ Polza (Enter — использовать уже настроенный): ').strip()
    key = key or (values.get('POLZA_API_KEY') or '').strip()
    if not key or '\n' in key or '\r' in key or '\0' in key:
        raise RuntimeError('Ключ не указан или имеет неверный формат')
    print('Проверяю каталог и один короткий запрос к модели…')
    try:
        model = asyncio.run(check_provider(key, values.get('POLZA_RESUME_MODEL') or ''))
    except Exception:
        raise RuntimeError('Polza не подтвердил доступ к модели. .env не изменён. Проверьте ключ, баланс и ID DeepSeek Flash.') from None
    updated = update_env(before.decode('utf-8'), {'POLZA_API_KEY': key, 'POLZA_RESUME_MODEL': model})
    parsed = dotenv_values(stream=io.StringIO(updated))
    if parsed.get('POLZA_API_KEY') != key or parsed.get('POLZA_RESUME_MODEL') != model:
        raise RuntimeError('Не удалось подготовить .env; файл не изменён')
    backup = app / ('.env.resume-backup-' + time.strftime('%Y%m%d-%H%M%S'))
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(before)
    atomic_write(env_file, updated.encode('utf-8'), owner)
    try:
        service('restart')
        check_api(port)
        service('is-active', '--quiet')
    except Exception:
        atomic_write(env_file, before, owner)
        try:
            service('restart')
        except Exception:
            pass
        raise RuntimeError('Проверка LicenseBot не прошла. Прежний .env восстановлен. Проверьте службу licensebot.') from None
    print('ГОТОВО. Анализ резюме подключён; LicenseBot работает, API анализа отвечает.')
    print('Модель:', model)
    print('Резервная копия прежнего .env:', backup)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nНастройка прервана.', file=sys.stderr)
        sys.exit(130)
    except RuntimeError as error:
        print('ОШИБКА:', str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('ОШИБКА: не удалось завершить настройку. Проверьте доступ к файлам и службу LicenseBot.', file=sys.stderr)
        sys.exit(1)
