/**
 * Locale completeness — key-parity invariant across en / pt-BR / es.
 *
 * Mission C4 §T1.13 §9.1 row "Locale completeness" — extends the
 * dashboard's i18n discipline to guarantee that the new ``degraded.*``
 * namespace (and every existing namespace) has the SAME key tree in
 * all 3 locales. Without this guard, a missing pt-BR translation
 * silently falls back to English copy on the operator's screen even
 * though their mind language is ``pt`` — exactly the kind of silent
 * regression the v0.43.1 "decorative daemon" gap shipped.
 *
 * Generalizes across every voice.json key, but the test was created
 * to cover the C4 ``degraded.*`` additions specifically; future
 * namespaces inherit the check for free.
 */
import { describe, expect, it } from "vitest";

import enVoice from "@/locales/en/voice.json";
import ptVoice from "@/locales/pt-BR/voice.json";
import esVoice from "@/locales/es/voice.json";

type AnyJson = Record<string, unknown>;

function collectKeyPaths(
  obj: AnyJson,
  prefix: string,
  out: Set<string>,
): void {
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      collectKeyPaths(v as AnyJson, path, out);
    } else {
      out.add(path);
    }
  }
}

function pathSet(obj: AnyJson): Set<string> {
  const out = new Set<string>();
  collectKeyPaths(obj, "", out);
  return out;
}

describe("Locale completeness — voice namespace", () => {
  it("en + pt-BR + es have identical key paths across the voice namespace", () => {
    const en = pathSet(enVoice as AnyJson);
    const pt = pathSet(ptVoice as AnyJson);
    const es = pathSet(esVoice as AnyJson);

    const missingInPt = [...en].filter((k) => !pt.has(k));
    const missingInEs = [...en].filter((k) => !es.has(k));
    const extraInPt = [...pt].filter((k) => !en.has(k));
    const extraInEs = [...es].filter((k) => !en.has(k));

    expect(
      { missingInPt, missingInEs, extraInPt, extraInEs },
    ).toEqual({ missingInPt: [], missingInEs: [], extraInPt: [], extraInEs: [] });
  });

  it("Mission C4 §T1.9 — all degraded.* keys present in 3 locales", () => {
    const en = pathSet(enVoice as AnyJson);
    const pt = pathSet(ptVoice as AnyJson);
    const es = pathSet(esVoice as AnyJson);

    const requiredKeys = [
      "degraded.composite.title_one",
      "degraded.composite.title_other",
      "degraded.composite.ack",
      "degraded.voice.ladderExhausted.title",
      "degraded.voice.ladderExhausted.body",
      "degraded.voice.ladderExhausted.viewHistory",
      "degraded.voice.ladderExhausted.reconnectUsb",
      "degraded.llm.noProvider.title",
      "degraded.llm.noProvider.body",
      "degraded.llm.noProvider.installOllama",
      "degraded.llm.noProvider.openSettings",
      "degraded.stt.languageCoerced.title",
      "degraded.stt.languageCoerced.body",
      "degraded.stt.languageCoerced.switchToEnglish",
      "degraded.stt.languageCoerced.learnMore",
    ];
    for (const key of requiredKeys) {
      expect(en.has(key), `EN missing ${key}`).toBe(true);
      expect(pt.has(key), `pt-BR missing ${key}`).toBe(true);
      expect(es.has(key), `ES missing ${key}`).toBe(true);
    }
  });

  it("Mission C5 §T3.6 — all degraded.dashboard.* keys present in 3 locales", () => {
    const en = pathSet(enVoice as AnyJson);
    const pt = pathSet(ptVoice as AnyJson);
    const es = pathSet(esVoice as AnyJson);

    const requiredKeys = [
      "degraded.dashboard.bundle_partial.title",
      "degraded.dashboard.bundle_partial.partial.body",
      "degraded.dashboard.bundle_missing.title",
      "degraded.dashboard.bundle_missing.index_html_missing.body",
      "degraded.dashboard.bundle_missing.static_dir_missing.body",
      "degraded.dashboard.bundle_missing.legacy_index_html_no_assets.body",
      "degraded.dashboard.reinstall",
      "degraded.dashboard.runDoctor",
    ];
    for (const key of requiredKeys) {
      expect(en.has(key), `EN missing ${key}`).toBe(true);
      expect(pt.has(key), `pt-BR missing ${key}`).toBe(true);
      expect(es.has(key), `ES missing ${key}`).toBe(true);
    }
  });
});
