#!/usr/bin/env bun
"use strict";
/**
 * plugins/_js/tiktok_worker.js
 * يُشغَّل عبر Bun من plugins/tiktok.py — يحل رابط فيديو تيك توك مباشر عبر
 * مزودين احتياطيين (TikWM ثم TikMate) مع مهلة وإعادة محاولة تصاعدية
 * (Exponential Backoff)، بديلاً عن yt-dlp الذي بات يتعطل كثيراً على تيك توك.
 * مأخوذ من نفس منطق TikTok.js الأصلي (fetchWithTimeout/API_PROVIDERS) لكن
 * كـ CLI يرجع النتيجة مرة واحدة بدل خادم HTTP دائم (Bun.serve).
 *
 *   bun run tiktok_worker.js resolve <url>
 *
 * الإخراج (آخر سطر stdout): {"status":true,"url":"...","title":"..."}
 *                        أو {"status":false,"error":"..."}
 */

const TIMEOUT_MS          = 15000;
const MAX_RETRIES         = 3;
const RETRY_STATUS_CODES  = [429, 502, 503, 504];
const INITIAL_BACKOFF_MS  = 500;

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

const API_PROVIDERS = [
  {
    name: "TikWM",
    buildUrl: (url) => `https://tikwm.com/api/?url=${encodeURIComponent(url)}`,
    extractor: (json) => json?.data?.play,
    titleExtractor: (json) => json?.data?.title,
  },
  {
    name: "TikMate",
    buildUrl: (url) => `https://www.tikmate.cc/api/url?url=${encodeURIComponent(url)}`,
    extractor: (json) => json?.url || json?.video_url,
    titleExtractor: (json) => json?.title,
  },
];

function printJSON(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function fetchWithTimeout(url, options = {}, retries = MAX_RETRIES, backoff = INITIAL_BACKOFF_MS) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    clearTimeout(timeout);
    if (!res.ok && RETRY_STATUS_CODES.includes(res.status) && retries > 0) {
      await Bun.sleep(backoff);
      return fetchWithTimeout(url, options, retries - 1, Math.min(backoff * 2, 10000));
    }
    return res;
  } catch (error) {
    clearTimeout(timeout);
    if (retries > 0 && (error.name === "AbortError" || String(error.message || "").includes("timeout"))) {
      await Bun.sleep(backoff);
      return fetchWithTimeout(url, options, retries - 1, Math.min(backoff * 2, 10000));
    }
    throw error;
  }
}

function validateTikTokUrl(urlString) {
  let url;
  try {
    url = new URL(urlString);
  } catch {
    throw new Error("الرابط غير صالح (تأكد من صيغته)");
  }
  const validHosts = ["www.tiktok.com", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com"];
  if (!validHosts.includes(url.hostname.toLowerCase())) {
    throw new Error("الرابط يجب أن يكون من TikTok");
  }
}

async function resolve(tiktokUrl) {
  validateTikTokUrl(tiktokUrl);
  let lastError = null;

  for (const provider of API_PROVIDERS) {
    try {
      const res = await fetchWithTimeout(provider.buildUrl(tiktokUrl), { headers: { "User-Agent": UA } });
      if (!res.ok) { lastError = new Error(`${provider.name}: HTTP ${res.status}`); continue; }

      const json = await res.json();
      if (!json || typeof json !== "object") { lastError = new Error(`${provider.name}: JSON غير صالح`); continue; }

      const videoUrl = provider.extractor(json);
      if (videoUrl && typeof videoUrl === "string" && videoUrl.startsWith("http")) {
        const title = (provider.titleExtractor && provider.titleExtractor(json)) || "فيديو تيك توك";
        printJSON({ status: true, url: videoUrl, title });
        return;
      }
      lastError = new Error(`${provider.name}: لم يُعد رابطاً صالحاً`);
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(`تعذر استخراج رابط الفيديو من جميع المزودين. آخر خطأ: ${lastError?.message || "غير معروف"}`);
}

async function main() {
  const [, , mode, url] = process.argv;
  try {
    if (mode !== "resolve" || !url) throw new Error("الاستخدام: resolve <url>");
    await resolve(url);
  } catch (e) {
    printJSON({ status: false, error: String((e && e.message) || e) });
    process.exitCode = 1;
  }
}

main();
