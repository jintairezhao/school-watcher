(() => {
    const bridge = window.pywebview;
    if (!bridge?.api || !bridge._checkValue || !bridge._jsApiCallback) return;
    let repaired = false;
    for (const name of __WATCHER_BRIDGE_METHODS__) {
        if (typeof bridge.api[name] === 'function') continue;
        bridge._returnValuesCallbacks[name] = {};
        bridge.api[name] = (...args) => {
            const id = crypto.randomUUID();
            const result = new Promise((resolve, reject) => bridge._checkValue(name, resolve, reject, id));
            bridge._jsApiCallback(name, args, id);
            return result;
        };
        repaired = true;
    }
    if (repaired) window.dispatchEvent(new CustomEvent('pywebviewready'));
})();
