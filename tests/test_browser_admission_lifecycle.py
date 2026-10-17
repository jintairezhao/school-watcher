"""A closing page cannot leave a late origin grant blocking the next read."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.browser_service.admission import OriginAdmission, OriginBusy


class BrowserAdmissionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def admission(self):
        admission = OriginAdmission({'url': 'https://college.example.edu.cn/', 'request_id': 'a' * 64})
        admission.url = 'http://127.0.0.1:1/diagnostic-broker'
        return admission

    async def delayed_broker(self, *, cancel_reader=False, fail_after_commit=False, cancel_cleanup=False):
        admission = self.admission()
        entered, grant = asyncio.Event(), asyncio.Event()
        other = 'unrelated-worker-token'
        held, servers = {other}, []

        async def broker(action, data):
            if action == 'release':
                held.discard(data['token'])
                return {'released': True}

            async def commit():
                entered.set()
                await grant.wait()
                held.add(data['token'])
                if fail_after_commit:
                    raise OSError('Response lost after grant')
                return {'allowed': True}

            server = asyncio.create_task(commit())
            servers.append(server)
            # Server commit survives a client-side cancellation.
            return await asyncio.shield(server)

        with patch.object(admission, '_request', side_effect=broker):
            reading = asyncio.create_task(admission.admit('https://cdn.example.edu.cn/resource.js'))
            await entered.wait()
            if cancel_reader:
                reading.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await reading
            closing = asyncio.create_task(admission.close())
            await asyncio.sleep(0)
            if cancel_cleanup:
                closing.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await closing
                closing = asyncio.create_task(admission.close())
            grant.set()
            if not cancel_reader:
                if fail_after_commit:
                    with self.assertRaises(OSError):
                        await reading
                else:
                    await reading
            await asyncio.gather(*servers, return_exceptions=True)
            await closing
        self.assertEqual(held, {other}, 'Only this page\'s grant must be released, including late grants')
        self.assertFalse(admission.tokens)

    async def test_close_waits_for_a_late_successful_grant(self):
        await self.delayed_broker()

    async def test_cancelled_resource_cannot_leave_its_grant_behind(self):
        await self.delayed_broker(cancel_reader=True)

    async def test_lost_grant_response_still_releases_the_attempted_token(self):
        await self.delayed_broker(fail_after_commit=True)

    async def test_cancelled_cleanup_can_finish_the_original_pending_grant(self):
        await self.delayed_broker(cancel_reader=True, cancel_cleanup=True)

    async def test_closed_admission_rejects_later_callbacks_without_contacting_broker(self):
        admission = self.admission()
        with patch.object(admission, '_request', new_callable=AsyncMock) as broker:
            await admission.close()
            with self.assertRaises(OriginBusy):
                await admission.admit('https://college.example.edu.cn/late.js')
            broker.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
