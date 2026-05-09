/**
 * CostByPhaseCard tests (issue #43).
 *
 * Covers:
 *  - Renders nothing when total_cost is zero (fresh daemon).
 *  - Renders nothing when fetch fails.
 *  - Renders entries for known phases in order.
 *  - Buckets unknown phases as "other" rather than dropping silently.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";

import { CostByPhaseCard } from "./cost-by-phase-card";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockFetch.mockReset();
});

describe("CostByPhaseCard", () => {
  it("renders nothing when total_cost is zero", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        total_cost: 0,
        total_tokens: 0,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        by_phase: {},
        by_provider: {},
        by_model: {},
        tokens_by_phase: {},
      }),
    );
    const { container } = render(<CostByPhaseCard />);
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    });
    expect(
      container.querySelector('[data-testid="cost-by-phase-card"]'),
    ).toBeNull();
  });

  it("renders nothing when the fetch fails", async () => {
    mockFetch.mockRejectedValueOnce(new Error("network down"));
    const { container } = render(<CostByPhaseCard />);
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    });
    expect(
      container.querySelector('[data-testid="cost-by-phase-card"]'),
    ).toBeNull();
  });

  it("renders rows for known phases", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        total_cost: 1.0,
        total_tokens: 5000,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        by_phase: { think: 0.7, reflect: 0.2, dream: 0.1 },
        by_provider: {},
        by_model: {},
        tokens_by_phase: { think: 3000, reflect: 1500, dream: 500 },
      }),
    );
    render(<CostByPhaseCard />);
    expect(await screen.findByTestId("cost-by-phase-card")).toBeInTheDocument();
    expect(screen.getByTestId("phase-row-think")).toBeInTheDocument();
    expect(screen.getByTestId("phase-row-reflect")).toBeInTheDocument();
    expect(screen.getByTestId("phase-row-dream")).toBeInTheDocument();
  });

  it("buckets unknown phases as 'other' rather than dropping silently", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        total_cost: 1.0,
        total_tokens: 1000,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        by_phase: { think: 0.6, ner_legacy: 0.4 },
        by_provider: {},
        by_model: {},
        tokens_by_phase: {},
      }),
    );
    render(<CostByPhaseCard />);
    expect(await screen.findByTestId("phase-row-think")).toBeInTheDocument();
    expect(screen.getByTestId("phase-row-other")).toBeInTheDocument();
  });
});
