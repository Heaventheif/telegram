#!/usr/bin/env bun
"use strict";
/**
 * plugins/_js/transcribe_worker.js
 * يُشغَّل عبر Bun من plugins/media_tools.py — يفرّغ صوت/فيديو نصياً عبر
 * Groq Whisper (groq-sdk) مع توقيت كل مقطع، ليبني بايثون منها ملف VTT.
 *
 *   bun run transcribe_worker.js <filePath>
 *
 * الإخراج (آخر سطر stdout): {"status":true,"segments":[{text,start,end}]}
 *                        أو {"status":false,"error":"..."}
 * يتطلب متغير البيئة GROQ_API_KEY.
 */

const Groq = require("groq-sdk");
const fs = require("fs");

function printJSON(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function main() {
  const [, , filePath] = process.argv;
  try {
    if (!filePath) throw new Error("مسار ملف مفقود");
    if (!fs.existsSync(filePath)) throw new Error("الملف غير موجود: " + filePath);
    const apiKey = process.env.GROQ_API_KEY;
    if (!apiKey) throw new Error("GROQ_API_KEY غير مضبوط في البيئة");

    const groq = new Groq({ apiKey });
    const transcription = await groq.audio.transcriptions.create({
      file: fs.createReadStream(filePath),
      model: "whisper-large-v3",
      response_format: "verbose_json",
    });

    const segments = (transcription.segments || [])
      .map((s) => ({ text: (s.text || "").trim(), start: s.start, end: s.end }))
      .filter((s) => s.text);

    if (!segments.length) throw new Error("لم يُرجع Groq أي مقاطع نصية");
    printJSON({ status: true, segments });
  } catch (e) {
    printJSON({ status: false, error: String((e && e.message) || e) });
    process.exitCode = 1;
  }
}

main();
