import { useEffect, useState } from 'react';

// The admin UI has exactly two layout boundaries, mirroring Tailwind's `sm`
// and `lg`. Every responsive decision in the app is one of these, so the
// pixel values live here once rather than being restated per component.
export const BREAKPOINTS = {
  sm: 640,
  lg: 1024,
};

// Subscribe to a media query. Returns false during the first render on the
// server or before the effect runs; callers treat that as "narrow", which is
// the safe default since the mobile layout works at any width.
export function useMediaQuery(query) {
  const [matches, setMatches] = useState(() =>
    typeof window === 'undefined' ? false : window.matchMedia(query).matches,
  );

  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = (e) => setMatches(e.matches);
    setMatches(mql.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [query]);

  return matches;
}

// True at or above the named breakpoint. `useBreakpoint('lg')` reads as the
// same condition an `lg:` class expresses, so JS-driven layout and CSS-driven
// layout cannot drift.
export function useBreakpoint(name) {
  return useMediaQuery(`(min-width: ${BREAKPOINTS[name]}px)`);
}

// Prevent the page behind an overlay from scrolling while it is open. Used by
// the nav drawer and the slide-over, both of which own the whole viewport on a
// phone.
export function useScrollLock(active) {
  useEffect(() => {
    if (!active) return undefined;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [active]);
}
