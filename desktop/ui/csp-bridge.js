(() => {
    const bridge = window.pywebview;
    if (!bridge?.api || !bridge._checkValue || !bridge._jsApiCallback) return;
    const permitted = new Set(__WATCHER_BRIDGE_METHODS__);
    bridge._createApi = functions => {
        for (const {func: name} of functions) {
            if (!permitted.has(name)) continue;
            bridge._returnValuesCallbacks[name] = {};
            bridge.api[name] = (...args) => {
                const id = crypto.randomUUID();
                const result = new Promise((resolve, reject) => bridge._checkValue(name, resolve, reject, id));
                bridge._jsApiCallback(name, args, id);
                return result;
            };
        }
    };
})();
