/** Full-page navigation (own module so tests can mock it; jsdom has no navigation). */
export function navigateTo(url: string): void {
  window.location.assign(url);
}
