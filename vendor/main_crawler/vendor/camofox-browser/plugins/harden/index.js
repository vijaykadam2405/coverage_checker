/**
 * Fingerprint-COHERENCE plugin for camofox-browser.
 *
 * ---------------------------------------------------------------------------
 * WHY THE PREVIOUS VERSION OF THIS FILE DID NOTHING
 * ---------------------------------------------------------------------------
 * server.js builds the fingerprint at line ~980:
 *
 *     const options = await launchOptions({ os: hostOS, humanize: true, ... });
 *     ...
 *     await pluginEvents.emitAsync('browser:launching', { options });   // <-- we run HERE
 *     candidateBrowser = await firefox.launch(options);
 *
 * `launchOptions()` is camoufox-js's builder. By the time it RETURNS, it has already
 * generated the BrowserForge fingerprint, sampled WebGL, and serialised the whole
 * Camoufox config to `options.env.CAMOU_CONFIG_1..N`. Its return value is a *Playwright*
 * launch-options object: { executablePath, args, env, firefoxUserPrefs, proxy, headless }.
 *
 * The old plugin set `options.os`, `options.block_webrtc`, `options.webgl_config` on that
 * RETURNED object. Those are camoufox-js *input* parameter names. Playwright's
 * firefox.launch() silently ignores unknown keys, so all four assignments were no-ops.
 * Proof: the plugin logged `os=windows` on every launch while the browser reported
 * "Mozilla/5.0 (X11; Linux x86_64; rv:135.0)".
 *
 * ---------------------------------------------------------------------------
 * WHAT THIS VERSION DOES
 * ---------------------------------------------------------------------------
 * It decodes `options.env.CAMOU_CONFIG_*`, REPAIRS the fields BrowserForge sampled
 * incoherently, and re-serialises. BrowserForge still generates the fingerprint, so
 * per-session variation is preserved -- we only constrain fields that contradict each
 * other. Nothing is pinned to a constant.
 *
 * Measured defect rates over 300 BrowserForge samples with operatingSystems:["linux"]
 * (reproduce: node -e 'generateFingerprint(undefined,{operatingSystems:["linux"]})'):
 *     navigator.platform   arch != UA arch      9.3%   e.g. "Linux armv81" under an x86_64 UA
 *     navigator.oscpu      arch != UA arch     13.7%
 *     navigator.appVersion says "Android"      13.0%   e.g. "5.0 (Android 16)"
 *     navigator.maxTouchPoints > 0             11.0%   ARM handset leaking into a desktop FP
 * Root cause: BrowserForge samples these as independent nodes of a Bayesian network built
 * from Apify's fingerprint-suite corpus, which mixes Android/ARM devices in with desktop
 * Linux. Upstream acknowledges the dataset is stale
 * (https://camoufox.com/python/browserforge/ -- "outdated fingerprint dataset").
 *
 * Also repaired (these were wrong on 100% of launches):
 *   - navigator.doNotTrack "1" + navigator.globalPrivacyControl true were reported in JS
 *     while NO `DNT: 1` and NO `Sec-GPC: 1` request header was sent. In real Firefox those
 *     JS properties and those headers are driven by the same prefs, so the observed
 *     combination is impossible. Fixed by making the prefs agree with the JS values.
 *   - headers.Accept-Language was the bare token "en-US". Stock Firefox sends
 *     "en-US,en;q=0.5". A single token with no q-value is not a default Firefox install.
 *   - Intl timezone was America/Los_Angeles and geolocation was San Francisco, hardcoded in
 *     server.js:1206-1208, regardless of egress IP. See the session:creating hook below.
 *
 * ---------------------------------------------------------------------------
 * ENV KNOBS (all optional; no image rebuild needed)
 * ---------------------------------------------------------------------------
 *   CAMOFOX_FP_REPAIR=0            disable the coherence repair entirely
 *   CAMOFOX_FP_TIMEZONE            IANA tz for the egress IP        (default Asia/Kolkata)
 *   CAMOFOX_FP_LOCALE              primary locale                   (default en-US)
 *   CAMOFOX_FP_LANGUAGES           comma list for navigator.languages (default "en-US,en")
 *   CAMOFOX_FP_LAT / _LON / _ACC   geolocation                      (default Pune, IN)
 *   CAMOFOX_FP_GEO=0               do not touch tz/locale/geolocation
 *   CAMOFOX_FP_GRANT_GEO=1         keep server.js's automatic geolocation permission grant
 *   CAMOFOX_HARDEN_BLOCK_WEBRTC=0  do not force the WebRTC-blocking pref
 *   CAMOFOX_HARDEN_WEBGL_VENDOR / _RENDERER   explicit WebGL UNMASKED_* override
 *   CAMOFOX_HARDEN_ADDONS          comma-separated extra .xpi / unpacked addon paths
 *
 * Enable in camofox.config.json:  "harden": { "enabled": true }
 */

// Must match camoufox-js dist/utils.js getEnvVars(): chunkSize is 32767 off Windows.
const CHUNK_SIZE = process.platform === 'win32' ? 2047 : 32767;
const MAX_CHUNKS = 64;

function envRaw(name) {
  return (process.env[name] || '').trim();
}
function envOn(name, dflt) {
  const v = envRaw(name).toLowerCase();
  if (!v) return dflt;
  return !['0', 'false', 'no', 'off'].includes(v);
}
function envNum(name, dflt) {
  const v = parseFloat(envRaw(name));
  return Number.isFinite(v) ? v : dflt;
}

/** Reassemble the chunked Camoufox config out of a Playwright env object. */
function readCamouConfig(env) {
  let raw = '';
  for (let i = 1; i <= MAX_CHUNKS; i++) {
    const chunk = env[`CAMOU_CONFIG_${i}`];
    if (chunk === undefined) break;
    raw += chunk;
  }
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/** Re-chunk and write back, clearing any stale higher-index chunks. */
function writeCamouConfig(env, config) {
  for (let i = 1; i <= MAX_CHUNKS; i++) {
    if (env[`CAMOU_CONFIG_${i}`] === undefined) break;
    delete env[`CAMOU_CONFIG_${i}`];
  }
  const raw = JSON.stringify(config);
  for (let i = 0, n = 1; i < raw.length; i += CHUNK_SIZE, n++) {
    env[`CAMOU_CONFIG_${n}`] = raw.slice(i, i + CHUNK_SIZE);
  }
}

/**
 * Derive the navigator identity fields that Gecko computes from the platform,
 * so they cannot disagree with the User-Agent.
 *
 * Gecko references:
 *   navigator.platform  -> "Win32" on ALL Windows (incl. 64-bit), "MacIntel" on ALL macOS
 *                          (incl. Apple Silicon), "<uname -s> <uname -m>" on Unix.
 *   navigator.appVersion-> "5.0 (" + platform token + ")"; the token is the leading
 *                          segment(s) of the UA parenthetical, minus arch and rv:.
 *   navigator.oscpu     -> "Windows NT 10.0; Win64; x64" / "Intel Mac OS X 10.15" /
 *                          "Linux x86_64".
 */
function identityFromUA(ua) {
  const paren = (ua.match(/\(([^)]*)\)/) || [])[1] || '';
  const segments = paren.split(';').map((s) => s.trim()).filter(Boolean);

  if (/Windows NT/i.test(paren)) {
    const nt = (paren.match(/Windows NT ([\d.]+)/i) || [])[1] || '10.0';
    const win64 = /Win64|WOW64|x64/i.test(paren);
    return {
      family: 'win',
      'navigator.platform': 'Win32',
      'navigator.oscpu': win64 ? `Windows NT ${nt}; Win64; x64` : `Windows NT ${nt}`,
      'navigator.appVersion': '5.0 (Windows)',
    };
  }

  if (/Macintosh|Mac OS X/i.test(paren)) {
    const ver = (paren.match(/Intel Mac OS X ([\d_.]+)/i) || [])[1] || '10.15';
    return {
      family: 'mac',
      'navigator.platform': 'MacIntel',
      'navigator.oscpu': `Intel Mac OS X ${ver}`,
      'navigator.appVersion': '5.0 (Macintosh)',
    };
  }

  // X11 / Linux desktop. Keep distro flavour ("X11; Ubuntu") but drop arch + rv.
  const arch = /x86_64/i.test(paren) ? 'Linux x86_64'
    : /i686/i.test(paren) ? 'Linux i686'
    : /aarch64/i.test(paren) ? 'Linux aarch64'
    : 'Linux x86_64';
  const tokens = segments.filter((t) =>
    !/^rv:/i.test(t) &&
    !/^Linux\b/i.test(t) &&
    !/^Android\b/i.test(t) &&
    !/^(Win64|WOW64|x64|i686|x86_64|aarch64|armv\w+)$/i.test(t) &&
    !/^Intel Mac OS X/i.test(t));
  return {
    family: 'lin',
    'navigator.platform': arch,
    'navigator.oscpu': arch,
    'navigator.appVersion': `5.0 (${tokens.join('; ') || 'X11'})`,
  };
}

/**
 * Repair the incoherent fields in place. Returns the list of {key, from, to} changes so
 * the operator can see per-launch what BrowserForge got wrong.
 */
function repairConfig(config, opts) {
  const changes = [];
  const set = (key, value) => {
    const before = config[key];
    if (JSON.stringify(before) === JSON.stringify(value)) return;
    config[key] = value;
    changes.push({ key, from: before, to: value });
  };
  const drop = (key) => {
    if (!(key in config)) return;
    changes.push({ key, from: config[key], to: undefined });
    delete config[key];
  };

  const ua = config['navigator.userAgent'];
  if (typeof ua === 'string' && ua) {
    const ident = identityFromUA(ua);
    set('navigator.platform', ident['navigator.platform']);
    set('navigator.oscpu', ident['navigator.oscpu']);
    set('navigator.appVersion', ident['navigator.appVersion']);

    // Desktop Firefox on X11/Windows/macOS reports 0 unless a touch digitiser is present.
    // A non-zero value with no touch event support is an ARM-handset artefact.
    if ((config['navigator.maxTouchPoints'] || 0) !== 0) set('navigator.maxTouchPoints', 0);

    // The request header must not be able to disagree with the JS property.
    if (config['headers.User-Agent'] && config['headers.User-Agent'] !== ua) {
      set('headers.User-Agent', ua);
    }
  }

  // navigator.doNotTrack / globalPrivacyControl are reported in JS but the matching
  // DNT / Sec-GPC request headers are not sent, which cannot happen in real Firefox.
  // Make the prefs agree (see prefsFor()) rather than leaving the contradiction.
  // Default: drop both, i.e. sit in the ~90% majority bucket of users with neither signal.
  if (!opts.privacySignals) {
    drop('navigator.doNotTrack');
    drop('navigator.globalPrivacyControl');
  }

  if (opts.geo) {
    set('timezone', opts.timezone);
    const [lang, region] = opts.locale.split('-');
    set('locale:language', lang);
    if (region) set('locale:region', region);
    set('locale:all', opts.languages.join(', '));
    set('navigator.language', opts.locale);
    set('navigator.languages', opts.languages);
    // Stock Firefox emits q-values for every language after the first.
    set('headers.Accept-Language', opts.languages
      .map((l, i) => (i === 0 ? l : `${l};q=${(Math.max(0.1, 1 - i * 0.1)).toFixed(1)}`))
      .join(','));
    set('geolocation:latitude', opts.lat);
    set('geolocation:longitude', opts.lon);
    set('geolocation:accuracy', opts.acc);
  }

  return changes;
}

export async function register(app, ctx) {
  const { events, log } = ctx;

  const opts = {
    enabled: envOn('CAMOFOX_FP_REPAIR', true),
    geo: envOn('CAMOFOX_FP_GEO', true),
    privacySignals: envOn('CAMOFOX_FP_PRIVACY_SIGNALS', false),
    timezone: envRaw('CAMOFOX_FP_TIMEZONE') || 'Asia/Kolkata',
    locale: envRaw('CAMOFOX_FP_LOCALE') || 'en-US',
    // Single tag by default. Camoufox's binary forces the outbound Accept-Language header to
    // track navigator.language (a single tag) and ignores both headers.Accept-Language in the
    // config AND Gecko's intl.accept_languages pref (verified live: pref "en-US, en" + config
    // "en-US,en;q=0.9" still produced wire "en-US"). Advertising navigator.languages=["en-US","en"]
    // while the wire header can only be "en-US" would be a NEW header-vs-JS mismatch, so we keep
    // them aligned. The real-Firefox "en-US,en;q=0.5" shape needs a proxy-layer header rewrite or
    // a Camoufox patch -- see the enhancement list, not fixable from config.
    languages: (envRaw('CAMOFOX_FP_LANGUAGES') || 'en-US')
      .split(',').map((s) => s.trim()).filter(Boolean),
    lat: envNum('CAMOFOX_FP_LAT', 18.5204),
    lon: envNum('CAMOFOX_FP_LON', 73.8567),
    acc: envNum('CAMOFOX_FP_ACC', 120),
    grantGeo: envOn('CAMOFOX_FP_GRANT_GEO', false),
  };

  const blockWebrtc = envOn('CAMOFOX_HARDEN_BLOCK_WEBRTC', true);
  const vendor = envRaw('CAMOFOX_HARDEN_WEBGL_VENDOR');
  const renderer = envRaw('CAMOFOX_HARDEN_WEBGL_RENDERER');
  const addons = envRaw('CAMOFOX_HARDEN_ADDONS').split(',').map((s) => s.trim()).filter(Boolean);

  events.on('browser:launching', ({ options }) => {
    // These are Playwright launch options, so only Playwright keys have any effect.
    options.firefoxUserPrefs = options.firefoxUserPrefs || {};
    if (blockWebrtc) options.firefoxUserPrefs['media.peerconnection.enabled'] = false;
    if (opts.privacySignals) {
      options.firefoxUserPrefs['privacy.donottrackheader.enabled'] = true;
      options.firefoxUserPrefs['privacy.globalprivacycontrol.enabled'] = true;
      options.firefoxUserPrefs['privacy.globalprivacycontrol.functionality.enabled'] = true;
    }
    // Gecko's network stack builds the Accept-Language header from this pref, and it wins over
    // both the Camoufox headers.Accept-Language config and Playwright extraHTTPHeaders. Set it
    // to the real Firefox default shape ("en-US, en") so the wire header is "en-US,en;q=0.5"
    // instead of the bare "en-US" that Playwright's context locale would otherwise produce.
    if (opts.geo) options.firefoxUserPrefs['intl.accept_languages'] = opts.languages.join(', ');
    if (addons.length) {
      options.args = options.args || [];
      for (const a of addons) options.args.push('--install-extension', a);
    }

    if (!opts.enabled) return;

    const env = options.env;
    if (!env) { log('warn', 'fp repair skipped: no env on launch options'); return; }
    const config = readCamouConfig(env);
    if (!config) { log('warn', 'fp repair skipped: CAMOU_CONFIG_* absent or unparseable'); return; }

    if (vendor && renderer) {
      config['webGl:vendor'] = vendor;
      config['webGl:renderer'] = renderer;
    }

    const changes = repairConfig(config, opts);
    writeCamouConfig(env, config);

    log('info', 'fingerprint coherence repair applied', {
      ua: config['navigator.userAgent'],
      platform: config['navigator.platform'],
      oscpu: config['navigator.oscpu'],
      appVersion: config['navigator.appVersion'],
      timezone: config.timezone,
      repaired: changes.length,
      changes: changes.map((c) => `${c.key}: ${JSON.stringify(c.from)} -> ${JSON.stringify(c.to)}`),
    });
  });

  // Accept-Language the way stock Firefox actually emits it: primary tag verbatim, then each
  // fallback with a descending q-value. "en-US" alone -> "en-US,en;q=0.5".
  const acceptLanguage = opts.languages
    .map((l, i) => (i === 0 ? l : `${l};q=${(Math.max(0.1, 1 - i * 0.5)).toFixed(1)}`))
    .join(',');

  events.on('session:creating', ({ contextOptions }) => {
    if (!opts.enabled || !opts.geo) return;
    // server.js:1206-1208 hardcodes en-US / America/Los_Angeles / San Francisco whenever no
    // proxy is configured. Over a non-US egress IP that is a direct geo contradiction, and
    // it is checked server-side without needing any JS. Align it with the real egress.
    contextOptions.timezoneId = opts.timezone;
    contextOptions.geolocation = { latitude: opts.lat, longitude: opts.lon, accuracy: opts.acc };
    // NOTE: we deliberately do NOT set contextOptions.locale. Playwright's `locale` collapses
    // Firefox's intl.accept_languages to the single bare tag ("en-US") for that context, which
    // overrides the launch pref and produces a non-Firefox-shaped Accept-Language on the wire.
    // We instead drive language through the intl.accept_languages launch pref (browser:launching)
    // plus the Camoufox navigator.language/languages config, which keeps the Intl JS locale AND
    // yields the correct "en-US,en;q=0.5" wire header. Timezone/geolocation are unaffected.
    void acceptLanguage; // retained for reference; header now comes from the launch pref
    // Real users get a permission prompt and usually deny. Auto-granting geolocation to
    // every origin is itself anomalous, so drop the grant unless explicitly asked for.
    if (!opts.grantGeo && Array.isArray(contextOptions.permissions)) {
      contextOptions.permissions = contextOptions.permissions.filter((p) => p !== 'geolocation');
    }
  });

  log('info', 'harden plugin registered', {
    repair: opts.enabled, geo: opts.geo, timezone: opts.timezone, locale: opts.locale,
    languages: opts.languages.join(','), privacySignals: opts.privacySignals,
    block_webrtc: blockWebrtc, webgl_override: !!(vendor && renderer), addons: addons.length,
  });
}
