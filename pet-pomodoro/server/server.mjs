// AI 生成的后端代理：把照片转发给图像模型，顺便把静态页面也托管了。
//
// 为什么需要后端：API key 绝对不能放前端，否则谁打开网页都能偷走。
// 这个服务只做三件事：托管静态文件、转发生成请求、藏好 key。零依赖，Node 18+ 直接跑。
//
// 用哪家模型由环境变量决定：
//   PET_AI_PROVIDER=gemini  GEMINI_API_KEY=...     （推荐，图生图保人物特征最好）
//   PET_AI_PROVIDER=openai  OPENAI_API_KEY=...
//   PET_AI_PROVIDER=mock                            （不调用任何服务，用来跑通流程）
// 不设 PET_AI_PROVIDER 时按 key 自动挑一个，都没有就退化成 mock。

import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(fileURLToPath(new URL('../', import.meta.url)));
const PORT = Number(process.env.PORT || 8787);
const MAX_UPLOAD = 12 * 1024 * 1024; // 12MB，够 768px 的照片了

// —— 画风：给模型看的提示词 ——
// 用户想要的是「温馨可爱、用来陪伴」，所以每条提示词都强调暖色、柔和、治愈，
// 同时反复要求保留原照片的特征——不然生成出来就不是自己的猫了。
const AI_STYLES = {
  cozy: {
    label: '温馨可爱',
    prompt:
      'a warm, cozy chibi mascot character, soft rounded shapes, gentle pastel palette with warm cream and honey tones, ' +
      'soft ambient lighting, big friendly eyes, subtle rosy blush on the cheeks, clean thick outlines, ' +
      'high-quality children book illustration style, comforting and huggable',
  },
  plush: {
    label: '毛绒手办',
    prompt:
      'an adorable soft plush toy version, felt and fuzzy fabric texture, visible gentle stitching, ' +
      'stubby rounded proportions, studio product photo on a plain warm background, soft diffused light, ' +
      'squishy and huggable, collectible vinyl-plush hybrid look',
  },
  watercolor: {
    label: '绘本水彩',
    prompt:
      'a gentle watercolor storybook illustration, soft washes of warm color, visible paper grain, ' +
      'delicate ink linework, dreamy and tender mood, hand-painted picture book aesthetic',
  },
  sticker: {
    label: '简笔贴纸',
    prompt:
      'a cute flat vector sticker, bold clean outlines, simple cheerful shapes, limited warm palette, ' +
      'minimal shading, messaging-app sticker aesthetic, instantly readable at small size',
  },
};

const SUBJECT_HINT = {
  cat: 'the cat in the photo',
  dog: 'the dog in the photo',
  human: 'the person in the photo',
};

/** 拼最终提示词：画风 + 主体 + 一堆「别乱改」的约束 */
function buildPrompt({ style, species, extra }) {
  const preset = AI_STYLES[style] || AI_STYLES.cozy;
  const subject = SUBJECT_HINT[species] || 'the subject in the photo';
  return [
    `Redraw ${subject} as ${preset.prompt}.`,
    'Keep the recognizable identity: same fur or hair color and markings, same face shape, same expression, same accessories (glasses, collar, hat) if present.',
    'Front-facing head-and-shoulders portrait, centered, looking at the viewer, friendly and calm.',
    'Plain flat background in a single soft warm color, no scenery, no text, no watermark, no frame, no extra characters.',
    extra ? `Extra request from the user: ${extra}` : '',
    'Square image.',
  ]
    .filter(Boolean)
    .join(' ');
}

// —— provider 适配层：每家一个函数，输入输出统一 ——
// 想接别家（豆包 / 通义 / Replicate / fal）就照着加一个函数，再登记到 PROVIDERS 里。

/** Google Gemini 图像模型（nano banana）：原生图生图，保特征效果最好 */
async function generateWithGemini({ imageBase64, mimeType, prompt }) {
  const key = process.env.GEMINI_API_KEY;
  const model = process.env.GEMINI_IMAGE_MODEL || 'gemini-2.5-flash-image';
  const res = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-goog-api-key': key },
      body: JSON.stringify({
        contents: [
          { role: 'user', parts: [{ text: prompt }, { inline_data: { mime_type: mimeType, data: imageBase64 } }] },
        ],
      }),
    }
  );

  const json = await readJson(res, 'Gemini');
  const parts = json?.candidates?.[0]?.content?.parts || [];
  const image = parts.find((p) => p.inline_data?.data || p.inlineData?.data);
  if (!image) {
    const text = parts.find((p) => p.text)?.text;
    throw new HttpError(502, text ? `模型没有返回图片：${text}` : '模型没有返回图片');
  }
  const inline = image.inline_data || image.inlineData;
  return { base64: inline.data, mimeType: inline.mime_type || inline.mimeType || 'image/png' };
}

/** OpenAI 图像编辑接口 */
async function generateWithOpenAI({ imageBuffer, mimeType, prompt }) {
  const key = process.env.OPENAI_API_KEY;
  const model = process.env.OPENAI_IMAGE_MODEL || 'gpt-image-1';

  const form = new FormData();
  form.append('model', model);
  form.append('prompt', prompt);
  form.append('size', '1024x1024');
  form.append('n', '1');
  form.append('image', new Blob([imageBuffer], { type: mimeType }), 'photo.png');

  const res = await fetch('https://api.openai.com/v1/images/edits', {
    method: 'POST',
    headers: { authorization: `Bearer ${key}` },
    body: form,
  });

  const json = await readJson(res, 'OpenAI');
  const b64 = json?.data?.[0]?.b64_json;
  if (!b64) throw new HttpError(502, '模型没有返回图片');
  return { base64: b64, mimeType: 'image/png' };
}

/** 不调用任何外部服务，原样返回照片——用来验证整条链路是否打通 */
async function generateWithMock({ imageBase64, mimeType }) {
  return { base64: imageBase64, mimeType };
}

const PROVIDERS = {
  gemini: { label: 'Google Gemini', envKey: 'GEMINI_API_KEY', run: generateWithGemini },
  openai: { label: 'OpenAI', envKey: 'OPENAI_API_KEY', run: generateWithOpenAI },
  mock: { label: '本地回环（未接 AI）', envKey: null, run: generateWithMock },
};

function resolveProvider() {
  const explicit = process.env.PET_AI_PROVIDER;
  if (explicit) {
    const p = PROVIDERS[explicit];
    if (!p) throw new Error(`未知的 PET_AI_PROVIDER：${explicit}`);
    return explicit;
  }
  if (process.env.GEMINI_API_KEY) return 'gemini';
  if (process.env.OPENAI_API_KEY) return 'openai';
  return 'mock';
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function readJson(res, who) {
  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    throw new HttpError(502, `${who} 返回了非 JSON 内容（HTTP ${res.status}）`);
  }
  if (!res.ok) {
    const msg = json?.error?.message || json?.message || `HTTP ${res.status}`;
    // 把上游的状态码透传出去，前端好区分「key 不对」和「服务挂了」
    throw new HttpError(res.status === 401 || res.status === 403 ? 502 : 502, `${who}：${msg}`);
  }
  return json;
}

// —— HTTP ——

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
};

function send(res, status, body, headers = {}) {
  res.writeHead(status, { 'cache-control': 'no-store', ...headers });
  res.end(body);
}

function sendJson(res, status, data) {
  send(res, status, JSON.stringify(data), { 'content-type': 'application/json; charset=utf-8' });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_UPLOAD) {
        reject(new HttpError(413, '图片太大了，请压到 12MB 以内'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

/** 解析前端传来的 dataURL */
function parseDataUrl(dataUrl) {
  const m = /^data:([^;,]+);base64,(.+)$/s.exec(dataUrl || '');
  if (!m) throw new HttpError(400, '图片格式不对，需要 base64 的 dataURL');
  return { mimeType: m[1], base64: m[2] };
}

async function handleGenerate(req, res) {
  const raw = await readBody(req);
  let payload;
  try {
    payload = JSON.parse(raw.toString('utf8'));
  } catch {
    throw new HttpError(400, '请求体不是合法 JSON');
  }

  const { mimeType, base64 } = parseDataUrl(payload.image);
  const prompt = buildPrompt({
    style: payload.style,
    species: payload.species,
    extra: typeof payload.extra === 'string' ? payload.extra.slice(0, 300) : '',
  });

  const providerName = resolveProvider();
  const provider = PROVIDERS[providerName];
  const started = Date.now();

  const out = await provider.run({
    imageBase64: base64,
    imageBuffer: Buffer.from(base64, 'base64'),
    mimeType,
    prompt,
  });

  console.log(`[generate] ${providerName} ${payload.style || 'cozy'} ${Date.now() - started}ms`);
  sendJson(res, 200, {
    image: `data:${out.mimeType};base64,${out.base64}`,
    provider: providerName,
    prompt,
  });
}

async function serveStatic(req, res, pathname) {
  const rel = normalize(decodeURIComponent(pathname)).replace(/^(\.\.[/\\])+/, '');
  // 后端代码不属于网页资源，不对外暴露
  if (/^[/\\](server|node_modules)[/\\]/.test(rel)) throw new HttpError(403, '不可访问');
  let filePath = join(ROOT, rel === '/' ? 'index.html' : rel);
  if (!filePath.startsWith(ROOT)) throw new HttpError(403, '越权访问');

  let info;
  try {
    info = await stat(filePath);
  } catch {
    throw new HttpError(404, '没有这个文件');
  }
  if (info.isDirectory()) {
    filePath = join(filePath, 'index.html');
  }
  const body = await readFile(filePath);
  send(res, 200, body, { 'content-type': MIME[extname(filePath)] || 'application/octet-stream' });
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  try {
    if (url.pathname === '/api/status') {
      const name = resolveProvider();
      sendJson(res, 200, {
        provider: name,
        label: PROVIDERS[name].label,
        ready: name !== 'mock',
        styles: Object.entries(AI_STYLES).map(([id, s]) => ({ id, label: s.label })),
      });
      return;
    }
    if (url.pathname === '/api/generate') {
      if (req.method !== 'POST') throw new HttpError(405, '只接受 POST');
      await handleGenerate(req, res);
      return;
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') throw new HttpError(405, '不支持的方法');
    await serveStatic(req, res, url.pathname);
  } catch (err) {
    const status = err instanceof HttpError ? err.status : 500;
    if (status >= 500) console.error('[error]', err);
    if (url.pathname.startsWith('/api/')) sendJson(res, status, { error: err.message || '服务器错误' });
    else send(res, status, err.message || '服务器错误', { 'content-type': 'text/plain; charset=utf-8' });
  }
});

server.listen(PORT, () => {
  const name = resolveProvider();
  console.log(`番茄伙伴 → http://localhost:${PORT}`);
  console.log(`AI 生成：${PROVIDERS[name].label}${name === 'mock' ? '（设置 GEMINI_API_KEY 或 OPENAI_API_KEY 后自动启用）' : ''}`);
});
