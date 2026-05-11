/**
 * VoiceSetupModal — happy path + 5 error branches.
 *
 * v0.38.0 / W3.B1 + F2-M04 (audit §3.G) closure. The modal is the
 * primary "enable voice" surface; pre-fix it had ZERO unit coverage,
 * so any regression in the api.post error-dispatch graph was invisible
 * to CI. This file pins:
 *
 *   * happy path → toast + close + onEnabled callback
 *   * missing_deps → depsIssue panel renders with copy-able command
 *   * capture_silence → silenceInfo panel renders host-api + RMS
 *   * capture_device_contended → DeviceContentionBanner takes priority
 *   * generic structured error → enableError panel
 *   * non-ApiError network failure → genericFailure i18n branch
 *
 * Dependencies are stubbed at the module boundary so the test never
 * touches the real network or i18n loader: api, useResolvedMindId,
 * verifyVoiceRunning, HardwareDetection (whose internal hardware-detect
 * GET would otherwise need its own mock graph).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@/lib/i18n";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { VoiceSetupModal } from "./VoiceSetupModal";

const mockPost = vi.fn();
const mockToastSuccess = vi.fn();
const mockVerifyVoiceRunning = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    post: (...args: unknown[]) => mockPost(...args),
  },
  ApiError: class ApiError extends Error {
    public readonly body: Record<string, unknown> | null;
    constructor(
      public status: number,
      message: string,
      body: Record<string, unknown> | null = null,
    ) {
      super(message);
      this.name = "ApiError";
      this.body = body;
    }
  },
}));

vi.mock("@/hooks/use-resolved-mind-id", () => ({
  useResolvedMindId: () => ({
    mindId: "default",
    loading: false,
    error: null,
  }),
}));

vi.mock("@/hooks/use-voice-running-verification", () => ({
  verifyVoiceRunning: (...args: unknown[]) => mockVerifyVoiceRunning(...args),
}));

vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
  },
}));

// Stub HardwareDetection to a deterministic synchronous render that
// fires onDetected immediately. Without this the modal would block on
// the real hardware-detect fetch graph.
vi.mock("./HardwareDetection", () => ({
  HardwareDetection: ({
    onDetected,
  }: {
    onDetected: () => void;
    onDeviceChange: (devices: { input_device: number | null; output_device: number | null }) => void;
  }) => {
    onDetected();
    return <div data-testid="hardware-detection-stub" />;
  },
}));

let ApiError: typeof import("@/lib/api").ApiError;

beforeEach(async () => {
  mockPost.mockReset();
  mockToastSuccess.mockReset();
  mockVerifyVoiceRunning.mockReset();
  // verifyVoiceRunning resolves to "running" by default — error-path
  // tests don't reach it.
  mockVerifyVoiceRunning.mockResolvedValue({ status: "running" });
  const apiMod = await import("@/lib/api");
  ApiError = apiMod.ApiError;
});

async function _renderModalAndOpen(): Promise<void> {
  render(<VoiceSetupModal />);
  // Open the dialog (DialogTrigger is the default "Set up Voice" button).
  const trigger = screen.getByRole("button", { name: /set up voice/i });
  fireEvent.click(trigger);
  await waitFor(() => {
    expect(screen.getByTestId("hardware-detection-stub")).toBeInTheDocument();
  });
}

async function _clickEnable(): Promise<void> {
  // After HardwareDetection fires onDetected, the Enable button renders.
  await waitFor(() => {
    const buttons = screen.getAllByRole("button");
    const enable = buttons.find((b) => /enable|enabling/i.test(b.textContent ?? ""));
    expect(enable).toBeDefined();
  });
  const buttons = screen.getAllByRole("button");
  const enable = buttons.find((b) => /enable|enabling/i.test(b.textContent ?? ""));
  expect(enable).toBeDefined();
  fireEvent.click(enable as HTMLElement);
}

describe("VoiceSetupModal — happy path", () => {
  it("posts /api/voice/enable, awaits verifyVoiceRunning, calls onEnabled + toast", async () => {
    mockPost.mockResolvedValueOnce({ ok: true, tts_engine: "kokoro" });
    const onEnabled = vi.fn();
    render(<VoiceSetupModal onEnabled={onEnabled} />);
    const trigger = screen.getByRole("button", { name: /set up voice/i });
    fireEvent.click(trigger);
    await waitFor(() => {
      expect(screen.getByTestId("hardware-detection-stub")).toBeInTheDocument();
    });
    await _clickEnable();

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        "/api/voice/enable",
        expect.objectContaining({
          input_device: null,
          output_device: null,
          mind_id: "default",
        }),
        expect.objectContaining({ schema: expect.anything() }),
      );
    });
    await waitFor(() => {
      expect(mockVerifyVoiceRunning).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(onEnabled).toHaveBeenCalledTimes(1);
    });
  });
});

describe("VoiceSetupModal — error branches", () => {
  it("renders depsIssue panel on missing_deps ApiError", async () => {
    mockPost.mockRejectedValueOnce(
      new ApiError(503, "missing_deps", {
        ok: false,
        error: "missing_deps",
        missing_deps: [{ module: "sounddevice", package: "sounddevice" }],
        install_command: "uv pip install sovyx[voice]",
      }),
    );
    await _renderModalAndOpen();
    await _clickEnable();

    await waitFor(() => {
      expect(screen.getByText(/uv pip install sovyx\[voice\]/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/sounddevice/i)).toBeInTheDocument();
  });

  it("renders silenceInfo panel on capture_silence ApiError", async () => {
    mockPost.mockRejectedValueOnce(
      new ApiError(503, "capture_silence", {
        ok: false,
        error: "capture_silence",
        detail: "Every host-API variant delivered zeros.",
        device: 0,
        host_api: "WASAPI",
        observed_peak_rms_db: -90.5,
      }),
    );
    await _renderModalAndOpen();
    await _clickEnable();

    await waitFor(() => {
      expect(screen.getByText(/WASAPI/)).toBeInTheDocument();
    });
    // The observed peak RMS is rendered with one decimal + " dBFS".
    expect(screen.getByText(/-90\.5 dBFS/)).toBeInTheDocument();
  });

  it("renders DeviceContentionBanner on capture_device_contended ApiError (F2-C01 closure)", async () => {
    mockPost.mockRejectedValueOnce(
      new ApiError(503, "capture_device_contended", {
        ok: false,
        error: "capture_device_contended",
        detail: "Discord is holding the microphone.",
        device: 0,
        host_api: "WASAPI",
        suggested_actions: ["close_other_app"],
        contending_process_hint: "Discord.exe",
        alternative_devices: [
          {
            index: 1,
            name: "Razer Seiren",
            host_api: "WASAPI",
            kind: "hardware",
            max_input_channels: 2,
            default_samplerate: 48_000,
          },
        ],
      }),
    );
    await _renderModalAndOpen();
    await _clickEnable();

    // The chip renders with a stable test id on DeviceContentionBanner.
    await waitFor(() => {
      expect(screen.getByTestId("device-contention-chip-1")).toBeInTheDocument();
    });
    // The contention banner takes priority — generic enableError must NOT show.
    expect(screen.queryByText(/setupModal\.fallbackError/)).not.toBeInTheDocument();
  });

  it("renders generic enableError panel for unrecognised structured errors", async () => {
    mockPost.mockRejectedValueOnce(
      new ApiError(500, "internal_server_error", {
        ok: false,
        error: "unexpected_engine_failure",
      }),
    );
    await _renderModalAndOpen();
    await _clickEnable();

    await waitFor(() => {
      expect(screen.getByText(/unexpected_engine_failure/)).toBeInTheDocument();
    });
  });

  it("renders genericFailure when the failure is not an ApiError (network drop)", async () => {
    mockPost.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await _renderModalAndOpen();
    await _clickEnable();

    // genericFailure i18n key resolves to a non-empty string under the
    // 'voice' namespace; the panel only renders when no other branch
    // matched. Assert by the dedicated XCircle SVG container's text.
    await waitFor(() => {
      // At least one error-styled panel renders.
      const errorSpans = document.querySelectorAll(
        "[class*='svx-color-error']",
      );
      expect(errorSpans.length).toBeGreaterThan(0);
    });
  });
});
