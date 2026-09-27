async function directoryRequest(url, method, body) {
    const response = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '操作未完成，请稍后重试');
    return data;
}
document.querySelectorAll('[data-subscribe-name]').forEach(button => {
    button.addEventListener('click', async () => {
        button.disabled = true;
        button.textContent = '正在订阅…';
        try {
            const schoolId = button.dataset.schoolId;
            const result = await directoryRequest(schoolId ? '/api/subscriptions' : '/api/catalog/subscribe', 'POST',
                schoolId ? {school_id: Number(schoolId)} : {name: button.dataset.subscribeName});
            location.href = '/subscriptions/' + (schoolId || result.school_id);
        } catch (error) {
            showToast('error', error.message);
            button.textContent = '重试订阅';
            button.disabled = false;
        }
    });
});
document.querySelectorAll('[data-unsubscribe]').forEach(button => {
    button.addEventListener('click', async () => {
        button.disabled = true;
        try {
            await directoryRequest('/api/subscriptions/' + button.dataset.unsubscribe, 'DELETE');
            location.reload();
        } catch (error) {
            showToast('error', error.message);
            button.disabled = false;
        }
    });
});
document.getElementById('submitSchoolForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.target.querySelector('button');
    const feedback = document.getElementById('submitFeedback');
    button.disabled = true;
    feedback.textContent = '正在添加学校…';
    try {
        const result = await directoryRequest('/api/schools', 'POST', {
            name: document.getElementById('subName').value.trim(), url: document.getElementById('subUrl').value.trim()
        });
        location.href = '/subscriptions/' + result.id;
    } catch (error) {
        feedback.textContent = error.message;
        button.disabled = false;
    }
});
