/**
 * Mission LIVE-2 Phase 4 — companion-language STT-fallback disclosure.
 *
 * Policy: an STT-unsupported companion language (e.g. Português) is NEVER
 * blocked — it drives the LLM conversation + TTS voice. The picker must:
 *   1. keep offering Portuguese (and other STT-unsupported languages), and
 *   2. truthfully disclose that speech recognition will fall back to
 *      English when such a language is chosen, sourcing the supported-STT
 *      set from the backend SSoT via /api/voice/voices (no hardcoded copy).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import "@/lib/i18n";
import { PersonalityStep } from "./PersonalityStep";

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
  isAbortError: (err: unknown) =>
    err instanceof Error && err.name === "AbortError",
}));

// Mirrors the /api/voice/voices payload. stt_supported_languages is the
// Moonshine set (no Portuguese); supported_languages is the Kokoro/TTS set
// (includes pt-br) — the two intentionally differ.
const CATALOG = {
  supported_languages: ["en-us", "es", "ja", "pt-br", "zh"],
  by_language: {},
  recommended_per_language: {},
  stt_supported_languages: ["ar", "en", "es", "ja", "ko", "uk", "vi", "zh"],
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockResolvedValue(CATALOG);
  mockPost.mockResolvedValue({});
});

function renderStep() {
  return render(
    <PersonalityStep mindName="Aria" onConfigured={vi.fn()} onSkip={vi.fn()} />,
  );
}

describe("PersonalityStep — STT fallback disclosure", () => {
  it("offers Portuguese as a selectable companion language", () => {
    renderStep();
    const option = screen.getByRole("option", { name: "Português" });
    expect(option).toBeInTheDocument();
    expect((option as HTMLOptionElement).disabled).toBe(false);
  });

  it("does not show the disclosure for the default STT-supported language", async () => {
    renderStep();
    // Default language derives from navigator.language (en-US → en), which
    // IS STT-supported. Wait for the catalog fetch to resolve first.
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(
      screen.queryByTestId("stt-fallback-disclosure"),
    ).not.toBeInTheDocument();
  });

  it("discloses the English STT fallback when Portuguese is chosen", async () => {
    renderStep();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "pt" },
    });

    await waitFor(() => {
      expect(
        screen.getByTestId("stt-fallback-disclosure"),
      ).toBeInTheDocument();
    });
    // The notice names the chosen language and is truthful about the
    // English speech-recognition fallback (scoped to the disclosure, since
    // "Português" also appears in the <option> list).
    const disclosure = screen.getByTestId("stt-fallback-disclosure");
    expect(within(disclosure).getAllByText(/Português/).length).toBeGreaterThan(0);
    expect(within(disclosure).getByText(/English/)).toBeInTheDocument();
  });

  it("does not show the disclosure for an STT-supported non-default language", async () => {
    renderStep();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "es" },
    });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(
      screen.queryByTestId("stt-fallback-disclosure"),
    ).not.toBeInTheDocument();
  });
});
