import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { setViewport } from "../setupTests";
import { matchesAtMount, PHONE, useMediaQuery, WIDE } from "./useMediaQuery";

/**
 * The hook reads matchMedia through useSyncExternalStore rather than copying it into
 * state with an effect. What that buys is a value sampled during render instead of one
 * render late, so the tests below assert without waiting: anything that needed a
 * `waitFor` would be a render behind.
 */

function Probe({ query }: { query: string }) {
  return <output>{String(useMediaQuery(query))}</output>;
}

/** Logs what each render was given, so a render that got it wrong is visible even
 *  though act() flushes the correction before the test can see the DOM. */
function LoggingProbe({ query, seen }: { query: string; seen: boolean[] }) {
  seen.push(useMediaQuery(query));
  return null;
}

const shown = () => screen.getByRole("status").textContent;

afterEach(() => setViewport(800));

describe("useMediaQuery", () => {
  it("reports the viewport it is mounted at", () => {
    setViewport(1280);
    render(<Probe query={WIDE} />);
    expect(shown()).toBe("true");

    setViewport(800);
    expect(shown()).toBe("false");
  });

  it("follows a resize across the breakpoint, in both directions", () => {
    setViewport(800);
    render(<Probe query={WIDE} />);
    expect(shown()).toBe("false");
    setViewport(1280);
    expect(shown()).toBe("true");
    setViewport(600);
    expect(shown()).toBe("false");
  });

  it("answers for the query it is given now, not the one it had last render", () => {
    // 800px is above PHONE and below WIDE, so swapping the query flips the answer. The
    // new query is read during the render that changed it, so there are two renders and
    // both are right. Copying the value into state from an effect gave three: the one
    // that still answered for the old query, then the correction.
    setViewport(800);
    const seen: boolean[] = [];
    const { rerender } = render(<LoggingProbe query={PHONE} seen={seen} />);
    rerender(<LoggingProbe query={WIDE} seen={seen} />);
    expect(seen).toEqual([true, false]);
  });

  it("stops listening when unmounted, so a later resize reaches nothing", () => {
    setViewport(800);
    const { unmount } = render(<Probe query={WIDE} />);
    unmount();
    // An un-removed listener would try to render an unmounted tree on this.
    expect(() => setViewport(1280)).not.toThrow();
  });
});

describe("matchesAtMount", () => {
  it("answers for the current viewport without subscribing to it", () => {
    setViewport(1280);
    expect(matchesAtMount(WIDE)).toBe(true);
    setViewport(800);
    expect(matchesAtMount(WIDE)).toBe(false);
  });
});
