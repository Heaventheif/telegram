#!/usr/bin/env bun
"use strict";
/**
 * plugins/_js/youtube_worker.js
 * يُشغَّل عبر Bun من plugins/youtube.py — يحل رابط تحميل يوتيوب مباشر
 * عبر @vreden/youtube_scraper (لا يُنزّل الملف نفسه، فقط يرجع الرابط).
 *
 *   bun run youtube_worker.js probe <url>
 *   bun run youtube_worker.js download <video|audio> <quality> <url>
 *
 * الإخراج (آخر سطر stdout): {"status":true,...} أو {"status":false,"error":"..."}
 */

const yt = require("@vreden/youtube_scraper");

// جودات الصوت المدعومة فعلياً من الحزمة — نقرّب أي قيمة مطلوبة لأقربها
const AUDIO_QUALITIES = [92, 128, 256, 320];
function nearestAudioQuality(q) {
  q = Number(q);
  if (!Number.isFinite(q)) return 128;
  return AUDIO_QUALITIES.reduce((best, cur) =>
    Math.abs(cur - q) < Math.abs(best - q) ? cur : best
  );
}

function printJSON(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function probe(url) {
  const info = await yt.metadata(url);
  if (!info || info.status === false) {
    throw new Error((info && info.error) || "تعذّر جلب معلومات الفيديو عبر vreden");
  }
  printJSON({ status: true, title: info.title || "فيديو يوتيوب" });
}

async function download(kind, quality, url) {
  const isAudio = kind === "audio";
  const q = isAudio ? nearestAudioQuality(quality) : Number(quality);

  const res = isAudio ? await yt.ytmp3(url, q) : await yt.ytmp4(url, q);
  if (!res || res.status === false) {
    throw new Error((res && res.error) || "vreden: فشل التحويل");
  }

  const dl = res.download || {};
  if (!dl.status || !dl.url) {
    throw new Error("vreden: لا يوجد رابط تحميل في الاستجابة");
  }

  printJSON({
    status: true,
    url: dl.url,
    title: (res.metadata && res.metadata.title) || "يوتيوب",
  });
}

async function main() {
  const [, , mode, ...rest] = process.argv;
  try {
    if (mode === "probe") {
      const [url] = rest;
      if (!url) throw new Error("رابط مفقود");
      await probe(url);
    } else if (mode === "download") {
      const [kind, quality, url] = rest;
      if (!kind || !quality || !url) throw new Error("معطيات ناقصة (kind/quality/url)");
      await download(kind, quality, url);
    } else {
      throw new Error("وضع غير معروف: " + mode);
    }
  } catch (e) {
    printJSON({ status: false, error: String((e && e.message) || e) });
    process.exitCode = 1; // لا نستخدم process.exit() مباشرة بعد الكتابة — قد يقصّ الإخراج عند توجيهه إلى pipe (subprocess من بايثون)
  }
}

main();
