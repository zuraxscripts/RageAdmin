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
    pendingConnections: new Map(),
    playerMeta: new Map(),
    monitor: null,
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

function buildConnectionKeys(meta) {
    const src = meta || {};
    const keys = [];
    const rgscId = safeText(src.rgscId || src.rgsc_id);
    const serial = safeText(src.serial);
    const socialClub = safeText(src.socialClub || src.social_club).toLowerCase();
    const ip = safeText(src.ip).replace(/^::ffff:/i, '');

    if (rgscId) keys.push(`rgsc:${rgscId.toLowerCase()}`);
    if (serial) keys.push(`serial:${serial.toLowerCase()}`);
    if (socialClub) keys.push(`social:${socialClub}`);
    if (ip) keys.push(`ip:${ip}`);
    return keys;
}

function pruneConnectionMeta() {
    const now = Date.now();
    state.pendingConnections.forEach((meta, key) => {
        if (!meta || !meta.storedAt || (now - meta.storedAt) > 120000) {
            state.pendingConnections.delete(key);
        }
    });
}

function rememberConnectionMeta(meta) {
    pruneConnectionMeta();
    const payload = Object.assign({}, meta || {});
    payload.ip = safeText(payload.ip).replace(/^::ffff:/i, '');
    payload.socialClub = safeText(payload.socialClub);
    payload.rgscId = safeText(payload.rgscId);
    payload.serial = safeText(payload.serial);
    payload.gameType = safeText(payload.gameType);
    payload.storedAt = Date.now();
    payload.keys = buildConnectionKeys(payload);
    payload.keys.forEach((key) => {
        state.pendingConnections.set(key, payload);
    });
    return payload;
}

function attachMetaToPlayer(player) {
    const serverId = getServerId(player);
    if (state.playerMeta.has(serverId)) {
        return state.playerMeta.get(serverId);
    }

    pruneConnectionMeta();
    const probe = {
        ip: safeText(player && player.ip).replace(/^::ffff:/i, ''),
        socialClub: safeText(player && player.socialClub),
        rgscId: safeText(player && player.rgscId),
        serial: safeText(player && player.serial)
    };
    const keys = buildConnectionKeys(probe);
    for (let i = 0; i < keys.length; i += 1) {
        const meta = state.pendingConnections.get(keys[i]);
        if (meta) {
            state.playerMeta.set(serverId, meta);
            (meta.keys || []).forEach((key) => state.pendingConnections.delete(key));
            return meta;
        }
    }

    const fallback = rememberConnectionMeta(probe);
    state.playerMeta.set(serverId, fallback);
    return fallback;
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
    const ip = safeText(player.ip).replace(/^::ffff:/i, '');
    const packetLoss = safeNumber(player.packetLoss, 0);
    const meta = attachMetaToPlayer(player);
    const gameType = safeText((meta && meta.gameType) || player.gameType);

    return {
        serverId,
        name: safeText(player.name, 'Unknown'),
        ping: safeNumber(player.ping, 0),
        packetLoss,
        ip,
        socialClub,
        rgscId,
        serial,
        gameType,
        session: Math.max(0, now - joinTime),
        sessionActive: true,
        joinTime,
        identifiers: {
            social_club: socialClub,
            rgsc_id: rgscId,
            serial,
            game_type: gameType
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
    state.playerMeta.delete(serverId);
    requestJson('POST', '/api/panel-hook/player-disconnect', payload, (err) => {
        if (err) {
            logError(`player-disconnect sync failed: ${err.message}`);
        }
    });
}

function sendIncomingConnection(ip, serial, rgscName, rgscId, gameType) {
    const payload = rememberConnectionMeta({
        ip,
        serial,
        socialClub: rgscName,
        rgscId,
        gameType
    });

    requestJson('POST', '/api/panel-hook/incoming-connection', payload, (err) => {
        if (err) {
            logError(`incoming-connection sync failed: ${err.message}`);
        }
    });
}

function syncPlayers() {
    refreshConfig(false);
    const players = mp.players.toArray().map(buildPlayerSnapshot);
    requestJson('POST', '/api/panel-hook/players-sync', { players }, (err) => {
        if (err) {
            logError(`players-sync failed: ${err.message}`);
            return;
        }
        broadcastMonitorState(false);
    });
}

function sendHeartbeat() {
    refreshConfig(false);
    requestJson('POST', '/api/panel-hook/heartbeat', {
        package: 'rageadmin',
        playerCount: mp.players.toArray().length,
        panelHost: state.config.panelHost
    }, (err, response) => {
        if (err) {
            logError(`heartbeat failed: ${err.message}`);
            broadcastMonitorState(true);
            return;
        }
        if (response && response.monitor && typeof response.monitor === 'object') {
            updateMonitorState(response.monitor, true);
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

function emitClientEvent(player, eventName, payload) {
    if (!player || !eventName) {
        return;
    }
    const serialized = JSON.stringify(payload || {});
    try {
        player.call(eventName, [serialized]);
    } catch (err) {}
}

function emitAllClientEvent(eventName, payload) {
    const serialized = JSON.stringify(payload || {});
    try {
        mp.players.call(eventName, [serialized]);
    } catch (err) {}
}

function buildMonitorState(overrides) {
    const base = Object.assign({}, state.monitor || {}, overrides || {});
    base.playersOnline = mp.players.toArray().length;
    return base;
}

function broadcastMonitorState(disconnected) {
    const payload = buildMonitorState({ disconnected: !!disconnected });
    if (!payload || Object.keys(payload).length === 0) {
        return;
    }
    emitAllClientEvent('rageadmin:ui:monitor', payload);
}

function updateMonitorState(monitor, broadcastNow) {
    if (!monitor || typeof monitor !== 'object') {
        return;
    }
    state.monitor = buildMonitorState(monitor);
    if (broadcastNow) {
        broadcastMonitorState(false);
    }
}

function sendMonitorToPlayer(player) {
    if (!player || !state.monitor) {
        return;
    }
    emitClientEvent(player, 'rageadmin:ui:monitor', buildMonitorState());
}

function buildUiNotice(action, fallbackTitle, fallbackVariant) {
    const src = action || {};
    return {
        title: safeText(src.title, fallbackTitle || 'RageAdmin'),
        message: safeText(src.message || src.reason),
        duration: Math.max(2, safeNumber(src.duration, 5)),
        variant: safeText(src.variant, fallbackVariant || 'message').toLowerCase()
    };
}

function sendUiNotice(player, action, fallbackTitle, fallbackVariant) {
    if (!player) {
        return;
    }
    emitClientEvent(player, 'rageadmin:ui:notice', buildUiNotice(action, fallbackTitle, fallbackVariant));
}

function broadcastUiNotice(action, fallbackTitle, fallbackVariant) {
    emitAllClientEvent('rageadmin:ui:notice', buildUiNotice(action, fallbackTitle, fallbackVariant));
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
                sendUiNotice(target, action, 'Warning', 'warn');
                sendToPlayer(target, message || 'Warning from admin', 'warn');
                return;
            }

            if (type === 'message') {
                sendUiNotice(target, action, 'Admin Message', 'message');
                sendToPlayer(target, message, 'message');
                return;
            }

            if (type === 'broadcast') {
                broadcastUiNotice(action, 'Server Announcement', 'announce');
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
                    sendUiNotice(target, action, 'Kick Notice', 'warn');
                    sendToPlayer(target, message, 'warn');
                }
                setTimeout(() => {
                    try {
                        target.kick(message || 'Kicked by admin');
                    } catch (err) {}
                }, 900);
                return;
            }

            if (type === 'ban') {
                if (!target) {
                    return;
                }
                if (message) {
                    sendUiNotice(target, action, 'Ban Notice', 'warn');
                    sendToPlayer(target, message, 'warn');
                }
                setTimeout(() => {
                    try {
                        target.ban(message || 'Banned by admin');
                    } catch (err) {}
                }, 900);
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

mp.events.add('incomingConnection', (ip, serial, rgscName, rgscId, gameType) => {
    sendIncomingConnection(ip, serial, rgscName, rgscId, gameType);
    return false;
});

mp.events.add('playerJoin', (player) => {
    state.joinTimes.set(getServerId(player), Math.floor(Date.now() / 1000));
    attachMetaToPlayer(player);
    sendPlayerJoin(player);
    sendMonitorToPlayer(player);
    setTimeout(() => broadcastMonitorState(false), 300);
    setTimeout(syncPlayers, 250);
});

mp.events.add('playerReady', (player) => {
    if (player) {
        sendPlayerJoin(player);
        sendMonitorToPlayer(player);
    }
});

mp.events.add('playerQuit', (player, exitType, reason) => {
    sendPlayerDisconnect(player, exitType, reason);
    setTimeout(() => broadcastMonitorState(false), 300);
    setTimeout(syncPlayers, 250);
});
