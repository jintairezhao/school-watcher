/* Delegated events also cover reader panels replaced by inbox navigation. */
(function () {
    const workspace = document.querySelector('.inbox-workspace');
    if (!workspace) return;
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
    const finePointer = matchMedia('(hover: hover) and (pointer: fine)');
    let frame = 0, activeLetter = null, pointerX = 0, pointerY = 0;

    function resetLetter() {
        cancelAnimationFrame(frame);
        frame = 0;
        if (activeLetter) {
            ['--letter-x', '--letter-y', '--letter-angle', '--letter-rx', '--letter-ry'].forEach(name => activeLetter.style.removeProperty(name));
        }
        activeLetter = null;
    }

    workspace.addEventListener('pointermove', event => {
        if (reducedMotion.matches || !finePointer.matches || event.pointerType === 'touch') return;
        const letter = event.target.closest('[data-reader-letter]');
        if (!letter) { if (activeLetter) resetLetter(); return; }
        if (activeLetter !== letter) { resetLetter(); activeLetter = letter; }
        pointerX = event.clientX;
        pointerY = event.clientY;
        if (frame) return;
        frame = requestAnimationFrame(() => {
            frame = 0;
            if (!activeLetter?.isConnected) { resetLetter(); return; }
            const bounds = activeLetter.getBoundingClientRect();
            const x = Math.max(-1, Math.min(1, (pointerX - bounds.left) / bounds.width * 2 - 1));
            const y = Math.max(-1, Math.min(1, (pointerY - bounds.top) / bounds.height * 2 - 1));
            activeLetter.style.setProperty('--letter-x', (x * 4).toFixed(2) + 'px');
            activeLetter.style.setProperty('--letter-y', (y * 3).toFixed(2) + 'px');
            activeLetter.style.setProperty('--letter-angle', (x * 5 - 2).toFixed(2) + 'deg');
            activeLetter.style.setProperty('--letter-rx', (12 - y * 7).toFixed(2) + 'deg');
            activeLetter.style.setProperty('--letter-ry', (x * 9 - 12).toFixed(2) + 'deg');
        });
    });
    workspace.addEventListener('pointerout', event => {
        if (activeLetter && !activeLetter.contains(event.relatedTarget)) resetLetter();
    });
    workspace.addEventListener('click', event => {
        const letter = event.target.closest('[data-reader-letter]');
        if (letter) {
            resetLetter();
            const opened = letter.getAttribute('aria-pressed') !== 'true';
            letter.setAttribute('aria-pressed', String(opened));
            letter.setAttribute('aria-label', opened ? '收起信纸' : '拆开信封');
            letter.querySelector('[data-letter-hint]').textContent = opened ? '再点一下，收好' : '点一下，拆封';
            return;
        }
        const start = event.target.closest('[data-reader-start]');
        if (start && event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
            const firstNotice = workspace.querySelector('#noticeList [data-notice-link]');
            if (firstNotice) {
                event.preventDefault();
                firstNotice.click();
            }
        }
    });
    reducedMotion.addEventListener('change', resetLetter);
    finePointer.addEventListener('change', resetLetter);
    window.addEventListener('inbox:navigated', resetLetter);
    document.addEventListener('visibilitychange', resetLetter);
    window.addEventListener('blur', resetLetter);
})();
