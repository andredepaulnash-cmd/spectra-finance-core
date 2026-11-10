/**
 * The trade screen must distinguish an unavailable market feed from a query
 * that is still loading, and give the operator a visible retry path.
 *
 * @vitest-environment jsdom
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getOrderBook: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: { getOrderBook: mocks.getOrderBook },
}));

vi.mock("@/hooks/useWebSocket", () => ({
  useCondorWebSocket: () => {},
}));

import { OrderBook } from "./OrderBook";

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;

async function flush() {
  for (let i = 0; i < 6; i++) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

async function render() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <OrderBook server="local" connector="kraken_paper_trade" pair="BTC-USD" />
      </QueryClientProvider>,
    );
  });
  await flush();
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  mocks.getOrderBook.mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("OrderBook market-feed status", () => {
  it("shows the API failure and retries into a visible live order book", async () => {
    mocks.getOrderBook
      .mockRejectedValueOnce(new Error("HTTP 500: Order book not available"))
      .mockResolvedValueOnce({
        connector: "kraken_paper_trade",
        trading_pair: "BTC-USD",
        bids: [{ price: 65000, amount: 1.25 }],
        asks: [{ price: 65010, amount: 0.75 }],
      });

    await render();

    expect(container.textContent).toContain("Market feed unavailable");
    expect(container.textContent).toContain("HTTP 500: Order book not available");

    const retry = [...container.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Retry market feed",
    );
    expect(retry).toBeTruthy();

    await act(async () => {
      retry!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(container.textContent).toContain("65000.00");
    expect(container.textContent).toContain("65010.00");
    expect(mocks.getOrderBook).toHaveBeenCalledTimes(2);
  });
});
