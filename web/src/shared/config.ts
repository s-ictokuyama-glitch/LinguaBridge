/**
 * GAS Web アプリ（中継）の URL。
 * relay-gas/Code.gs をデプロイしたあと、発行された /exec URL をここに設定する。
 * 例: 'https://script.google.com/macros/s/XXXXXXXX/exec'
 */
export const DEFAULT_RELAY_URL = 'https://script.google.com/macros/s/AKfycbx8CN7IQHTdQ4LW2ajWcT56y9FGLbmvP8uQkSOk0wIj60vnotFGeMRpmVBrIT997Y6S/exec';

const STORAGE_KEY = 'lb:relayUrl';

/**
 * 中継 URL の解決。開発時は `?relay=<url>` で上書きでき、
 * 一度指定するとページ遷移後も localStorage 経由で引き継がれる。
 */
export function getRelayUrl(): string {
  const fromQuery = new URLSearchParams(location.search).get('relay');
  if (fromQuery) {
    localStorage.setItem(STORAGE_KEY, fromQuery);
    return fromQuery;
  }
  return localStorage.getItem(STORAGE_KEY) ?? DEFAULT_RELAY_URL;
}

/** 中継 URL が既定値から上書きされている場合、その URL。なければ null */
export function getRelayOverride(): string | null {
  const url = getRelayUrl();
  return url && url !== DEFAULT_RELAY_URL ? url : null;
}
