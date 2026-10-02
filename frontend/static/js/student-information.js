(function () {
    'use strict';
    // No request on article open: an explicit click is the only paid trigger.
    document.addEventListener('click', async event => {
        const button = event.target.closest('[data-student-analyze]');
        if (!button || button.disabled) return;
        const panel = button.closest('[data-student-information]');
        const message = panel.querySelector('[data-student-status]');
        const endpoint = '/api/announcements/' + panel.dataset.studentInformation + '/student-information';
        button.disabled = true;
        message.hidden = false;
        message.textContent = '正在提交分析…';
        try {
            let method = 'POST';
            for (let attempt = 0; attempt < 30 && panel.isConnected; attempt++) {
                const response = await fetch(endpoint, {method, headers: {
                    'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content
                }, signal: AbortSignal.timeout(15000)});
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || '分析暂时未完成');
                if (data.html) {
                    panel.outerHTML = data.html;
                    return;
                }
                message.textContent = data.message;
                if (!data.active) return;
                method = 'GET';
                await new Promise(resolve => setTimeout(resolve, 2000));
            }
            message.textContent = '分析仍在后台继续，稍后可再次查看；相同请求会复用已有任务。';
        } catch (error) {
            message.textContent = error.name === 'TimeoutError' ? '连接超时，可稍后再次查看分析进度。' : error.message;
        } finally {
            button.disabled = false;
        }
    });
})();
