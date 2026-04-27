const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const Url = require('url');

const PACKAGE_NAME = 'RageAdmin';
const CONFIG_PATH = path.join(__dirname, 'config.json');
const DEFAULT_CONFIG = {
    panelHost: 'http://127.0.0.1:20000',
    panelSecret: 'changeme',
    syncIntervalMs: 5000,
    heartbeatIntervalMs: 15000,
    requestTimeoutMs: 5000,
    logVerbose: true
};

let state = {
    config: Object.assign({}, DEFAULT_CONFIG),
    lastConfigMtimeMs: 0,
    joinTimes: new Map(),
    syncTimer: null,
    heartbeatTimer: null,
    configTimer: null,
    actionsTimer: null,
    lastErrorLogAt: 0
};
state.config = loadConfig();

function log(message) {
    console.log(`[${PACKAGE_NAME}] ${message}`);
}

function logError(message) {
    const now = Date.now();
    if ((now - state.lastErrorLogAt) < 10000) {
        return;
    }
    state.lastErrorLogAt = now;
    log(message);
}

function safeNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
}

function safeText(value, fallback = '') {
    const text = String(value == null ? fallback : value).trim();
    return text || fallback;
}

function loadConfig() {
    try {
        const stats = fs.statSync(CONFIG_PATH);
        state.lastConfigMtimeMs = Number(stats.mtimeMs || 0);
        const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
        const parsed = JSON.parse(raw);
        return Object.assign({}, DEFAULT_CONFIG, parsed || {});
    } catch (err) {
        return Object.assign({}, DEFAULT_CONFIG);
    }
}

function refreshConfig(force) {
    try {
        const stats = fs.statSync(CONFIG_PATH);
        const mtime = Number(stats.mtimeMs || 0);
        if (!force && mtime === state.lastConfigMtimeMs) {
            return false;
        }
    } catch (err) {
        if (!force) {
            return false;
        }
    }

    state.config = loadConfig();
    if (state.config.logVerbose) {
        log(`Config reloaded: ${state.config.panelHost}`);
    }
    return true;
}

function buildUrl(endpoint) {
    const parsed = new Url.URL(endpoint, state.config.panelHost);
    return parsed;
}

function requestJson(method, endpoint, payload, callback) {
    let parsed;
    try {
        parsed = buildUrl(endpoint);
    } catch (err) {
        callback(err);
        return;
    }

    const body = payload == null ? '' : JSON.stringify(payload);
    const transport = parsed.protocol === 'https:' ? https : http;
    const req = transport.request({
        protocol: parsed.protocol,
        hostname: parsed.hostname,
        port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
        path: `${parsed.pathname}${parsed.search}`,
        method,
        headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(body),
            'X-Panel-Secret': state.config.panelSecret,
            'User-Agent': 'RageAdmin-Bridge/1.0'
        },
        timeout: safeNumber(state.config.requestTimeoutMs, 5000)
    }, (res) => {
        let raw = '';
        res.setEncoding('utf8');
        res.on('data', chunk => {
            raw += chunk;
        });
        res.on('end', () => {
            let parsedBody = null;
            if (raw) {
                try {
                    parsedBody = JSON.parse(raw);
                } catch (err) {
                    parsedBody = raw;
                }
            }
            if (res.statusCode >= 400) {
                callback(new Error(`HTTP ${res.statusCode}`), parsedBody, res.statusCode);
                return;
            }
            callback(null, parsedBody, res.statusCode);
        });
    });

    req.on('timeout', () => {
        req.destroy(new Error('Request timed out'));
    });

    req.on('error', err => {
        callback(err);
    });

    if (body) {
        req.write(body);
    }
    req.end();
}

function getServerId(player) {
    return safeNumber(player && player.id, -1);
}

function getJoinTime(serverId) {
    if (!state.joinTimes.has(serverId)) {
        state.joinTimes.set(serverId, Math.floor(Date.now() / 1000));
    }
    return state.joinTimes.get(serverId);
}

function buildPlayerSnapshot(player) {
    const serverId = getServerId(player);
    const joinTime = getJoinTime(serverId);
    const now = Math.floor(Date.now() / 1000);
    const socialClub = safeText(player.socialClub);
    const rgscId = safeText(player.rgscId);
    const serial = safeText(player.serial);
    const ip = safeText(player.ip);

    return {
        serverId,
        name: safeText(player.name, 'Unknown'),
        ping: safeNumber(player.ping, 0),
        ip,
        socialClub,
        rgscId,
        serial,
        session: Math.max(0, now - joinTime),
        sessionActive: true,
        joinTime,
        identifiers: {
            social_club: socialClub,
            rgsc_id: rgscId,
            serial
        }
    };
}

function findPlayerByServerId(serverId) {
    const wanted = safeNumber(serverId, -1);
    if (wanted < 0) {
        return null;
    }
    const players = mp.players.toArray();
    for (let i = 0; i < players.length; i += 1) {
        const player = players[i];
        if (getServerId(player) === wanted) {
            return player;
        }
    }
    return null;
}

function sendPlayerJoin(player) {
    const snapshot = buildPlayerSnapshot(player);
    requestJson('POST', '/api/panel-hook/player-join', snapshot, (err, response) => {
        if (err) {
            logError(`player-join sync failed: ${err.message}`);
            return;
        }
        if (response && response.banned) {
            const reason = safeText(response.reason, 'Banned');
            try {
                player.outputChatBox(`!{#f87171}[RageAdmin]!{#ffffff} ${reason}`);
            } catch (e) {}
            try {
                if (player && typeof player.ban === 'function') {
                    player.ban(reason);
                    return;
                }
            } catch (e) {}
            player.kick(reason);
        }
    });
}

function sendPlayerDisconnect(player, exitType, reason) {
    const serverId = getServerId(player);
    const payload = {
        serverId,
        reason: safeText(reason || exitType, 'disconnect')
    };
    state.joinTimes.delete(serverId);
    requestJson('POST', '/api/panel-hook/player-disconnect', payload, (err) => {
        if (err) {
            logError(`player-disconnect sync failed: ${err.message}`);
        }
    });
}

function syncPlayers() {
    refreshConfig(false);
    const players = mp.players.toArray().map(buildPlayerSnapshot);
    requestJson('POST', '/api/panel-hook/players-sync', { players }, (err) => {
        if (err) {
            logError(`players-sync failed: ${err.message}`);
        }
    });
}

function sendHeartbeat() {
    refreshConfig(false);
    requestJson('POST', '/api/panel-hook/heartbeat', {
        package: 'rageadmin',
        playerCount: mp.players.toArray().length,
        panelHost: state.config.panelHost
    }, (err) => {
        if (err) {
            logError(`heartbeat failed: ${err.message}`);
        }
    });
}

function sendToPlayer(player, message, kind) {
    const text = safeText(message);
    if (!player || !text) {
        return;
    }

    const prefix = kind === 'warn' ? '!{#fbbf24}[Warning]!{#ffffff} ' : '!{#c6a13a}[RageAdmin]!{#ffffff} ';
    try {
        player.outputChatBox(`${prefix}${text}`);
    } catch (err) {}
    try {
        player.notify(`${kind === 'warn' ? '~y~Warning~w~' : '~y~RageAdmin~w~'}: ${text}`);
    } catch (err) {}
}

function applyPendingActions() {
    refreshConfig(false);
    const endpoint = `/api/panel-hook/pending-actions?secret=${encodeURIComponent(state.config.panelSecret)}`;
    requestJson('GET', endpoint, null, (err, actions) => {
        if (err) {
            logError(`pending-actions failed: ${err.message}`);
            return;
        }
        if (!Array.isArray(actions) || actions.length === 0) {
            return;
        }

        actions.forEach((action) => {
            if (!action || typeof action !== 'object') {
                return;
            }

            const type = safeText(action.type).toLowerCase();
            const target = findPlayerByServerId(action.serverId);
            const message = safeText(action.message || action.reason);

            if (type === 'warn') {
                sendToPlayer(target, message || 'Warning from admin', 'warn');
                return;
            }

            if (type === 'message') {
                sendToPlayer(target, message, 'message');
                return;
            }

            if (type === 'broadcast') {
                mp.players.toArray().forEach((player) => {
                    sendToPlayer(player, message, 'message');
                });
                return;
            }

            if (type === 'kick') {
                if (!target) {
                    return;
                }
                if (message) {
                    sendToPlayer(target, message, 'warn');
                }
                target.kick(message || 'Kicked by admin');
                return;
            }

            if (type === 'ban') {
                if (!target) {
                    return;
                }
                if (message) {
                    sendToPlayer(target, message, 'warn');
                }
                target.ban(message || 'Banned by admin');
            }
        });
    });
}

function startIntervals() {
    if (state.syncTimer) {
        return;
    }
    state.syncTimer = setInterval(syncPlayers, Math.max(2000, safeNumber(state.config.syncIntervalMs, 5000)));
    state.heartbeatTimer = setInterval(sendHeartbeat, Math.max(5000, safeNumber(state.config.heartbeatIntervalMs, 15000)));
    state.configTimer = setInterval(() => refreshConfig(false), 10000);
    state.actionsTimer = setInterval(applyPendingActions, 1500);
    setTimeout(syncPlayers, 1500);
    setTimeout(sendHeartbeat, 2500);
}

mp.events.add('packagesLoaded', () => {
    refreshConfig(true);
    log(`bridge ready -> ${state.config.panelHost}`);
    startIntervals();
});

mp.events.add('playerJoin', (player) => {
    state.joinTimes.set(getServerId(player), Math.floor(Date.now() / 1000));
    sendPlayerJoin(player);
    setTimeout(syncPlayers, 250);
});

mp.events.add('playerReady', (player) => {
    if (player) {
        sendPlayerJoin(player);
    }
});

mp.events.add('playerQuit', (player, exitType, reason) => {
    sendPlayerDisconnect(player, exitType, reason);
    setTimeout(syncPlayers, 250);
});
