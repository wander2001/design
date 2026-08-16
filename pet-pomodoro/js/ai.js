// 前端调 AI 生成的薄封装。真正的 key 在后端（server/server.mjs），这里只发照片。

const STATUS_URL = './api/status';
const GENERATE_URL = './api/generate';

let statusCache = null;

/** 后端有没有配好 AI；直接开静态文件时会失败，返回 unavailable */
export async function fetchAiStatus() {
  if (statusCache) return statusCache;
  try {
    const res = await fetch(STATUS_URL, { headers: { accept: 'application/json' } });
    if (!res.ok) throw new Error(String(res.status));
    statusCache = await res.json();
  } catch {
    statusCache = { provider: null, label: null, ready: false, offline: true, styles: [] };
  }
  return statusCache;
}

/**
 * 把取好景的照片发给后端生成形象。
 * @returns {Promise<string>} 生成结果的 dataURL
 */
export async function generateCharacter({ image, style, species, extra, signal }) {
  const res = await fetch(GENERATE_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ image, style, species, extra }),
    signal,
  });

  let payload = null;
  try {
    payload = await res.json();
  } catch {
    throw new Error(`服务返回异常（HTTP ${res.status}）`);
  }
  if (!res.ok) throw new Error(payload?.error || `生成失败（HTTP ${res.status}）`);
  if (!payload?.image) throw new Error('服务没有返回图片');
  return payload.image;
}
