/* Vitest unit tests for Mission H4 §T3.4 ResourceHealthSection widget. */

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ResourceHealthSection } from "./ResourceHealthSection";

// Mock react-i18next so tests don't need full i18n setup.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

// Mock useApiPoller hook to inject controlled data.
const mockPollerState: {
  data: unknown;
  error: string | null;
} = { data: null, error: null };

vi.mock("@/hooks/use-api-poller", () => ({
  useApiPoller: () => mockPollerState,
}));

describe("ResourceHealthSection", () => {
  beforeEach(() => {
    mockPollerState.data = null;
    mockPollerState.error = null;
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading state when no data yet", () => {
    mockPollerState.data = null;
    mockPollerState.error = null;
    render(<ResourceHealthSection />);
    expect(screen.getByTestId("resource-health-loading")).toBeInTheDocument();
  });

  it("shows degraded state on poller error", () => {
    mockPollerState.data = null;
    mockPollerState.error = "degraded";
    render(<ResourceHealthSection />);
    expect(screen.getByTestId("resource-health-degraded")).toBeInTheDocument();
  });

  it("renders 8 cohort sections when snapshot present", () => {
    mockPollerState.data = {
      observed_at_unix: 1716143280,
      cohorts: {
        "process.rss_bytes": 100_000_000,
        "asyncio.task_count": 5,
        "to_thread.pool_size": 4,
        "lock_dict.total_cardinality": 42,
        "onnx.session_count": 4,
        "gc.objects_count": 50000,
        "tracemalloc.is_tracing": false,
        "exception_cohort.retained_bytes_estimate": 0,
      },
      canonical_field_count: 28,
      legacy_alias_count: 1,
    };
    render(<ResourceHealthSection />);
    expect(screen.getByTestId("resource-health-sections")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-process")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-asyncio")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-to_thread")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-lock_dict")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-onnx")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-gc")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-tracemalloc")).toBeInTheDocument();
    expect(
      screen.getByTestId("resource-section-exception_cohort"),
    ).toBeInTheDocument();
  });

  it("toggles a section open + closed when its row is clicked", () => {
    mockPollerState.data = {
      observed_at_unix: 1716143280,
      cohorts: {
        "process.rss_bytes": 100_000_000,
        "process.num_threads": 18,
      },
      canonical_field_count: 28,
      legacy_alias_count: 1,
    };
    render(<ResourceHealthSection />);
    const section = screen.getByTestId("resource-section-process");
    const button = section.querySelector("button");
    expect(button).not.toBeNull();
    expect(button?.getAttribute("aria-expanded")).toBe("false");
    if (button) {
      fireEvent.click(button);
    }
    expect(button?.getAttribute("aria-expanded")).toBe("true");
    if (button) {
      fireEvent.click(button);
    }
    expect(button?.getAttribute("aria-expanded")).toBe("false");
  });

  it("renders the field rows in the open section", () => {
    mockPollerState.data = {
      observed_at_unix: 1716143280,
      cohorts: {
        "process.rss_bytes": 100_000_000,
        "process.num_threads": 18,
      },
      canonical_field_count: 28,
      legacy_alias_count: 1,
    };
    render(<ResourceHealthSection />);
    const section = screen.getByTestId("resource-section-process");
    const button = section.querySelector("button");
    if (button) {
      fireEvent.click(button);
    }
    expect(
      screen.getByTestId("resource-field-process.rss_bytes"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("resource-field-process.num_threads"),
    ).toBeInTheDocument();
  });

  it("renders the section field count + fieldsLabel translation", () => {
    mockPollerState.data = {
      observed_at_unix: 1716143280,
      cohorts: {
        "process.rss_bytes": 100_000_000,
        "process.num_threads": 18,
        "process.cpu_percent": 12.5,
      },
      canonical_field_count: 28,
      legacy_alias_count: 1,
    };
    render(<ResourceHealthSection />);
    const section = screen.getByTestId("resource-section-process");
    // 3 present fields (rss + threads + cpu) + the i18n key resources.fieldsLabel.
    expect(section.textContent).toMatch(/3.*resources\.fieldsLabel/);
  });

  it("renders only the fields actually present in the snapshot (partial data)", () => {
    mockPollerState.data = {
      observed_at_unix: 1716143280,
      cohorts: {
        "process.rss_bytes": 100_000_000,
        // process.num_threads missing on purpose
      },
      canonical_field_count: 28,
      legacy_alias_count: 1,
    };
    render(<ResourceHealthSection />);
    const section = screen.getByTestId("resource-section-process");
    const button = section.querySelector("button");
    if (button) {
      fireEvent.click(button);
    }
    expect(
      screen.getByTestId("resource-field-process.rss_bytes"),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("resource-field-process.num_threads"),
    ).not.toBeInTheDocument();
  });

  it("formats array + object + boolean cohort values without crashing", () => {
    mockPollerState.data = {
      observed_at_unix: 1716143280,
      cohorts: {
        "onnx.session_labels": [
          "brain.embedding",
          "voice.vad.silero",
          "voice.wake_word",
        ],
        "lock_dict.per_owner": { "bridge.manager": 12, "voice.health": 30 },
        "tracemalloc.is_tracing": true,
      },
      canonical_field_count: 28,
      legacy_alias_count: 1,
    };
    render(<ResourceHealthSection />);
    const onnx = screen.getByTestId("resource-section-onnx");
    const button = onnx.querySelector("button");
    if (button) {
      fireEvent.click(button);
    }
    expect(
      screen.getByTestId("resource-field-onnx.session_labels"),
    ).toBeInTheDocument();
    const tm = screen.getByTestId("resource-section-tracemalloc");
    const tmBtn = tm.querySelector("button");
    if (tmBtn) {
      fireEvent.click(tmBtn);
    }
    expect(screen.getByText("true")).toBeInTheDocument();
  });

  it("renders an em-dash placeholder when observed_at is missing", () => {
    mockPollerState.data = {
      cohorts: {},
      canonical_field_count: 28,
      legacy_alias_count: 1,
    };
    render(<ResourceHealthSection />);
    const section = screen.getByTestId("resource-health-section");
    expect(section.textContent).toContain("—");
  });
});
