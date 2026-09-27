/* Account sections use URL fragments; the original section anchors remain valid. */
(function () {
    'use strict';
    const panels = [...document.querySelectorAll('[data-account-panel]')];
    const links = [...document.querySelectorAll('[data-account-section]')];
    if (!panels.length) return;
    const securityAnchors = new Set(['security', 'password', 'recovery', 'password-title', 'security-title', 'curPw', 'newPw', 'secPw', 'secQ', 'secA']);
    function applySection() {
        const anchor = location.hash.slice(1);
        const active = securityAnchors.has(anchor) ? 'security' : 'subscriptions';
        panels.forEach(panel => { panel.hidden = panel.dataset.accountPanel !== active; });
        links.forEach(link => {
            if (link.dataset.accountSection === active) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        });
        if (anchor && anchor !== active) {
            const target = document.getElementById(anchor);
            if (target && target.closest('[data-account-panel]')) target.scrollIntoView({block: 'start'});
        }
    }
    addEventListener('hashchange', applySection);
    applySection();
})();
