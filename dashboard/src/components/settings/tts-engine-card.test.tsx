/**
 * TtsEngineCard tests (issue #39).
 *
 * Covers:
 *  - Renders nothing when only "auto" is available (no real choice).
 *  - Renders nothing when fetch fails.
 *  - Renders selector when both engines available.
 *  - Save POSTs the selected engine.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@/test/test-utils";

import { TtsEngineCard } from "./tts-engine-card";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;
const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    error: (...args: unknown[]) => mockToastError(...args),
  },
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockFetch.mockReset();
  mockToastSuccess.mockReset();
  mockToastError.mockReset();
});

describe("TtsEngineCard", () => {
  it("renders nothing when only auto is available", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ available: ["auto"], default: "auto" }),
    );
    const { container } = render(<TtsEngineCard />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(
      container.querySelector('[data-testid="tts-engine-card"]'),
    ).toBeNull();
  });

  it("renders nothing when fetch fails", async () => {
    mockFetch.mockRejectedValueOnce(new Error("network down"));
    const { container } = render(<TtsEngineCard />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(
      container.querySelector('[data-testid="tts-engine-card"]'),
    ).toBeNull();
  });

  it("renders a selector when both engines are available", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        available: ["auto", "piper", "kokoro"],
        default: "piper",
      }),
    );
    render(<TtsEngineCard />);
    expect(await screen.findByTestId("tts-engine-card")).toBeInTheDocument();
    const select = screen.getByTestId("tts-engine-select");
    expect(select).toBeInTheDocument();
    // All three options surface — operator's mental model includes
    // "auto" as a deliberate fallback choice (not just the default).
    expect(select.querySelectorAll("option")).toHaveLength(3);
  });

  it("POSTs the selection on save", async () => {
    mockFetch
      .mockResolvedValueOnce(
        jsonResponse({
          available: ["auto", "piper", "kokoro"],
          default: "piper",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    render(<TtsEngineCard />);
    const select = (await screen.findByTestId(
      "tts-engine-select",
    )) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "kokoro" } });

    const saveButton = screen.getByText(/save/i);
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalled();
    });

    // Last call is the POST; assert body shape.
    const lastCall = mockFetch.mock.calls.at(-1)!;
    const init = lastCall[1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ tts_engine: "kokoro" });
  });
});
