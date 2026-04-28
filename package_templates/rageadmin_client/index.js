const UI_URL = 'http://package/rageadmin/ui/index.html';

let browser = null;
let browserReady = false;
let queuedCalls = [];

function safeJson(value, fallback) {
    if (typeof value === 'string') {
        return value;
    }
    try {
        return JSON.stringify(value == null ? fallback : value);
    } catch (err) {
        return JSON.stringify(fallback || {});
    }
}

function ensureBrowser() {
    if (browser) {
        return browser;
    }

    browser = mp.browsers.new(UI_URL);
    browser.active = true;
    browser.inputEnabled = false;
    browser.mouseInputEnabled = false;
    browser.orderId = 9999;
    return browser;
}

function flushBrowserCalls() {
    if (!browser || !browserReady || queuedCalls.length === 0) {
        return;
    }
    const calls = queuedCalls.slice();
    queuedCalls = [];
    calls.forEach((entry) => {
        try {
            browser.call(entry.eventName, entry.payload);
        } catch (err) {}
    });
}

function callBrowser(eventName, payload) {
    ensureBrowser();
    const serialized = safeJson(payload, {});
    if (!browserReady) {
        queuedCalls.push({ eventName, payload: serialized });
        return;
    }
    try {
        browser.call(eventName, serialized);
    } catch (err) {}
}

mp.events.add('browserDomReady', (createdBrowser) => {
    if (!browser || createdBrowser !== browser) {
        return;
    }
    browserReady = true;
    flushBrowserCalls();
});

mp.events.add('browserLoadingFailed', (createdBrowser, url, errorCode) => {
    if (browser && createdBrowser === browser) {
        mp.gui.chat.push(`[RageAdmin] Notice UI failed to load (${errorCode || 'unknown'}): ${url || UI_URL}`);
    }
});

mp.events.add('rageadmin:ui:notice', (rawPayload) => {
    callBrowser('rageadminPushNotice', rawPayload || '{}');
});

mp.events.add('playerReady', () => {
    ensureBrowser();
});

ensureBrowser();
