/**
 * Per-device localStorage helpers: per-device settings overrides (e.g. theme).
 */

export const DEVICE_PREFIX = 'pulsar-device.';

/**
 * This device's own value for a settings key, or undefined if never set here.
 * @param {string} key
 */
export function getDeviceSetting(key) {
  try {
    const raw = localStorage.getItem(DEVICE_PREFIX + key);
    return raw === null ? undefined : JSON.parse(raw);
  } catch {
    return undefined;
  }
}

/**
 * Remember this device's value for a settings key (never touches the shared KV).
 * @param {string} key
 * @param {any} value
 */
export function putDeviceSetting(key, value) {
  try {
    localStorage.setItem(DEVICE_PREFIX + key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}
