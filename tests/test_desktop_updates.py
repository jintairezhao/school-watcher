"""Release selection, integrity verification and desktop-only ownership behavior."""
import asyncio
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop.updater import (REPOSITORY, UpdateError, asset_name, check_update,
                             download_update, platform_key, version_tuple)


class UpdateTests(unittest.TestCase):
    def fixture(self, version='0.2.0', target='windows-x64'):
        payload = b'a valid installer fixture'
        name = asset_name(version, target)
        base = f'https://github.com/{REPOSITORY}/releases/download/v{version}/'
        release = {'tag_name': 'v' + version, 'draft': False, 'prerelease': False,
            'body': 'Release notes', 'assets': [
                {'name': name, 'size': len(payload), 'browser_download_url': base + name},
                {'name': 'SHA256SUMS.txt', 'browser_download_url': base + 'SHA256SUMS.txt'}]}
        contents = {f'https://api.github.com/repos/{REPOSITORY}/releases/latest': json.dumps(release).encode(),
                    base + name: payload,
                    base + 'SHA256SUMS.txt': f'{hashlib.sha256(payload).hexdigest()}  {name}\n'.encode()}
        def opener(url):
            return io.BytesIO(contents[url])
        return payload, release, contents, opener

    def test_selects_all_three_native_architectures(self):
        for system, machine, target in [('Windows','AMD64','windows-x64'),('Darwin','arm64','macos-arm64'),('Darwin','x86_64','macos-x64')]:
            self.assertEqual(platform_key(system,machine),target)
            _,_,_,opener = self.fixture(target=target)
            update = check_update('0.1.0',target,opener)
            self.assertEqual(update.name, asset_name('0.2.0',target))

    def test_no_downgrade_and_numeric_version_order(self):
        _,_,_,opener = self.fixture()
        self.assertIsNone(check_update('0.2.0','windows-x64',opener))
        self.assertIsNone(check_update('1.0.0','windows-x64',opener))
        self.assertGreater(version_tuple('v0.10.0'), version_tuple('0.9.0'))

    def test_rejects_missing_hash_foreign_assets_and_prereleases(self):
        for mutation in ('hash','url','prerelease'):
            _, release, contents, opener = self.fixture()
            if mutation == 'hash':
                contents[release['assets'][1]['browser_download_url']] = b''
            elif mutation == 'url':
                release['assets'][0]['browser_download_url'] = 'https://example.com/installer.exe'
            else:
                release['prerelease'] = True
            contents[f'https://api.github.com/repos/{REPOSITORY}/releases/latest'] = json.dumps(release).encode()
            with self.assertRaises(UpdateError):
                check_update('0.1.0','windows-x64',opener)

    def test_complete_download_and_tampering_never_leave_an_installer(self):
        payload, release, contents, opener = self.fixture()
        update = check_update('0.1.0','windows-x64',opener)
        with tempfile.TemporaryDirectory() as temp:
            result = download_update(update,temp,opener=opener)
            self.assertEqual(result.read_bytes(),payload)
            result.unlink()
            contents[update.url] = b'x' * len(payload)
            with self.assertRaises(UpdateError):
                download_update(update,temp,opener=opener)
            self.assertFalse(result.exists())
            self.assertFalse(list(Path(temp).rglob('*.part')))

    def test_truncated_and_oversize_downloads_are_rejected(self):
        payload, _, contents, opener = self.fixture()
        update = check_update('0.1.0','windows-x64',opener)
        for invalid in (payload[:-1], payload+b'x'):
            contents[update.url] = invalid
            with tempfile.TemporaryDirectory() as temp, self.assertRaises(UpdateError):
                download_update(update,temp,opener=opener)

    def test_refuses_path_traversal_and_invalid_versions(self):
        _,_,_,opener = self.fixture()
        update = check_update('0.1.0','windows-x64',opener)
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(UpdateError):
            download_update(replace(update,name='../escape.exe'),temp,opener=opener)
        for value in ('1.2.3/../../bad','v1.0.0-beta','1.2','01.2.3'):
            with self.assertRaises(UpdateError):
                version_tuple(value)


class DesktopOwnershipTests(unittest.TestCase):
    def test_server_registration_never_grants_admin(self):
        from backend import create_app
        from backend.database.db import db
        from backend.database.models import User
        for desktop, expected in ((False,'user'),):
            app = create_app({'TESTING':True,'SECRET_KEY':'isolated-desktop-test',
                              'SQLALCHEMY_DATABASE_URI':'sqlite://','DESKTOP_MODE':desktop})
            with app.app_context():
                db.create_all()
                for number in (1,2):
                    client = app.test_client()
                    with client.session_transaction() as session:
                        session['_csrf_token'] = 'test-token'
                    response = client.post('/register',data={'username':f'local{number}','password':'test-password',
                        'confirm':'test-password','csrf_token':'test-token'})
                    self.assertEqual(response.status_code,302)
                    self.assertEqual(db.session.get(User,number).role,expected if number==1 else 'user')
                db.session.remove()
                db.engine.dispose()

    def test_mac_verification_does_not_start_linux_display_services(self):
        from backend.browser_service.display import PrivateDisplay
        async def check():
            display = PrivateDisplay()
            with patch('backend.browser_service.display.sys.platform','darwin'), patch('asyncio.create_subprocess_exec') as spawn:
                self.assertEqual(await display.start(),{})
                await display.expose()
                await display.close()
                spawn.assert_not_called()
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
