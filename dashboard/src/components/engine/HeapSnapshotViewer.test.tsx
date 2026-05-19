/* Vitest unit tests for Mission H4 §8 T4.3 HeapSnapshotViewer widget. */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HeapSnapshotViewer } from "./HeapSnapshotViewer";

// Mock react-i18next so tests do not need full i18n setup.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      if (options && Object.keys(options).length > 0) {
        return `${key}:${JSON.stringify(options)}`;
      }
      return key;
    },
  }),
}));

const mockApiFetch = vi.fn();

vi.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
}));

function buildPayload(overrides: Record<string, unknown> = {}) {
  return {
    kind: "heap_snapshot",
    schema_version: "1.0",
    observed_at_unix: 1_716_143_280,
    cohort: "rss_growth",
    cohort_observed: 1_073_741_824,
    cohort_budget: 536_870_912,
    tracemalloc_snapshot: {
      top_allocators: [
        {
          rank: 1,
          size_bytes: 524_288_000,
          count: 1234,
          traceback: ["site-packages/foo.py:42", "site-packages/bar.py:99"],
        },
        {
          rank: 2,
          size_bytes: 5_000,
          count: 7,
          traceback: ["src/sovyx/observability/anomaly.py:224"],
        },
      ],
      total_allocators: 2,
    },
    ...overrides,
  };
}

function mockResponse(
  status: number,
  body: unknown,
): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  } as unknown as Response;
}

describe("HeapSnapshotViewer", () => {
  beforeEach(() => {
    mockApiFetch.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading state before the fetch resolves", () => {
    mockApiFetch.mockReturnValue(new Promise(() => undefined));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    expect(screen.getByTestId("heap-snapshot-loading")).toBeInTheDocument();
  });

  it("renders the not-found state on 404", async () => {
    mockApiFetch.mockResolvedValue(mockResponse(404, {}));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-not-found")).toBeInTheDocument();
    });
  });

  it("renders the error state on 5xx", async () => {
    mockApiFetch.mockResolvedValue(mockResponse(503, {}));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-error")).toBeInTheDocument();
    });
  });

  it("renders the error state on network failure", async () => {
    mockApiFetch.mockRejectedValue(new Error("network down"));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-error")).toBeInTheDocument();
    });
  });

  it("renders the top allocator table on success", async () => {
    mockApiFetch.mockResolvedValue(mockResponse(200, buildPayload()));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-table")).toBeInTheDocument();
    });
    expect(screen.getByTestId("heap-snapshot-row-1")).toBeInTheDocument();
    expect(screen.getByTestId("heap-snapshot-row-2")).toBeInTheDocument();
  });

  it("renders cohort context line when cohort + budget present", async () => {
    mockApiFetch.mockResolvedValue(mockResponse(200, buildPayload()));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(
        screen.getByText(/heapSnapshot\.cohortContext/),
      ).toBeInTheDocument();
    });
  });

  it("omits cohort context line when payload has no cohort", async () => {
    mockApiFetch.mockResolvedValue(
      mockResponse(200, buildPayload({ cohort: undefined })),
    );
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-viewer")).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/heapSnapshot\.cohortContext/),
    ).not.toBeInTheDocument();
  });

  it("formats sizes across B / KiB / MiB / GiB ranges", async () => {
    const payload = buildPayload({
      tracemalloc_snapshot: {
        top_allocators: [
          { rank: 1, size_bytes: 500, count: 1, traceback: [] },
          { rank: 2, size_bytes: 5 * 1024, count: 2, traceback: [] },
          { rank: 3, size_bytes: 7 * 1024 * 1024, count: 3, traceback: [] },
          {
            rank: 4,
            size_bytes: 2 * 1024 * 1024 * 1024,
            count: 4,
            traceback: [],
          },
        ],
        total_allocators: 4,
      },
    });
    mockApiFetch.mockResolvedValue(mockResponse(200, payload));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-table")).toBeInTheDocument();
    });
    expect(screen.getByText("500 B")).toBeInTheDocument();
    expect(screen.getByText("5.0 KiB")).toBeInTheDocument();
    expect(screen.getByText("7.0 MiB")).toBeInTheDocument();
    expect(screen.getByText("2.00 GiB")).toBeInTheDocument();
  });

  it("truncates traceback to the last two frames", async () => {
    const payload = buildPayload({
      tracemalloc_snapshot: {
        top_allocators: [
          {
            rank: 1,
            size_bytes: 1024,
            count: 1,
            traceback: ["a.py:1", "b.py:2", "c.py:3", "d.py:4"],
          },
        ],
        total_allocators: 1,
      },
    });
    mockApiFetch.mockResolvedValue(mockResponse(200, payload));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-row-1")).toBeInTheDocument();
    });
    expect(screen.getByText(/c\.py:3 ← d\.py:4/)).toBeInTheDocument();
    expect(screen.queryByText(/a\.py:1/)).not.toBeInTheDocument();
  });

  it("renders an empty body when allocators is empty (table only)", async () => {
    const payload = buildPayload({
      tracemalloc_snapshot: { top_allocators: [], total_allocators: 0 },
    });
    mockApiFetch.mockResolvedValue(mockResponse(200, payload));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-table")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("heap-snapshot-row-1")).not.toBeInTheDocument();
  });

  it("renders the heap-snapshot-viewer wrapper when payload resolves", async () => {
    mockApiFetch.mockResolvedValue(mockResponse(200, buildPayload()));
    render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("heap-snapshot-viewer")).toBeInTheDocument();
    });
    // The viewer wrapper carries the section heading so screen-readers
    // announce "Heap snapshot" via the aria-labelledby anchor.
    expect(
      screen.getByRole("heading", { name: /heapSnapshot\.title/ }),
    ).toBeInTheDocument();
  });

  it("re-fetches when the timestamp prop changes", async () => {
    mockApiFetch.mockResolvedValue(mockResponse(200, buildPayload()));
    const { rerender } = render(<HeapSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith(
        "/api/engine/resources/heap-snapshot/1716143280",
      );
    });
    rerender(<HeapSnapshotViewer timestamp={1716143400} />);
    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith(
        "/api/engine/resources/heap-snapshot/1716143400",
      );
    });
  });
});
