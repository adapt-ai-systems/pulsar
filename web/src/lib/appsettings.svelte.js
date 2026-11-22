/**
 * Shared app-wide settings loaded once from the daemon's /settings/{key} store.
 *
 * Keys (matching the backend app_settings schema):
 *   owner.name        string  — unset = "the user"
 *   owner.timezone    string  — IANA zone name; unset = browser zone
 *   hub.url           string  — base URL of the pages hub; unset = hub off
 *   metrics.url       string  — base URL of the metrics dashboard; unset = bar off
 *   guard.enabled     boolean — default true
 *   guard.extra_paths string[] — extra protected paths
 *   default_backup    object  — { model, effort? } for new pulses; unset = no backup
 */
import { getSetting, putSetting, deleteSetting } from './api.js';
import { setDefaultTimeZone } from './time.js';

export const SETTING_KEYS = {
  ownerName: 'owner.name',
  ownerTimezone: 'owner.timezone',
  hubUrl: 'hub.url',
  metricsUrl: 'metrics.url',
  guardEnabled: 'guard.enabled',
  guardExtraPaths: 'guard.extra_paths',
  defaultBackup: 'default_backup'
};

export const appSettings = $state({
  loaded: false,
  ownerName: '',
  ownerTimezone: '',
  hubUrl: '',
  metricsUrl: '',
  guardEnabled: true,
  guardExtraPaths: [],
  defaultBackup: null
});

/** Display name for UI strings that name the owner. */
export function ownerDisplayName(raw = appSettings.ownerName) {
  const s = typeof raw === 'string' ? raw.trim() : '';
  return s || 'the user';
}

/** Validate an IANA time zone string; returns trimmed zone or null. */
export function validTimeZone(raw) {
  const s = typeof raw === 'string' ? raw.trim() : '';
  if (!s) return null;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: s });
    return s;
  } catch {
    return null;
  }
}

/** Active IANA time zone (undefined = browser local zone). */
export function activeTimeZone(raw = appSettings.ownerTimezone) {
  return validTimeZone(raw) || undefined;
}

/** Normalize an optional base URL setting (trimmed, no trailing slashes). */
export function normalizeUrlSetting(raw) {
  if (typeof raw !== 'string') return '';
  return raw.trim().replace(/\/+$/, '');
}

/** Parse extra protected paths from a newline-delimited string or string array. */
export function parseExtraPaths(raw) {
  if (Array.isArray(raw)) {
    return raw.map((x) => String(x ?? '').trim()).filter(Boolean);
  }
  if (typeof raw !== 'string') return [];
  return raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

/** Normalize a default_backup value ({ model, effort? }) or null when unset. */
export function normalizeDefaultBackup(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const model = typeof raw.model === 'string' ? raw.model.trim() : '';
  if (!model) return null;
  const effort = typeof raw.effort === 'string' ? raw.effort.trim() : '';
  return effort ? { model, effort } : { model };
}

/** Whether the Pages Hub feature is configured and enabled. */
export function isHubEnabled() {
  return normalizeUrlSetting(appSettings.hubUrl).length > 0;
}

/** Whether the Metrics bar feature is configured and enabled. */
export function isMetricsEnabled() {
  return normalizeUrlSetting(appSettings.metricsUrl).length > 0;
}

let loadingPromise = null;

/**
 * Load all app-wide settings from the daemon once (or refresh when `force` is true).
 * Missing keys keep neutral defaults (hub/metrics off, guard on, browser zone, "the user", no default backup).
 */
export async function loadAppSettings({ force = false } = {}) {
  if (loadingPromise && !force) return loadingPromise;
  const run = (async () => {
    const [nameR, tzR, hubR, metricsR, guardR, pathsR, backupR] = await Promise.all([
      getSetting(SETTING_KEYS.ownerName),
      getSetting(SETTING_KEYS.ownerTimezone),
      getSetting(SETTING_KEYS.hubUrl),
      getSetting(SETTING_KEYS.metricsUrl),
      getSetting(SETTING_KEYS.guardEnabled),
      getSetting(SETTING_KEYS.guardExtraPaths),
      getSetting(SETTING_KEYS.defaultBackup)
    ]);

    if (nameR.ok) appSettings.ownerName = typeof nameR.value === 'string' ? nameR.value.trim() : '';
    if (tzR.ok) {
      appSettings.ownerTimezone = typeof tzR.value === 'string' ? tzR.value.trim() : '';
      setDefaultTimeZone(activeTimeZone(appSettings.ownerTimezone) ?? null);
    }
    if (hubR.ok) appSettings.hubUrl = normalizeUrlSetting(hubR.value);
    if (metricsR.ok) appSettings.metricsUrl = normalizeUrlSetting(metricsR.value);
    if (guardR.ok) appSettings.guardEnabled = guardR.value === null || guardR.value === undefined ? true : !!guardR.value;
    if (pathsR.ok) appSettings.guardExtraPaths = parseExtraPaths(pathsR.value);
    if (backupR.ok) appSettings.defaultBackup = normalizeDefaultBackup(backupR.value);
    appSettings.loaded = true;
    return appSettings;
  })();
  loadingPromise = run;
  try {
    return await run;
  } finally {
    if (loadingPromise === run) loadingPromise = null;
  }
}

/**
 * Persist all app-wide settings to /settings/{key} and update local reactive state.
 * Empty optional settings are deleted so the key is truly unset on the daemon.
 * @param {{
 *   ownerName: string,
 *   ownerTimezone: string,
 *   hubUrl: string,
 *   metricsUrl: string,
 *   guardEnabled: boolean,
 *   guardExtraPaths: string[],
 *   defaultBackup?: { model: string, effort?: string } | null
 * }} values
 * @returns {Promise<{ ok: boolean, error?: string }>}
 */
export async function saveAppSettings(values) {
  const ownerName = typeof values.ownerName === 'string' ? values.ownerName.trim() : '';
  const ownerTimezone = typeof values.ownerTimezone === 'string' ? values.ownerTimezone.trim() : '';
  const hubUrl = normalizeUrlSetting(values.hubUrl);
  const metricsUrl = normalizeUrlSetting(values.metricsUrl);
  const guardEnabled = !!values.guardEnabled;
  const guardExtraPaths = parseExtraPaths(values.guardExtraPaths);
  const defaultBackup = 'defaultBackup' in values
    ? normalizeDefaultBackup(values.defaultBackup)
    : appSettings.defaultBackup;

  if (ownerTimezone && !validTimeZone(ownerTimezone)) {
    return { ok: false, error: `Unknown time zone "${ownerTimezone}". Use an IANA name like America/New_York or UTC.` };
  }
  for (const [label, url] of [['Pages hub URL', hubUrl], ['Metrics bar URL', metricsUrl]]) {
    if (url) {
      try {
        const parsed = new URL(url);
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
          return { ok: false, error: `${label} must start with http:// or https://.` };
        }
      } catch {
        return { ok: false, error: `${label} must be a valid URL.` };
      }
    }
  }

  const writeOpt = (key, val) => (val ? putSetting(key, val) : deleteSetting(key));
  const results = await Promise.all([
    writeOpt(SETTING_KEYS.ownerName, ownerName),
    writeOpt(SETTING_KEYS.ownerTimezone, ownerTimezone),
    writeOpt(SETTING_KEYS.hubUrl, hubUrl),
    writeOpt(SETTING_KEYS.metricsUrl, metricsUrl),
    putSetting(SETTING_KEYS.guardEnabled, guardEnabled),
    putSetting(SETTING_KEYS.guardExtraPaths, guardExtraPaths),
    writeOpt(SETTING_KEYS.defaultBackup, defaultBackup)
  ]);

  const failed = results.find((r) => !r.ok);
  if (failed) return { ok: false, error: failed.error || 'failed to save settings' };

  appSettings.ownerName = ownerName;
  appSettings.ownerTimezone = ownerTimezone;
  appSettings.hubUrl = hubUrl;
  appSettings.metricsUrl = metricsUrl;
  appSettings.guardEnabled = guardEnabled;
  appSettings.guardExtraPaths = guardExtraPaths;
  appSettings.defaultBackup = defaultBackup;
  appSettings.loaded = true;
  setDefaultTimeZone(activeTimeZone(ownerTimezone) ?? null);
  return { ok: true };
}
